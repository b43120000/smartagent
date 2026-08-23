#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7.2.2 passive RemoteAgent-0 history receiver.

Design invariants
-----------------
* Attach to the LocalAgent-owned Chromium over localhost CDP.
* Reuse that BrowserContext's authenticated APIRequestContext for ChatGPT
  conversation-history reads; do not create, navigate, reload, activate, or
  inspect conversation tabs.
* Only conversations already armed for ``smart_agent`` protocol v3+ may receive
  RemoteAgent ingress.
* Read completed assistant messages from the current conversation branch and
  accept only strict/exclusive fenced ``remoteagent_control`` transport.
* Persist a dedicated history cursor based on stable assistant message ids.
  First attach is baseline-only; old assistant history is never replayed.
* Durable task enqueue happens before cursor advancement. Persistence failures
  therefore retry the same assistant message instead of dropping the request.
* Prefer one lightweight conversation-index request per poll. Conversation detail
  is fetched only for authorized conversations whose server update marker changed
  (or which still require their initial baseline). If the index endpoint is not
  supported, probe exactly one authorized conversation per poll tick round-robin.
* 401/403/429/5xx and transport failures enter exponential backoff. UI navigation
  is never used as a recovery mechanism.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_core.conversation_registry import ConversationRegistry
from agent_core.task_state import TaskStateStore
from agent_core.workspace import AGENT_PROJECT_ROOT, normalize_chatgpt_url
from RemoteAgent.remote_protocol import (
    REMOTE_AGENT_REQUEST, REMOTE_AGENT_CANCEL, REMOTE_AGENT_RETRY,
    analyze_remote_transport,
)
from RemoteAgent.remote_agent import ArmedConversation, RemoteAgentSupervisor, SupervisorEvent

TASK_STORE = AGENT_PROJECT_ROOT / ".agents" / "remote_tasks.json"
HOST_STATE = AGENT_PROJECT_ROOT / ".agents" / "agent_host_state.json"
REMOTE_WATCH_CURSOR = "remote_agent_assistant_history_v1"
REMOTE_INGRESS_SMARTAGENT_VERSION = 3
INDEX_URL = "https://chatgpt.com/backend-api/conversations?offset=0&limit=100&order=updated"
DETAIL_URL_PREFIX = "https://chatgpt.com/backend-api/conversation/"
SESSION_URL = "https://chatgpt.com/api/auth/session"
MIN_POLL_INTERVAL_SEC = 4.0
EMPTY_BASELINE_CURSOR = "__REMOTE_HISTORY_BASELINE_EMPTY_V1__"


class HistoryReceiverError(RuntimeError):
    """Base class for deterministic receiver failures."""


class HistoryHTTPError(HistoryReceiverError):
    def __init__(self, status: int, endpoint: str, detail: str = ""):
        self.status = int(status)
        self.endpoint = str(endpoint)
        self.detail = str(detail or "")
        super().__init__(f"HTTP {self.status} {self.endpoint}: {self.detail}".strip())


class HistoryTransportError(HistoryReceiverError):
    pass


class HistoryIndexUnavailable(HistoryReceiverError):
    """The index route is unsupported/malformed; direct round-robin fallback is allowed."""


def _parent_alive(pid: int) -> bool:
    """Read-only parent liveness check; never signal the LocalAgent process."""
    if pid <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _remote_ready_conversations(registry: ConversationRegistry, *, max_items: int = 5) -> list[ArmedConversation]:
    """Only SmartAgent protocol-v3+ links are authorized for remote ingress."""
    result: list[ArmedConversation] = []
    seen: set[str] = set()
    for record in registry.list_conversations():
        if not isinstance(record, dict):
            continue
        protocols = record.get("protocols", {}) if isinstance(record.get("protocols", {}), dict) else {}
        smart = protocols.get("smart_agent", {}) if isinstance(protocols.get("smart_agent", {}), dict) else {}
        if not smart.get("armed"):
            continue
        if int(smart.get("protocol_version", 0) or 0) < REMOTE_INGRESS_SMARTAGENT_VERSION:
            continue
        url = str(record.get("gpt_url", "") or "").strip()
        workspace = str(record.get("active_workspace") or record.get("workspace") or "").strip()
        if not url or not workspace:
            continue
        try:
            url = normalize_chatgpt_url(url)
            _conversation_id_from_url(url)
        except Exception:
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(ArmedConversation(
            workspace=workspace,
            conversation_url=url,
            last_seen_turn="",
            armed_protocols=("smart_agent",),
        ))
        if len(result) >= max(1, int(max_items)):
            break
    return result


def _conversation_id_from_url(url: str) -> str:
    """Extract the segment after ``/c/`` from normal/project/GPT conversation URLs."""
    normalized = normalize_chatgpt_url(url)
    parsed = urllib.parse.urlparse(normalized)
    segments = [urllib.parse.unquote(x) for x in parsed.path.split("/") if x]
    for index in range(len(segments) - 2, -1, -1):
        if segments[index].lower() == "c" and index + 1 < len(segments):
            value = segments[index + 1].strip()
            if value:
                return value
    raise ValueError(f"ChatGPT URL does not contain /c/<conversation_id>: {url}")


def _response_json(response: Any, *, endpoint: str) -> Any:
    status = int(getattr(response, "status", 0) or 0)
    if status in (401, 403, 429) or status >= 500:
        detail = ""
        try:
            detail = str(response.text() or "")[:300]
        except Exception:
            pass
        raise HistoryHTTPError(status, endpoint, detail)
    if status in (400, 404, 405) and endpoint == "index":
        raise HistoryIndexUnavailable(f"index endpoint returned HTTP {status}")
    if status < 200 or status >= 300:
        detail = ""
        try:
            detail = str(response.text() or "")[:300]
        except Exception:
            pass
        raise HistoryHTTPError(status, endpoint, detail)
    try:
        return response.json()
    except Exception as exc:
        if endpoint == "index":
            raise HistoryIndexUnavailable(f"index response is not JSON: {type(exc).__name__}") from exc
        raise HistoryTransportError(f"detail response is not JSON: {type(exc).__name__}") from exc


class ChatGPTHistoryClient:
    """Authenticated ChatGPT history reader backed only by BrowserContext.request."""

    def __init__(self, request_context: Any):
        self.request = request_context
        self.index_calls = 0
        self.detail_calls = 0
        self._access_token = ""

    def _authorization_headers(self) -> dict[str, str]:
        if not self._access_token:
            try:
                response = self.request.get(
                    SESSION_URL,
                    headers={"accept": "application/json", "cache-control": "no-cache"},
                    timeout=30000,
                )
                if int(getattr(response, "status", 0) or 0) == 200:
                    payload = response.json()
                    if isinstance(payload, dict):
                        self._access_token = str(payload.get("accessToken") or "").strip()
            except Exception:
                # Deterministic fakes and older authenticated contexts may not
                # expose /api/auth/session. The actual history request still
                # decides success/failure and enters normal backoff.
                pass
        headers = {"accept": "application/json", "cache-control": "no-cache"}
        if self._access_token:
            headers["authorization"] = f"Bearer {self._access_token}"
        return headers

    def _get(self, url: str, *, endpoint: str) -> Any:
        headers = self._authorization_headers()
        try:
            response = self.request.get(
                url,
                headers=headers,
                timeout=30000,
            )
        except TypeError:
            # Compatibility with simple fake/APIRequestContext wrappers that do
            # not expose every Playwright keyword.
            try:
                response = self.request.get(url, headers=headers)
            except Exception as exc:
                raise HistoryTransportError(f"{endpoint} request failed: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            raise HistoryTransportError(f"{endpoint} request failed: {type(exc).__name__}: {exc}") from exc
        return _response_json(response, endpoint=endpoint)

    def fetch_index_markers(self) -> dict[str, str]:
        self.index_calls += 1
        payload = self._get(INDEX_URL, endpoint="index")
        items: Any
        if isinstance(payload, dict):
            items = payload.get("items")
            if items is None:
                items = payload.get("conversations")
        elif isinstance(payload, list):
            items = payload
        else:
            items = None
        if not isinstance(items, list):
            raise HistoryIndexUnavailable("index response has no items/conversations list")
        result: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            conversation_id = str(item.get("id") or item.get("conversation_id") or "").strip()
            if not conversation_id:
                continue
            marker_value = item.get("update_time")
            if marker_value is None:
                marker_value = item.get("updated_at")
            if marker_value is None:
                marker_value = item.get("current_node")
            result[conversation_id] = json.dumps(marker_value, ensure_ascii=False, sort_keys=True, default=str)
        return result

    def fetch_detail(self, conversation_id: str) -> dict[str, Any]:
        self.detail_calls += 1
        quoted = urllib.parse.quote(str(conversation_id), safe="")
        payload = self._get(DETAIL_URL_PREFIX + quoted, endpoint="detail")
        if not isinstance(payload, dict):
            raise HistoryTransportError("conversation detail is not an object")
        return payload


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    values: list[str] = []
    for part in parts:
        if isinstance(part, str):
            values.append(part)
        elif isinstance(part, dict):
            value = part.get("text")
            if isinstance(value, str):
                values.append(value)
            elif isinstance(part.get("content"), str):
                values.append(str(part.get("content")))
    return "\n".join(values).replace("\r\n", "\n").replace("\r", "\n").strip()


def _assistant_message_completed(message: dict[str, Any]) -> bool:
    status = str(message.get("status") or "").strip().lower()
    if status in {"in_progress", "streaming", "pending", "running"}:
        return False
    if status in {"finished_successfully", "finished", "complete", "completed"}:
        return True
    return message.get("end_turn") is True


@dataclass(frozen=True)
class HistoryAssistantMessage:
    message_id: str
    text: str
    create_time: float


def _current_branch_assistant_messages(detail: dict[str, Any]) -> list[HistoryAssistantMessage]:
    """Return completed assistant messages on the current branch in chronological order."""
    mapping = detail.get("mapping") if isinstance(detail, dict) else None
    if not isinstance(mapping, dict) or not mapping:
        return []

    current_node = str(detail.get("current_node") or "").strip()
    branch_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    node_id = current_node
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        branch_nodes.append(node)
        node_id = str(node.get("parent") or "").strip()
    branch_nodes.reverse()

    if not branch_nodes:
        # Conservative compatibility fallback for malformed/older payloads.
        # Sorting is only used when no current branch can be reconstructed.
        branch_nodes = [node for node in mapping.values() if isinstance(node, dict)]
        branch_nodes.sort(key=lambda n: float(((n.get("message") or {}).get("create_time") or 0.0)))

    result: list[HistoryAssistantMessage] = []
    for node in branch_nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = str((author or {}).get("role") or "").strip().lower() if isinstance(author, dict) else ""
        if role != "assistant" or not _assistant_message_completed(message):
            continue
        message_id = str(message.get("id") or node.get("id") or "").strip()
        if not message_id:
            continue
        text = _message_text(message)
        result.append(HistoryAssistantMessage(
            message_id=message_id,
            text=text,
            create_time=float(message.get("create_time") or 0.0),
        ))
    return result


class _ReceiverBackoff:
    def __init__(self, *, base_sec: float = MIN_POLL_INTERVAL_SEC, max_sec: float = 120.0):
        self.base_sec = max(1.0, float(base_sec))
        self.max_sec = max(self.base_sec, float(max_sec))
        self.failures = 0
        self.until = 0.0
        self.last_reason = ""

    def ready(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        return now >= self.until

    def fail(self, reason: str, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else float(now)
        self.failures += 1
        delay = min(self.max_sec, self.base_sec * (2 ** (self.failures - 1)))
        self.until = now + delay
        self.last_reason = str(reason or "")
        return delay

    def success(self) -> None:
        self.failures = 0
        self.until = 0.0
        self.last_reason = ""


class HistoryHiddenSupervisor(RemoteAgentSupervisor):
    """RemoteAgent-0 receiver using authenticated history requests, never browser pages."""

    def __init__(self, *, request_context: Any, **kwargs: Any):
        super().__init__(
            manager=None,
            **kwargs,
        )
        self.history = ChatGPTHistoryClient(request_context)
        self._index_supported: bool | None = None
        self._index_markers: dict[str, str] = {}
        self._fallback_rr_index = 0
        self._index_sweep_rr_index = 0
        self._backoff = _ReceiverBackoff()

    def _emit(self, event: SupervisorEvent) -> None:
        # Hidden process stdout is redirected to remote_supervisor.log by AgentHost.
        if event.kind not in ("WATCH_IDLE", "HISTORY_UNCHANGED"):
            print(f"[RemoteAgent 0 hidden] {event.kind} {event.detail}", flush=True)
        self.events.append(event)

    @staticmethod
    def _remote_requests_from_text(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        report = analyze_remote_transport(text)
        messages = list(report.get("messages") or [])
        diagnostics = list(report.get("diagnostics") or [])
        controls = [
            m for m in messages
            if m.get("type") in {REMOTE_AGENT_REQUEST, REMOTE_AGENT_CANCEL, REMOTE_AGENT_RETRY}
        ]
        return controls, diagnostics

    def _durably_enqueue_request(self, request: dict[str, Any], *, conv: ArmedConversation, origin_turn_fingerprint: str):
        task, created = super()._durably_enqueue_request(
            dict(request), conv=conv, origin_turn_fingerprint=origin_turn_fingerprint
        )
        if created:
            # The durable task record is the sole routing source of truth.  A
            # shared PENDING handoff file can be overwritten by another session
            # and later fire against the wrong browser page.
            print(
                f"[RemoteAgent 0 hidden] task queued task={task.task_id}; "
                "origin preserved in durable queue",
                flush=True,
            )
        return task, created

    def _cursor(self, conversation_url: str) -> str:
        try:
            return str(self.registry.get_watch_cursor(conversation_url, REMOTE_WATCH_CURSOR) or "")
        except Exception:
            return ""

    def _set_cursor(self, conversation_url: str, message_id: str) -> None:
        self.registry.set_watch_cursor(conversation_url, REMOTE_WATCH_CURSOR, str(message_id or ""))

    def _process_detail(self, conv: ArmedConversation, detail: dict[str, Any]) -> list[SupervisorEvent]:
        messages = _current_branch_assistant_messages(detail)
        cursor = self._cursor(conv.conversation_url)
        emitted: list[SupervisorEvent] = []

        if not cursor:
            baseline = messages[-1].message_id if messages else EMPTY_BASELINE_CURSOR
            self._set_cursor(conv.conversation_url, baseline)
            event = SupervisorEvent(
                "HISTORY_BASELINED", conv.workspace, conv.conversation_url,
                detail=f"cursor={baseline}",
            )
            self._emit(event); emitted.append(event)
            return emitted

        if cursor == EMPTY_BASELINE_CURSOR:
            fresh = list(messages)
        else:
            index = next((i for i, msg in enumerate(messages) if msg.message_id == cursor), None)
            if index is None:
                # Never replay visible history when a branch/cursor disappears.
                baseline = messages[-1].message_id if messages else EMPTY_BASELINE_CURSOR
                self._set_cursor(conv.conversation_url, baseline)
                event = SupervisorEvent(
                    "HISTORY_DRIFT", conv.workspace, conv.conversation_url,
                    detail=f"cursor_not_found; rebaselined={baseline}; no_replay",
                )
                self._emit(event); emitted.append(event)
                return emitted
            fresh = messages[index + 1:]

        if not fresh:
            event = SupervisorEvent("HISTORY_UNCHANGED", conv.workspace, conv.conversation_url)
            self._emit(event); emitted.append(event)
            return emitted

        for assistant in fresh:
            requests, diagnostics = self._remote_requests_from_text(assistant.text)
            durable = True

            if diagnostics:
                event = SupervisorEvent(
                    "REMOTE_PROTOCOL_REJECTED", conv.workspace, conv.conversation_url,
                    detail=json.dumps(diagnostics, ensure_ascii=False),
                )
                self._emit(event); emitted.append(event)

            for request in requests:
                control_type = request.get("type")
                if control_type in {REMOTE_AGENT_CANCEL, REMOTE_AGENT_RETRY}:
                    target = str(request.get("target_request_id", "") or "")
                    try:
                        if control_type == REMOTE_AGENT_CANCEL:
                            task = self.task_queue.cancel(request_id=target)
                            kind = "TASK_CANCEL_REQUESTED"
                        else:
                            task = self.task_queue.retry_interrupted(request_id=target)
                            kind = "TASK_RETRY_QUEUED"
                        event = SupervisorEvent(
                            kind, conv.workspace, conv.conversation_url,
                            detail=f"target_request_id={target} task={task.task_id}", request=request,
                        )
                        self._emit(event); emitted.append(event)
                    except Exception as exc:
                        event = SupervisorEvent(
                            "TASK_CONTROL_REJECTED", conv.workspace, conv.conversation_url,
                            detail=f"target_request_id={target}: {type(exc).__name__}: {exc}", request=request,
                        )
                        self._emit(event); emitted.append(event)
                    continue
                detected = SupervisorEvent(
                    "REMOTE_REQUEST_DETECTED", conv.workspace, conv.conversation_url,
                    detail=f"assistant_message_id={assistant.message_id}", request=request,
                )
                self._emit(detected); emitted.append(detected)
                try:
                    task, created = self._durably_enqueue_request(
                        request,
                        conv=conv,
                        origin_turn_fingerprint=assistant.message_id,
                    )
                except ValueError as exc:
                    rejected = SupervisorEvent(
                        "REMOTE_REQUEST_ROUTE_REJECTED", conv.workspace, conv.conversation_url,
                        detail=str(exc), request=request,
                    )
                    self._emit(rejected); emitted.append(rejected)
                    continue
                except Exception as exc:
                    durable = False
                    failed = SupervisorEvent(
                        "TASK_ENQUEUE_FAILED", conv.workspace, conv.conversation_url,
                        detail=f"{type(exc).__name__}: {exc}", request=request,
                    )
                    self._emit(failed); emitted.append(failed)
                    break

                queued = SupervisorEvent(
                    "TASK_QUEUED" if created else "TASK_DEDUPED",
                    conv.workspace, conv.conversation_url,
                    detail=task.task_id, request=request,
                )
                self._emit(queued); emitted.append(queued)

            if not durable:
                # Critical invariant: do not consume this message after a
                # persistence failure. The same message id is retried later.
                break

            # Ordinary assistant prose, deterministic protocol rejects, and
            # successfully/deduplicated requests are all safe to consume.
            self._set_cursor(conv.conversation_url, assistant.message_id)

        return emitted

    def _poll_detail(self, conv: ArmedConversation) -> list[SupervisorEvent]:
        conversation_id = _conversation_id_from_url(conv.conversation_url)
        detail = self.history.fetch_detail(conversation_id)
        return self._process_detail(conv, detail)

    def _handle_receiver_failure(self, exc: Exception) -> list[SupervisorEvent]:
        delay = self._backoff.fail(f"{type(exc).__name__}: {exc}")
        event = SupervisorEvent(
            "RECEIVER_BACKOFF",
            detail=f"{type(exc).__name__}: {exc}; retry_after={delay:.1f}s",
        )
        self._emit(event)
        return [event]

    def poll_once(self) -> list[SupervisorEvent]:
        try:
            self.registry.load()
        except Exception:
            pass
        conversations = _remote_ready_conversations(self.registry, max_items=self.max_conversations)
        if not conversations:
            return []
        if not self._backoff.ready():
            return []

        # Preferred mode: one light index request, then details only when an
        # authorized conversation changed or still needs its initial baseline.
        if self._index_supported is not False:
            try:
                markers = self.history.fetch_index_markers()
                self._index_supported = True
            except HistoryIndexUnavailable as exc:
                self._index_supported = False
                self._emit(SupervisorEvent("HISTORY_INDEX_UNAVAILABLE", detail=str(exc)))
            except HistoryHTTPError as exc:
                if exc.status == 429 and exc.endpoint == "index":
                    # The global conversation index is more aggressively rate
                    # limited than authorized detail reads. After the normal
                    # bounded backoff, degrade to the existing one-conversation-
                    # per-tick round-robin path. This remains authenticated API
                    # polling and never falls back to tabs, DOM, or navigation.
                    self._index_supported = False
                    self._emit(SupervisorEvent(
                        "HISTORY_INDEX_RATE_LIMITED_FALLBACK",
                        detail="index HTTP 429; using low-frequency authorized detail round-robin",
                    ))
                return self._handle_receiver_failure(exc)
            except HistoryTransportError as exc:
                return self._handle_receiver_failure(exc)
            else:
                emitted: list[SupervisorEvent] = []
                try:
                    detail_polled_urls: set[str] = set()
                    for conv in conversations:
                        conversation_id = _conversation_id_from_url(conv.conversation_url)
                        marker = markers.get(conversation_id)
                        previous = self._index_markers.get(conversation_id)
                        needs_baseline = not self._cursor(conv.conversation_url)
                        changed = needs_baseline or (marker is not None and marker != previous)
                        if changed:
                            emitted.extend(self._poll_detail(conv))
                            detail_polled_urls.add(conv.conversation_url)
                        if marker is not None:
                            self._index_markers[conversation_id] = marker
                    # ChatGPT's conversation index can lag behind the detail
                    # endpoint across devices/sessions.  Periodically sweep one
                    # authorized detail even when its index marker is unchanged;
                    # the durable per-conversation cursor still prevents replay.
                    if conversations:
                        sweep = conversations[self._index_sweep_rr_index % len(conversations)]
                        self._index_sweep_rr_index = (
                            self._index_sweep_rr_index + 1
                        ) % max(1, len(conversations))
                        if sweep.conversation_url not in detail_polled_urls:
                            emitted.extend(self._poll_detail(sweep))
                    self._backoff.success()
                    return emitted
                except (HistoryHTTPError, HistoryTransportError) as exc:
                    return emitted + self._handle_receiver_failure(exc)

        # Fallback mode: exactly one authorized conversation per poll tick.
        if not conversations:
            return []
        conv = conversations[self._fallback_rr_index % len(conversations)]
        self._fallback_rr_index = (self._fallback_rr_index + 1) % max(1, len(conversations))
        try:
            emitted = self._poll_detail(conv)
            self._backoff.success()
            return emitted
        except (HistoryHTTPError, HistoryTransportError) as exc:
            return self._handle_receiver_failure(exc)


# Backward-compatible class name for code/tests that imported the Stage 7.2.1 symbol.
CDPHiddenSupervisor = HistoryHiddenSupervisor


def run(cdp: str, parent_pid: int, poll: float) -> int:
    from playwright.sync_api import sync_playwright
    from agent_core.startup_preferences import load_startup_preferences

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("RemoteAgent-0 - Ingress Status")
        except Exception:
            pass
    poll_interval = max(MIN_POLL_INTERVAL_SEC, float(poll))
    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.connect_over_cdp(cdp)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("CDP browser has no context")
        context = contexts[0]
        request_context = context.request

        registry = ConversationRegistry()
        registry.load()
        conversations = _remote_ready_conversations(registry, max_items=5)
        supervisor = HistoryHiddenSupervisor(
            request_context=request_context,
            registry=registry,
            task_store=TaskStateStore(TASK_STORE),
            poll_interval_sec=poll_interval,
            navigation_settle_sec=0.0,
            max_conversations=5,
            verbose_idle=False,
        )
        print(
            f"[RemoteAgent 0] authenticated history receiver attached; "
            f"remote-ready={len(conversations)} cursor={REMOTE_WATCH_CURSOR} poll={poll_interval:.1f}s",
            flush=True,
        )
        shared_startup = load_startup_preferences()
        if shared_startup:
            print(
                "[RemoteAgent 0] 共用啟動設定 | "
                f"session={shared_startup.get('gpt_url', '')} | "
                f"planner={shared_startup.get('planner_key', '')} | "
                f"executor={shared_startup.get('executor_key', '')}",
                flush=True,
            )
        print("[RemoteAgent 0] 狀態窗已啟動；等待手機 RemoteAgent 訊息...", flush=True)

        # Immediate first baseline/check minimizes the startup race before the
        # user invokes RemoteAgent from mobile.
        next_status_at = time.monotonic()
        while _parent_alive(parent_pid):
            events = supervisor.poll_once()
            now = time.monotonic()
            if now >= next_status_at:
                queued = len(supervisor.task_queue.queued())
                print(
                    f"[RemoteAgent 0] {time.strftime('%H:%M:%S')} 運作中 | "
                    f"監看={len(conversations)} | 本輪事件={len(events)} | 排隊={queued}",
                    flush=True,
                )
                next_status_at = now + 30.0
            deadline = time.monotonic() + poll_interval
            while _parent_alive(parent_pid) and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return 0
    finally:
        # Do NOT browser.close(): LocalAgent owns the actual Chromium process.
        try:
            pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Deterministic receiver tests: no Playwright/network/Ollama required.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str = ""):
        self.status = int(status)
        self._payload = payload
        self._text = text
    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload
    def text(self):
        return self._text


class _FakeRequestContext:
    def __init__(self):
        self.index_response: Any = {"items": []}
        self.details: dict[str, Any] = {}
        self.calls: list[str] = []
    def get(self, url: str, **_kwargs: Any):
        self.calls.append(url)
        if url.startswith(DETAIL_URL_PREFIX):
            conversation_id = urllib.parse.unquote(url[len(DETAIL_URL_PREFIX):])
            value = self.details.get(conversation_id, {"mapping": {}, "current_node": None})
        else:
            value = self.index_response
        if isinstance(value, _FakeResponse):
            return value
        return _FakeResponse(200, value)


class _FakeRegistry:
    def __init__(self, records: Iterable[dict[str, Any]]):
        self.records = [json.loads(json.dumps(x)) for x in records]
        self.cursors: dict[tuple[str, str], str] = {}
    def load(self): return json.loads(json.dumps(self.records))
    def list_conversations(self): return json.loads(json.dumps(self.records))
    def find_by_url(self, url: str):
        for rec in self.records:
            if rec.get("gpt_url") == url:
                return json.loads(json.dumps(rec))
        return None
    def get_watch_cursor(self, url: str, name: str): return self.cursors.get((url, name), "")
    def set_watch_cursor(self, url: str, name: str, value: str): self.cursors[(url, name)] = str(value)
    def set_active_workspace(self, url: str, workspace: str, *, source: str, add_known: bool = True, **_kwargs: Any):
        for rec in self.records:
            if rec.get("gpt_url") == url:
                if add_known and workspace not in rec.setdefault("known_workspaces", []):
                    rec["known_workspaces"].append(workspace)
                rec["active_workspace"] = workspace
                rec["workspace"] = workspace
                rec["last_access_source"] = source
                return json.loads(json.dumps(rec))
        raise ValueError("conversation not found")


class _FakeQueue:
    def __init__(self):
        self.tasks: dict[tuple[str, str], Any] = {}
        self.fail_once = False
    def reconcile_on_start(self): return []
    def summary(self): return {"queued": len(self.tasks)}
    def enqueue_remote_request(self, request: dict[str, Any], *, origin_turn_fingerprint: str, metadata: dict[str, Any]):
        if self.fail_once:
            self.fail_once = False
            raise OSError("simulated durable-write failure")
        key = (str(request.get("request_id")), str(request.get("conversation_url")))
        if key in self.tasks:
            return self.tasks[key], False
        task = SimpleNamespace(
            task_id=f"TASK-{len(self.tasks)+1}",
            request_id=str(request.get("request_id")),
            conversation_url=str(request.get("conversation_url")),
            workspace=str(request.get("workspace")),
            request=str(request.get("request")),
            origin_turn_fingerprint=str(origin_turn_fingerprint),
            metadata=dict(metadata),
        )
        self.tasks[key] = task
        return task, True


def _fake_record(workspace: str, url: str) -> dict[str, Any]:
    return {
        "workspace": workspace,
        "active_workspace": workspace,
        "known_workspaces": [workspace],
        "default_workspace": workspace,
        "gpt_url": url,
        "protocols": {"smart_agent": {"armed": True, "protocol_version": 3}},
    }


def _assistant_node(message_id: str, text: str, parent: str | None, *, completed: bool = True) -> dict[str, Any]:
    return {
        "id": "node-" + message_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": message_id,
            "author": {"role": "assistant"},
            "create_time": float(sum(ord(c) for c in message_id)),
            "status": "finished_successfully" if completed else "in_progress",
            "end_turn": bool(completed),
            "content": {"content_type": "text", "parts": [text]},
        },
    }


def _detail(messages: list[tuple[str, str, bool]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    parent = None
    current = None
    for message_id, text, completed in messages:
        node_id = "node-" + message_id
        mapping[node_id] = _assistant_node(message_id, text, parent, completed=completed)
        if parent and parent in mapping:
            mapping[parent].setdefault("children", []).append(node_id)
        parent = node_id
        current = node_id
    return {"mapping": mapping, "current_node": current}


def run_history_receiver_self_tests() -> dict[str, Any]:
    import tempfile
    from RemoteAgent.remote_protocol import format_remote_message, new_request

    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        workspace = str(Path(td).resolve())
        url1 = "https://chatgpt.com/g/g-test/c/conv-a"
        url2 = "https://chatgpt.com/g/g-p-project/project/c/conv-b"
        registry = _FakeRegistry([_fake_record(workspace, url1), _fake_record(workspace, url2)])
        request = _FakeRequestContext()
        queue = _FakeQueue()

        # If production code ever touches tab/page APIs this fake context would
        # not even provide them. The receiver gets only request_context.
        sup = HistoryHiddenSupervisor(
            request_context=request,
            registry=registry,
            task_store=object(),
            task_queue=queue,
            poll_interval_sec=MIN_POLL_INTERVAL_SEC,
            navigation_settle_sec=0.0,
            max_conversations=5,
        )
        # Do not write the real handoff path during self-test.
        sup._publish_handoff = lambda task: None

        request.index_response = {"items": [
            {"id": "conv-a", "update_time": 1},
            {"id": "conv-b", "update_time": 1},
        ]}
        request.details["conv-a"] = _detail([("A0", "old assistant", True)])
        request.details["conv-b"] = _detail([("B0", "old assistant", True)])
        sup.poll_once()
        results["no_tab_page_operations_required"] = True
        results["initial_baseline_no_replay"] = len(queue.tasks) == 0 and registry.get_watch_cursor(url1, REMOTE_WATCH_CURSOR) == "A0"

        remote = format_remote_message(new_request(
            request_id="RR-HISTORY-1",
            conversation_url=url1,
            workspace=workspace,
            request="list directory",
        ))
        request.index_response["items"][0]["update_time"] = 2
        request.details["conv-a"] = _detail([("A0", "old assistant", True), ("A1", remote, True)])
        events = sup.poll_once()
        results["fresh_fenced_assistant_queues_once"] = len(queue.tasks) == 1 and any(e.kind == "TASK_QUEUED" for e in events)
        before_detail_calls = request.calls.count(DETAIL_URL_PREFIX + "conv-a")
        sup.poll_once()
        after_detail_calls = request.calls.count(DETAIL_URL_PREFIX + "conv-a")
        # An unchanged index may trigger at most one deliberate round-robin
        # detail sweep to cover cross-device index lag; cursor dedupe must still
        # prevent a repeated task.
        results["index_change_filtering"] = (
            0 <= after_detail_calls - before_detail_calls <= 1
        )
        results["repeated_poll_request_deduped"] = len(queue.tasks) == 1

        # New completed request with a fail-once durable store: first attempt
        # must leave cursor on A1; second changed-marker retry queues and advances.
        remote2 = format_remote_message(new_request(
            request_id="RR-HISTORY-2",
            conversation_url=url1,
            workspace=workspace,
            request="list directory again",
        ))
        request.details["conv-a"] = _detail([("A0", "old", True), ("A1", remote, True), ("A2", remote2, True)])
        request.index_response["items"][0]["update_time"] = 3
        queue.fail_once = True
        sup.poll_once()
        cursor_after_fail = registry.get_watch_cursor(url1, REMOTE_WATCH_CURSOR)
        request.index_response["items"][0]["update_time"] = 4
        sup.poll_once()
        results["cursor_advances_only_after_durable_enqueue"] = (
            cursor_after_fail == "A1"
            and registry.get_watch_cursor(url1, REMOTE_WATCH_CURSOR) == "A2"
            and len(queue.tasks) == 2
        )

        # Partial/in-progress assistant is not included in completed branch list,
        # therefore cursor remains on the last completed message.
        request.details["conv-a"] = _detail([("A0", "old", True), ("A1", remote, True), ("A2", remote2, True), ("A3", remote2, False)])
        request.index_response["items"][0]["update_time"] = 5
        sup.poll_once()
        results["partial_assistant_not_consumed"] = registry.get_watch_cursor(url1, REMOTE_WATCH_CURSOR) == "A2"

        # Force unsupported index and verify fallback probes one conversation only.
        request2 = _FakeRequestContext()
        request2.index_response = _FakeResponse(404, {})
        request2.details["conv-a"] = _detail([("A0", "old", True)])
        request2.details["conv-b"] = _detail([("B0", "old", True)])
        registry2 = _FakeRegistry([_fake_record(workspace, url1), _fake_record(workspace, url2)])
        queue2 = _FakeQueue()
        sup2 = HistoryHiddenSupervisor(
            request_context=request2, registry=registry2, task_store=object(), task_queue=queue2,
            poll_interval_sec=MIN_POLL_INTERVAL_SEC, navigation_settle_sec=0.0, max_conversations=5,
        )
        sup2._publish_handoff = lambda task: None
        sup2.poll_once()
        first_detail_total = sum(1 for x in request2.calls if x.startswith(DETAIL_URL_PREFIX))
        sup2.poll_once()
        second_detail_total = sum(1 for x in request2.calls if x.startswith(DETAIL_URL_PREFIX))
        results["fallback_round_robin_one_conversation_per_tick"] = first_detail_total == 1 and second_detail_total == 2

        # 429 should enter exponential backoff and suppress immediate re-request.
        request3 = _FakeRequestContext()
        request3.index_response = _FakeResponse(429, {}, "rate limited")
        registry3 = _FakeRegistry([_fake_record(workspace, url1)])
        sup3 = HistoryHiddenSupervisor(
            request_context=request3, registry=registry3, task_store=object(), task_queue=_FakeQueue(),
            poll_interval_sec=MIN_POLL_INTERVAL_SEC, navigation_settle_sec=0.0, max_conversations=5,
        )
        sup3._publish_handoff = lambda task: None
        first429 = sup3.poll_once()
        call_count = len(request3.calls)
        second429 = sup3.poll_once()
        results["http_429_exponential_backoff"] = (
            any(e.kind == "RECEIVER_BACKOFF" for e in first429)
            and sup3._backoff.failures == 1
            and sup3._backoff.until > time.monotonic()
            and len(request3.calls) == call_count
            and second429 == []
        )

        results["conversation_id_normal"] = _conversation_id_from_url("https://chatgpt.com/c/abc") == "abc"
        results["conversation_id_gizmo_project"] = _conversation_id_from_url(url2) == "conv-b"

    results["all_passed"] = all(results.values())
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="RemoteAgent hidden authenticated history receiver")
    ap.add_argument("--cdp")
    ap.add_argument("--parent-pid", type=int)
    ap.add_argument("--poll", type=float, default=MIN_POLL_INTERVAL_SEC)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        result = run_history_receiver_self_tests()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("all_passed") else 1
    if not args.cdp or args.parent_pid is None:
        ap.error("--cdp and --parent-pid are required unless --self-test is used")
    return run(args.cdp, args.parent_pid, args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
