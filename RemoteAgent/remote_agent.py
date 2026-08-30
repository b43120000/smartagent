#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 RemoteAgent Supervisor: watcher + durable task queue.

0號 responsibilities in this stage:
- reuse Stage 3 ConversationRegistry trust state;
- round-robin already-armed linked ChatGPT conversations;
- use Stage 5 ConversationWatcher for fresh human turns only;
- parse only Stage 4 RemoteAgent control traffic;
- durably enqueue validated REMOTE_AGENT_REQUEST tasks before ACKing watcher state;
- dedupe requests across polling/restarts;
- enforce Single Active Worker + Queue state semantics.

Explicit non-responsibilities (Stage 8+):
- NO worker process launch;
- NO smartagent_tool parsing/execution;
- NO LocalAgent planner/executor menus;
- NO duplicate watcher implementation.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_core.conversation_registry import ConversationRegistry
from agent_core.conversation_watcher import (
    ConversationWatcher,
    WATCH_BASELINED,
    WATCH_DRIFT,
    WATCH_FRESH,
    WATCH_IDLE,
    WATCH_WAIT,
)
from agent_core.workspace import AGENT_PROJECT_ROOT, normalize_chatgpt_url
from agent_core.task_state import (
    RemoteTaskQueue, TaskStateStore, TaskStateError,
    TASK_QUEUED, TASK_RUNNING, TASK_COMPLETED, TASK_INTERRUPTED,
)
from RemoteAgent.remote_protocol import (
    REMOTE_AGENT_REQUEST,
    analyze_remote_transport,
)

DEFAULT_POLL_INTERVAL_SEC = 3.0
DEFAULT_NAVIGATION_SETTLE_SEC = 0.75
DEFAULT_PROTOCOL_NAME = "smart_agent"
DEFAULT_TASK_STORE = AGENT_PROJECT_ROOT / ".agents" / "remote_tasks.json"


@dataclass(frozen=True)
class ArmedConversation:
    workspace: str
    conversation_url: str
    last_seen_turn: str = ""
    armed_protocols: tuple[str, ...] = ()

    def key(self) -> tuple[str, str]:
        return (os.path.normcase(self.workspace), self.conversation_url)


@dataclass(frozen=True)
class SupervisorEvent:
    kind: str
    workspace: str = ""
    conversation_url: str = ""
    detail: str = ""
    request: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {
            "kind": self.kind,
            "workspace": self.workspace,
            "conversation_url": self.conversation_url,
            "detail": self.detail,
        }
        if self.request is not None:
            value["request"] = dict(self.request)
        return value


def _armed_protocol_names(record: dict[str, Any]) -> tuple[str, ...]:
    protocols = record.get("protocols", {}) if isinstance(record, dict) else {}
    if not isinstance(protocols, dict):
        return ()
    names = []
    for name, state in protocols.items():
        if isinstance(state, dict) and state.get("armed") is True:
            names.append(str(name))
    return tuple(sorted(set(names)))


def list_armed_conversations(registry: ConversationRegistry) -> list[ArmedConversation]:
    """Return linked conversations that already have a verified armed protocol.

    Stage 6 deliberately reuses Stage 3's conversation/session trust state.
    It does not invent a second RemoteAgent-specific arming database.
    """
    records = registry.load()
    result: list[ArmedConversation] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        armed = _armed_protocol_names(record)
        if not armed:
            continue
        workspace = str(record.get("workspace", "") or "")
        url = str(record.get("gpt_url", "") or "")
        if not workspace or not url:
            continue
        try:
            url = normalize_chatgpt_url(url)
        except ValueError:
            continue
        item = ArmedConversation(
            workspace=workspace,
            conversation_url=url,
            last_seen_turn=str(record.get("last_seen_turn", "") or ""),
            armed_protocols=armed,
        )
        if item.key() in seen:
            continue
        seen.add(item.key())
        result.append(item)
    return result


class RemoteAgentSupervisor:
    """Persistent Stage 7 watcher/supervisor. It queues tasks but never executes tools."""

    def __init__(
        self,
        *,
        registry: ConversationRegistry | None = None,
        manager: Any = None,
        task_store: TaskStateStore | None = None,
        task_queue: RemoteTaskQueue | None = None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        navigation_settle_sec: float = DEFAULT_NAVIGATION_SETTLE_SEC,
        max_conversations: int = 5,
        verbose_idle: bool = False,
    ):
        self.registry = registry or ConversationRegistry()
        self.manager = manager
        self.task_store = task_store or TaskStateStore(DEFAULT_TASK_STORE)
        self.task_queue = task_queue or RemoteTaskQueue(self.task_store)
        self._reconciled_tasks = self.task_queue.reconcile_on_start()
        self.poll_interval_sec = max(0.25, float(poll_interval_sec))
        self.navigation_settle_sec = max(0.0, float(navigation_settle_sec))
        self.max_conversations = max(1, int(max_conversations))
        self.verbose_idle = bool(verbose_idle)
        self._stop_requested = False
        self._scraper = None
        self._watchers: dict[tuple[str, str], ConversationWatcher] = {}
        self._current_url = ""
        self.events: list[SupervisorEvent] = []

    def request_stop(self, *_args) -> None:
        self._stop_requested = True

    def _emit(self, event: SupervisorEvent) -> None:
        self.events.append(event)
        prefix = "[RemoteAgent 0]"
        if event.kind == "REMOTE_REQUEST_DETECTED" and event.request:
            request_id = event.request.get("request_id", "")
            print(
                f"{prefix} REMOTE_AGENT_REQUEST detected request_id={request_id} "
                f"workspace={event.workspace}",
                flush=True,
            )
            return
        if event.kind in ("TASK_QUEUED", "TASK_DEDUPED") and event.request:
            print(
                f"{prefix} {event.kind} request_id={event.request.get('request_id','')} "
                f"task_id={event.detail}",
                flush=True,
            )
            return
        if event.kind in ("WATCH_IDLE",) and not self.verbose_idle:
            return
        tail = f" | {event.detail}" if event.detail else ""
        print(f"{prefix} {event.kind}{tail}", flush=True)

    def _get_manager(self):
        if self.manager is None:
            from agent_core.web_runtime import get_manager
            self.manager = get_manager()
        return self.manager

    def _get_scraper(self):
        if self._scraper is None:
            self._scraper = self._get_manager().get_or_create("chatgpt", False)
        return self._scraper

    def _navigate(self, conversation_url: str) -> Any:
        scraper = self._get_scraper()
        target = normalize_chatgpt_url(conversation_url)
        page = getattr(scraper, "_page", None)
        if page is None:
            raise RuntimeError("WebGPT page unavailable")
        # Use the shared verified switch path.  It suppresses/dismisses known
        # blockers, handles project-directory redirects, and refuses to send
        # unless the exact /c/<id> route has a visible composer.
        scraper.navigate_to_conversation(target)
        if self.navigation_settle_sec:
            time.sleep(self.navigation_settle_sec)
        self._current_url = target
        return scraper

    def _watcher_for(self, conv: ArmedConversation, runtime: Any) -> ConversationWatcher:
        key = conv.key()
        watcher = self._watchers.get(key)
        if watcher is None:
            watcher = ConversationWatcher(
                runtime,
                registry=self.registry,
                workspace=conv.workspace,
                conversation_url=conv.conversation_url,
                role="user",
                last_seen_fingerprint=conv.last_seen_turn,
            )
            self._watchers[key] = watcher
        else:
            watcher.runtime = runtime
        return watcher

    @staticmethod
    def _remote_requests_from_turn(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Parse RemoteAgent control only; SmartAgent tool text naturally yields none."""
        report = analyze_remote_transport(text)
        messages = list(report.get("messages") or [])
        diagnostics = list(report.get("diagnostics") or [])
        requests = [m for m in messages if m.get("type") == REMOTE_AGENT_REQUEST]
        return requests, diagnostics

    def _durably_enqueue_request(
        self, request: dict[str, Any], *, conv: ArmedConversation, origin_turn_fingerprint: str
    ):
        # Stage 7 trust boundary: routing always comes from the authorized
        # conversation registry. Remote control text cannot redirect a task to
        # another conversation or arbitrary local directory.
        finder = getattr(self.registry, "find_by_url", None)
        record = finder(conv.conversation_url) if callable(finder) else {}
        record = record or {}
        workspace = str(record.get("active_workspace") or conv.workspace or "").strip()
        if not workspace or not Path(workspace).exists() or not Path(workspace).is_dir():
            raise ValueError("authorized conversation has no usable active workspace")
        enriched = dict(request)
        enriched["conversation_url"] = conv.conversation_url
        enriched["workspace"] = str(Path(workspace).resolve())
        enriched["transport"] = "WEBGPT"
        enriched["endpoint"] = "chatgpt.com"
        from agent_core.routing import build_route_context
        route_context = build_route_context(
            request=str(enriched.get("request", "") or ""),
            source="remote",
            conversation_url=conv.conversation_url,
            requested_mode="AUTO",
            requested_carrier="AUTO",
        )
        reply_route = {
            "transport": "WEBGPT",
            "endpoint": "chatgpt.com",
            "conversation_url": conv.conversation_url,
        }
        return self.task_queue.enqueue_remote_request(
            enriched,
            origin_turn_fingerprint=origin_turn_fingerprint,
            metadata={
                "armed_protocols": list(conv.armed_protocols),
                "detected_by": "RemoteAgentSupervisor",
                "routing_source": "conversation_registry",
                "route_context": route_context,
                "reply_route": reply_route,
                "transport": "WEBGPT",
            },
        )

    def poll_conversation(self, conv: ArmedConversation) -> list[SupervisorEvent]:
        runtime = self._navigate(conv.conversation_url)
        watcher = self._watcher_for(conv, runtime)

        # Stage 7 invariant: do NOT advance last_seen_turn until every accepted
        # Remote request in that human turn is durably persisted.
        result = watcher.poll(acknowledge=False)
        emitted: list[SupervisorEvent] = []

        if result.status == WATCH_FRESH:
            for turn in result.fresh_turns:
                requests, diagnostics = self._remote_requests_from_turn(turn.raw_text or turn.text)
                turn_committed = True
                if diagnostics:
                    event = SupervisorEvent(
                        "REMOTE_PROTOCOL_REJECTED", conv.workspace, conv.conversation_url,
                        json.dumps(diagnostics, ensure_ascii=False),
                    )
                    self._emit(event); emitted.append(event)

                for request in requests:
                    detected = SupervisorEvent(
                        "REMOTE_REQUEST_DETECTED", conv.workspace, conv.conversation_url,
                        detail=f"origin_turn={turn.fingerprint}", request=request,
                    )
                    self._emit(detected); emitted.append(detected)
                    try:
                        task, created = self._durably_enqueue_request(
                            request, conv=conv, origin_turn_fingerprint=turn.fingerprint
                        )
                    except ValueError as exc:
                        # Invalid routing is deterministic bad control traffic, not a
                        # transient persistence failure. Consume the turn but never queue.
                        rejected = SupervisorEvent(
                            "REMOTE_REQUEST_ROUTE_REJECTED", conv.workspace, conv.conversation_url,
                            detail=str(exc), request=request,
                        )
                        self._emit(rejected); emitted.append(rejected)
                        continue
                    except Exception as exc:
                        # Fail closed: persistence failure means cursor is NOT ACKed,
                        # so the same turn is retried on the next poll.
                        turn_committed = False
                        failed = SupervisorEvent(
                            "TASK_ENQUEUE_FAILED", conv.workspace, conv.conversation_url,
                            detail=f"{type(exc).__name__}: {exc}", request=request,
                        )
                        self._emit(failed); emitted.append(failed)
                        break

                    queued_event = SupervisorEvent(
                        "TASK_QUEUED" if created else "TASK_DEDUPED",
                        conv.workspace, conv.conversation_url,
                        detail=task.task_id, request=request,
                    )
                    self._emit(queued_event); emitted.append(queued_event)

                if not requests and not diagnostics:
                    ignored = SupervisorEvent(
                        "FRESH_TURN_IGNORED", conv.workspace, conv.conversation_url,
                        detail="not RemoteAgent control traffic",
                    )
                    self._emit(ignored); emitted.append(ignored)

                if turn_committed:
                    watcher.acknowledge(turn.fingerprint)
                else:
                    # Preserve ordering: do not consume later fresh turns after an
                    # enqueue failure in an earlier turn.
                    break

        elif result.status == WATCH_WAIT:
            event = SupervisorEvent("WATCH_WAIT", conv.workspace, conv.conversation_url, result.detail)
            self._emit(event); emitted.append(event)
        elif result.status == WATCH_BASELINED:
            event = SupervisorEvent("WATCH_BASELINED", conv.workspace, conv.conversation_url, result.detail)
            self._emit(event); emitted.append(event)
        elif result.status == WATCH_DRIFT:
            # Stage 5 intentionally does not advance cursor with acknowledge=False
            # on drift. Explicitly re-baseline without replaying visible history.
            watcher.acknowledge(result.latest_fingerprint)
            event = SupervisorEvent("WATCH_DRIFT", conv.workspace, conv.conversation_url, result.detail)
            self._emit(event); emitted.append(event)
        else:
            event = SupervisorEvent("WATCH_IDLE", conv.workspace, conv.conversation_url)
            self._emit(event); emitted.append(event)
        return emitted

    def poll_once(self) -> list[SupervisorEvent]:
        conversations = list_armed_conversations(self.registry)[: self.max_conversations]
        if not conversations:
            event = SupervisorEvent("NO_ARMED_CONVERSATIONS", detail="registry has no armed linked chats")
            self._emit(event)
            return [event]

        emitted: list[SupervisorEvent] = []
        for conv in conversations:
            if self._stop_requested:
                break
            try:
                emitted.extend(self.poll_conversation(conv))
            except Exception as exc:
                event = SupervisorEvent(
                    "WATCH_ERROR",
                    conv.workspace,
                    conv.conversation_url,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                self._emit(event); emitted.append(event)
        return emitted

    def run(self) -> int:
        conversations = list_armed_conversations(self.registry)[: self.max_conversations]
        print("[RemoteAgent 0] Stage 7 Supervisor starting", flush=True)
        print(f"[RemoteAgent 0] armed conversations={len(conversations)} max={self.max_conversations}", flush=True)
        for idx, conv in enumerate(conversations, 1):
            print(
                f"  [{idx}] {conv.workspace} -> {conv.conversation_url} "
                f"armed={','.join(conv.armed_protocols)}",
                flush=True,
            )
        print("[RemoteAgent 0] TASK QUEUE ENABLED: worker launch/tool execution is still disabled in Stage 7", flush=True)
        print(f"[RemoteAgent 0] task store={self.task_store.path} summary={self.task_queue.summary()}", flush=True)
        for task in self._reconciled_tasks:
            print(f"[RemoteAgent 0] TASK_RECONCILED task_id={task.task_id} state={task.state}", flush=True)

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, self.request_stop)
        except Exception:
            pass

        try:
            while not self._stop_requested:
                self.poll_once()
                deadline = time.monotonic() + self.poll_interval_sec
                while not self._stop_requested and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            return 0
        finally:
            try:
                self._get_manager().close_all()
            except Exception:
                pass
            self._scraper = None
            print("[RemoteAgent 0] stopped; browser/profile released", flush=True)
            try:
                signal.signal(signal.SIGINT, previous_int)
                if hasattr(signal, "SIGTERM") and previous_term is not None:
                    signal.signal(signal.SIGTERM, previous_term)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Deterministic Stage 7 tests: no Playwright/network/Ollama required.
# ---------------------------------------------------------------------------

class _FakeElement:
    def __init__(self, text: str):
        self._text = text
    def inner_text(self): return self._text
    def text_content(self): return self._text
    def get_attribute(self, _name: str): return None
    def query_selector_all(self, _selector: str): return []


class _FakePage:
    def __init__(self, runtime):
        self.runtime = runtime
        self.url = "https://chatgpt.com/"
        self.goto_calls: list[str] = []
    def goto(self, url, **_kwargs):
        self.url = url
        self.goto_calls.append(url)
        self.runtime.active_url = url


class _FakeScraper:
    def __init__(self, conversations: dict[str, list[str]]):
        self.conversations = conversations
        self.active_url = "https://chatgpt.com/"
        self.generation_active = False
        self._page = _FakePage(self)
    def _turn_elements(self, role: str):
        if role != "user": return []
        return [_FakeElement(x) for x in self.conversations.get(self.active_url, [])]
    def _is_generation_active(self): return self.generation_active
    def _dismiss_known_blocking_dialogs(self): return False
    def navigate_to_conversation(self, url: str):
        self._page.goto(url)


class _FakeManager:
    def __init__(self, scraper):
        self.scraper = scraper
        self.create_count = 0
        self.close_count = 0
    def get_or_create(self, service: str, headless: bool = False):
        assert service == "chatgpt"
        self.create_count += 1
        return self.scraper
    def close_all(self): self.close_count += 1


class _FakeRegistry:
    def __init__(self, records: Iterable[dict[str, Any]]):
        self.records = [json.loads(json.dumps(x)) for x in records]
    def load(self): return json.loads(json.dumps(self.records))
    def find(self, workspace: str, url: str):
        for rec in self.records:
            if rec["workspace"] == workspace and rec["gpt_url"] == url:
                return json.loads(json.dumps(rec))
        return None
    def set_last_seen_turn(self, workspace: str, url: str, fp: str):
        for rec in self.records:
            if rec["workspace"] == workspace and rec["gpt_url"] == url:
                rec["last_seen_turn"] = fp
                return


def _fake_record(workspace: str, url: str, *, armed: bool = True) -> dict[str, Any]:
    return {
        "workspace": workspace,
        "gpt_url": url,
        "last_seen_turn": "",
        "protocols": {"smart_agent": {"armed": armed, "protocol_name": "smart_agent"}},
    }


def run_remote_supervisor_self_tests() -> dict[str, Any]:
    import tempfile
    from RemoteAgent.remote_protocol import format_remote_message, new_request

    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        task_store = TaskStateStore(Path(td) / "remote_tasks.json")
        url1 = "https://chatgpt.com/c/stage7-a"
        url2 = "https://chatgpt.com/c/stage7-b"
        records = [_fake_record(str(PROJECT_ROOT), url1), _fake_record(str(PROJECT_ROOT), url2)]
        registry = _FakeRegistry(records)
        scraper = _FakeScraper({url1: ["old a"], url2: ["old b"]})
        manager = _FakeManager(scraper)
        sup = RemoteAgentSupervisor(
            registry=registry, manager=manager, task_store=task_store,
            poll_interval_sec=0.25, navigation_settle_sec=0.0, max_conversations=5,
        )

        first = sup.poll_once()
        results["two_armed_conversations_round_robin"] = (
            scraper._page.goto_calls == [url1, url2]
            and sum(e.kind == "WATCH_BASELINED" for e in first) == 2
        )
        results["single_persistent_scraper_instance"] = manager.create_count == 1

        idle = []
        for _ in range(20):
            idle.extend(sup.poll_once())
        results["twenty_idle_polls_no_task"] = len(task_store.all()) == 0

        scraper.conversations[url1].append(
            '```smartagent_tool\n{"tool":"run_command","action_id":"A-X","command":"echo SHOULD_NOT_RUN"}\n```'
        )
        smart_events = sup.poll_once()
        results["smartagent_tool_never_executes_or_queues"] = (
            len(task_store.all()) == 0
            and any(e.kind == "FRESH_TURN_IGNORED" for e in smart_events)
        )

        remote = new_request(
            conversation_url=url2, workspace=str(PROJECT_ROOT),
            request="list directory only", request_id="RR-STAGE7-1",
        )
        scraper.conversations[url2].append(format_remote_message(remote))
        detected = sup.poll_once()
        detected_again = sup.poll_once()
        tasks = task_store.all()
        results["remote_request_durable_enqueue_exactly_once"] = (
            len(tasks) == 1 and tasks[0].state == TASK_QUEUED
            and sum(e.kind == "TASK_QUEUED" for e in detected) == 1
            and not any(e.kind in ("TASK_QUEUED", "REMOTE_REQUEST_DETECTED") for e in detected_again)
        )

        # V1 identity is transport + conversation + turn identity. Repeating the
        # same request_id/text in a distinct human turn is a distinct task.
        scraper.conversations[url2].append(format_remote_message(remote))
        replay_events = sup.poll_once()
        results["same_request_new_turn_distinct"] = (
            len(task_store.all()) == 2 and any(e.kind == "TASK_QUEUED" for e in replay_events)
        )

        # Remote-supplied routing is ignored; the authorized monitored binding
        # remains the only routing source.
        wrong = new_request(
            conversation_url=url2, workspace=str(PROJECT_ROOT / "other"),
            request="must not queue", request_id="RR-WRONG-ROUTE",
        )
        scraper.conversations[url2].append(format_remote_message(wrong))
        wrong_events = sup.poll_once()
        routed_tasks = task_store.all()
        wrong_task = next((t for t in routed_tasks if t.request_id == "RR-WRONG-ROUTE"), None)
        results["remote_routing_override_ignored"] = (
            len(routed_tasks) == 3
            and any(e.kind == "TASK_QUEUED" for e in wrong_events)
            and wrong_task is not None
            and wrong_task.conversation_url == url2
            and Path(wrong_task.workspace).resolve() == PROJECT_ROOT.resolve()
            and wrong_task.metadata.get("reply_route", {}).get("conversation_url") == url2
        )

        # Durable enqueue must happen before watcher cursor ACK.  Simulate a
        # transient store failure and confirm the same turn is retried.
        class _FailOnceQueue:
            def __init__(self, real_queue):
                self.real_queue = real_queue
                self.failed = False
            def reconcile_on_start(self): return []
            def summary(self): return self.real_queue.summary()
            def enqueue_remote_request(self, *args, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise OSError("simulated persistence failure")
                return self.real_queue.enqueue_remote_request(*args, **kwargs)

        url3 = "https://chatgpt.com/c/stage7-durable"
        reg3 = _FakeRegistry([_fake_record(str(PROJECT_ROOT), url3)])
        scraper3 = _FakeScraper({url3: ["old"]})
        manager3 = _FakeManager(scraper3)
        store3 = TaskStateStore(Path(td) / "durable_tasks.json")
        realq3 = RemoteTaskQueue(store3)
        sup3 = RemoteAgentSupervisor(
            registry=reg3, manager=manager3, task_store=store3, task_queue=_FailOnceQueue(realq3),
            poll_interval_sec=0.25, navigation_settle_sec=0.0, max_conversations=5,
        )
        sup3.poll_once()  # baseline
        req3 = new_request(
            conversation_url=url3, workspace=str(PROJECT_ROOT),
            request="durable retry", request_id="RR-DURABLE-1",
        )
        scraper3.conversations[url3].append(format_remote_message(req3))
        failed_events = sup3.poll_once()
        retry_events = sup3.poll_once()
        results["enqueue_failure_does_not_ack_turn"] = (
            any(e.kind == "TASK_ENQUEUE_FAILED" for e in failed_events)
            and any(e.kind == "TASK_QUEUED" for e in retry_events)
            and len(store3.all()) == 1
        )

        results["stage7_still_has_no_worker_or_tool_execution"] = all(
            not hasattr(sup, name) for name in ("launch_worker", "execute_tool")
        )

        registry2 = _FakeRegistry([_fake_record(str(PROJECT_ROOT), url1, armed=False)])
        results["unarmed_conversations_excluded"] = list_armed_conversations(registry2) == []

    results["all_passed"] = all(results.values())
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteAgent Stage 7 Supervisor (durable queue, no worker)")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--max-conversations", type=int, default=5)
    parser.add_argument("--once", action="store_true", help="poll one round then exit")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verbose-idle", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        result = run_remote_supervisor_self_tests()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("all_passed") else 1

    supervisor = RemoteAgentSupervisor(
        poll_interval_sec=args.poll_interval,
        max_conversations=args.max_conversations,
        verbose_idle=args.verbose_idle,
    )
    if args.once:
        supervisor.poll_once()
        try:
            supervisor._get_manager().close_all()
        except Exception:
            pass
        return 0
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
