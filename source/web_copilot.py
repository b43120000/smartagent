#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import re
import time
import urllib.request
from pathlib import Path
from agent_core.conversation_identity import conversation_id as _shared_conversation_id
from agent_core.web_provider_routing import provider_for_url
from agent_core.paths import (
    agent_host_state_path,
    dispatcher_state_path,
    remote_tasks_path,
    remote_transport_sessions_path,
)


def _conversation_id(url: str) -> str:
    return _shared_conversation_id(url) or "(unknown)"


def _resolve_webcopilot_target(explicit_url: str) -> tuple[str, str]:
    """Resolve WebCopilot from the startup selection shared with LocalAgent."""
    import smart_agent as sa

    saved = sa._validated_saved_startup()
    workspace = str(saved.get("workspace", "") or "").strip()
    target_url = str(explicit_url or "").strip() or str(
        saved.get("gpt_url", "") or ""
    ).strip()
    if not workspace or not target_url:
        raise RuntimeError(
            "webcopilot_target_not_configured: run Edit_workspace.bat first"
        )
    if _conversation_id(target_url) == "(unknown)":
        raise RuntimeError("webcopilot_requires_linked_conversation")
    return workspace, target_url


def _remote_dispatcher_live(root: str | Path) -> bool:
    path = dispatcher_state_path(root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    pid = int(state.get("pid", 0) or 0)
    heartbeat = float(state.get("heartbeat_at", 0) or 0)
    return bool(
        str(state.get("status", "")).upper() == "RUNNING"
        and heartbeat > 0
        and time.time() - heartbeat <= 15.0
        and _process_alive(pid)
    )


def _ensure_remote_dispatcher(root: str | Path) -> bool:
    root=Path(root)
    if _remote_dispatcher_live(root):
        return False
    env=os.environ.copy()
    flags=getattr(subprocess,"CREATE_NEW_CONSOLE",0) if os.name=="nt" else 0
    subprocess.Popen(
        [sys.executable,"-m","agent_core.host_supervisor","--dispatcher-only"],
        cwd=str(root),env=env,stdin=subprocess.DEVNULL,creationflags=flags,
    )
    return True


def _process_alive(pid: int) -> bool:
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _publish_webcopilot_host_state(
    root: str | Path,
    *,
    workspace: str,
    conversation_url: str,
    cdp_endpoint: str,
) -> bool:
    """Expose WebCopilot's authenticated browser as Agent0's CDP host.

    This avoids cold-starting a second full LocalAgent (and its CMD/browser)
    merely to export the same authenticated browser state to Agent1_n.
    """
    root = Path(root)
    path = agent_host_state_path(root)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    current_pid = int(current.get("host_pid", 0) or 0)
    current_owner = str(current.get("browser_owner", "") or "")
    if _process_alive(current_pid) and (
        current_pid != os.getpid()
        or current_owner not in {"", "WEBGPT_COPILOT"}
    ):
        return False
    payload = {
        "version": 1,
        "host_pid": os.getpid(),
        "status": "ready",
        "workspace": str(Path(workspace).resolve()),
        "conversation_url": str(conversation_url),
        "updated_at": time.time(),
        "cdp_endpoint": str(cdp_endpoint),
        "browser_owner": "WEBGPT_COPILOT",
        "remote_autostart": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return True


def _release_webcopilot_host_state(root: str | Path) -> bool:
    """Remove only the host-state publication owned by this process."""
    path = agent_host_state_path(root)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if (
        int(current.get("host_pid", 0) or 0) != os.getpid()
        or str(current.get("browser_owner", "")) != "WEBGPT_COPILOT"
    ):
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cdp_available(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _reconcile_canonical_page(context, target_url: str, current_page=None):
    """Keep one marked page for a conversation and recover a replaced observer page."""
    target_id = _conversation_id(target_url)
    if context is None or target_id == "(unknown)":
        return current_page
    matches = [
        candidate
        for candidate in list(getattr(context, "pages", []) or [])
        if _conversation_id(str(getattr(candidate, "url", "") or "")) == target_id
    ]
    marker = "SMARTAGENT_CANONICAL_CONVERSATION:" + target_id

    def rank(candidate):
        try:
            if str(candidate.evaluate("() => window.name") or "") == marker:
                return 0
        except Exception:
            pass
        return 1 if candidate is current_page else 2

    if matches:
        matches.sort(key=rank)
        canonical = matches[0]
    else:
        canonical = context.new_page()
        canonical.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    try:
        canonical.evaluate("value => { window.name = value; }", marker)
    except Exception:
        pass
    for duplicate in matches:
        if duplicate is canonical:
            continue
        try:
            duplicate.close(run_before_unload=False)
        except Exception:
            pass
    return canonical


def _attach_existing_browser(endpoint: str, target_url: str):
    from playwright.sync_api import sync_playwright

    print(f"[WebGPT Copilot][TRACE] existing CDP detected: {endpoint}", flush=True)
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(endpoint)
    if not browser.contexts:
        raise RuntimeError("Existing CDP browser has no BrowserContext")
    context = browser.contexts[0]
    target_id = _conversation_id(target_url)
    matches = []
    for candidate in context.pages:
        print(f"[WebGPT Copilot][TRACE] existing tab: {candidate.url}", flush=True)
        if target_id != "(unknown)" and _conversation_id(candidate.url) == target_id:
            matches.append(candidate)
    canonical_marker = "SMARTAGENT_CANONICAL_CONVERSATION:" + target_id
    def canonical_rank(candidate):
        try:
            return 0 if str(candidate.evaluate("() => window.name") or "") == canonical_marker else 1
        except Exception:
            return 1
    matches.sort(key=canonical_rank)
    page = matches[0] if matches else None
    for duplicate in matches[1:]:
        try:
            duplicate.close(run_before_unload=False)
        except Exception:
            pass
    if page is None:
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    elif _conversation_id(page.url) != target_id:
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page = _reconcile_canonical_page(context, target_url, page)
    return pw, browser, context, page


def _install_shared_scraper(page, context, attached_pw, service: str):
    from agent_core import web_runtime

    manager = web_runtime.get_manager()
    scraper = web_runtime.WebLLMScraper(service=service)
    scraper._pw = attached_pw
    scraper._browser = context
    scraper._page = page
    scraper._attached_over_cdp = True
    scraper._owns_attached_page = False
    manager._scrapers[service] = scraper
    print("[WebGPT Copilot] SmartAgent adopted the same ChatGPT page/session.", flush=True)
    return scraper


def _build_shared_agent(workspace: str):
    import smart_agent as sa

    saved = sa._validated_saved_startup()
    planner_key = str(saved.get("planner_key", "") or "")
    executor_key = str(saved.get("executor_key", "") or "")
    operator_key = str(saved.get("operator_key", "") or "")
    if planner_key not in sa.MODELS or sa.MODELS[planner_key].get("provider") != "web_scraper":
        planner_key = "web_chatgpt"
    if executor_key not in sa.MODELS:
        executor_key = "cloud_gemma"
    if operator_key not in sa.MODELS:
        operator_key = "cloud_gptoss"
    tier = sa.MODELS[planner_key]["tier"]
    strategy = {
        "planner": planner_key,
        "executor": executor_key,
        "operator": operator_key,
        "label": f"Tier {tier} [WEBCOPILOT_SHARED_PAGE]",
        "use_web_scraper": True,
    }
    sa.TIER_STRATEGY[tier] = strategy
    agent = sa.SmartAgent(tier, strategy)
    result = agent.set_workspace_root(workspace)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "workspace setup failed"))
    return agent


def _emit_webcopilot_event(event: str, message: str, task_id: str = ""):
    print(message, flush=True)
    print("[WEBCOPILOT_EVENT] " + json.dumps({"event": event, "message": message, "task_id": task_id}, ensure_ascii=False), flush=True)


def _compact_task_value(value, limit: int = 500) -> str:
    if value in (None, "", {}, []):
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    value = str(value).strip()
    return value if len(value) <= limit else value[:limit] + "..."


def _seed_webcopilot_tasks(task_queue, conversation_url: str) -> dict[str, str]:
    """Restore non-terminal WebCopilot tasks after a console restart."""
    with task_queue.store.process_lock():
        task_queue.store.load()
        tasks = [task.as_dict() for task in task_queue.store.all()]
    tracked = {}
    for task in tasks:
        route = dict(task.get("reply_route") or {})
        if (
            str(route.get("transport", "")).upper() == "WEBGPT_COPILOT"
            and str(task.get("conversation_url", "")) == str(conversation_url)
            and str(task.get("state", "")) in {"QUEUED", "RUNNING"}
        ):
            tracked[str(task["task_id"])] = ""
    return tracked


def _poll_webcopilot_task_states(task_queue, tracked: dict[str, str], page) -> None:
    """Mirror durable task transitions to the visible WebCopilot console."""
    if not tracked:
        return
    with task_queue.store.process_lock():
        task_queue.store.load()
        snapshots = {
            task_id: task_queue.store.get(task_id).as_dict()
            if task_queue.store.get(task_id) is not None else None
            for task_id in list(tracked)
        }
    event_for_state = {
        "QUEUED": "TASK_ACCEPTED",
        "RUNNING": "TASK_STARTED",
        "COMPLETED": "TASK_COMPLETED",
        "FAILED": "TASK_FAILED",
        "INTERRUPTED": "TASK_INTERRUPTED",
        "CANCELLED": "TASK_CANCELLED",
    }
    terminal = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
    for task_id, task in snapshots.items():
        if task is None:
            _emit_webcopilot_event(
                "ERROR", f"[WebGPT Copilot] task disappeared: {task_id}", task_id,
            )
            tracked.pop(task_id, None)
            continue
        state = str(task.get("state", "") or "")
        if tracked.get(task_id) == state:
            continue
        tracked[task_id] = state
        event = event_for_state.get(state, "TASK_STATUS")
        detail = ""
        if state == "COMPLETED":
            detail = _compact_task_value((task.get("result_ledger") or {}).get("final"))
        elif state in {"FAILED", "INTERRUPTED", "CANCELLED"}:
            detail = _compact_task_value(task.get("error"))
        message = f"[WebGPT Copilot] {event} task_id={task_id}"
        if detail:
            message += f" detail={detail}"
        _emit_webcopilot_event(event, message, task_id)
        if state == "RUNNING":
            _present_webcopilot_notice(page, f"Agent1 worker started [{task_id}]", "info")
        elif state == "COMPLETED":
            _present_webcopilot_notice(page, f"Task completed [{task_id}]", "done")
        elif state in terminal:
            _present_webcopilot_notice(page, f"Task {state.lower()} [{task_id}]: {detail}", "error")
        if state in terminal:
            tracked.pop(task_id, None)


def _present_webcopilot_notice(page, message: str, kind: str = "info") -> bool:
    try:
        page.evaluate("""([message, kind]) => {
            let el = document.querySelector('[data-smartagent-webcopilot-notice]');
            if (!el) {
                el = document.createElement('div');
                el.setAttribute('data-smartagent-webcopilot-notice', '1');
                Object.assign(el.style, {position:'fixed',right:'20px',bottom:'20px',zIndex:'2147483647',maxWidth:'520px',padding:'12px 16px',borderRadius:'10px',fontFamily:'system-ui,sans-serif',fontSize:'14px',boxShadow:'0 4px 18px rgba(0,0,0,.22)',whiteSpace:'pre-wrap'});
                document.body.appendChild(el);
            }
            el.textContent = message;
            el.dataset.kind = kind;
            el.style.background = kind === 'error' ? '#5b1a1a' : kind === 'done' ? '#123d2c' : '#1f2937';
            el.style.color = '#fff';
        }""", [str(message), str(kind)])
        return True
    except Exception:
        return False


def _extract_webcopilot_request(text: str) -> str:
    from agent_core.current_conversation_adapter import extract_webcopilot_request
    return extract_webcopilot_request(text)



def _observed_user_turn_texts(page) -> list[str]:
    from agent_core.web_ui import create_web_ui_for_page
    return [
        turn.text
        for turn in create_web_ui_for_page(page).observation_turns("user")
        if turn.text
    ]

def _baseline_existing_messages(page) -> set[str]:
    try:
        seen = set(_observed_user_turn_texts(page))
    except Exception as exc:
        print(f"[WebGPT Copilot][WARN] startup baseline failed: {type(exc).__name__}: {exc}", flush=True)
        seen = set()
    print(f"[WebGPT Copilot] Startup baseline: discarded {len(seen)} existing user messages.", flush=True)
    return seen

def scan_webgpt(page, seen_messages: set[str]):
    try:
        messages = _observed_user_turn_texts(page)
    except Exception as exc:
        print(f"[WebGPT Copilot][ERROR] web_ui scan failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    for text in messages:
        if not text or not re.match(r"(?is)^\s*webcopilot\b", text):
            continue
        if text in seen_messages:
            continue
        seen_messages.add(text)
        print(f"[WebGPT Copilot][DOM] webcopilot message: {text}", flush=True)
        return text
    return None

def main():
    print("[WebCopilot] STARTING", flush=True)
    parser = argparse.ArgumentParser(description="WebGPT Copilot")
    parser.add_argument("--webgpt-url", default="")
    args = parser.parse_args()
    try:
        workspace, target_url = _resolve_webcopilot_target(args.webgpt_url)
    except RuntimeError as exc:
        print(f"[WebCopilot] ERROR {exc}", flush=True)
        return 2

    from agent_core import web_runtime

    service = provider_for_url(target_url)
    cdp_endpoint = web_runtime.CHATGPT_CDP_ENDPOINT if service == "chatgpt" else ""
    attached_pw = None
    published_host_state = False
    try:
        if service == "chatgpt" and _cdp_available(cdp_endpoint):
            print("[WebGPT Copilot] Fast path: attach existing LocalAgent Chromium/CDP; no profile relaunch.", flush=True)
            attached_pw, _browser, context, page = _attach_existing_browser(cdp_endpoint, target_url)
            scraper = _install_shared_scraper(page, context, attached_pw, service)
            browser_mode = "REUSE_EXISTING_CDP_SHARED_PAGE"
            title = page.title() or "(title unavailable)"
        else:
            print("[WebGPT Copilot] Slow path: no LocalAgent CDP exists, so WebCopilot must cold-start its own persistent browser/profile.", flush=True)
            web_runtime.SERVICE_CONFIG[service]["url"] = target_url
            scraper = web_runtime.get_manager().get_or_create(service)
            scraper.navigate_to_conversation(target_url)
            page = getattr(scraper, "_page", None)
            if page is None:
                raise RuntimeError("WebCopilot browser page is unavailable")
            context = getattr(scraper, "_browser", None)
            browser_mode = "OWN_BROWSER_SHARED_PAGE"
            title = scraper.get_conversation_display_name() or "(title unavailable)"

        # Publish in both cold-start and attach-existing modes.  A Chromium
        # process can outlive the previous WebCopilot process briefly, making
        # a restart take the attach path even though no LocalAgent owns it.
        # The helper refuses to overwrite any live host process.
        published_host_state = _publish_webcopilot_host_state(
            Path(__file__).resolve().parent,
            workspace=workspace,
            conversation_url=target_url,
            cdp_endpoint=cdp_endpoint,
        )
        _emit_webcopilot_event(
            "BROWSER_HOST_READY" if published_host_state else "BROWSER_HOST_REUSED",
            "[WebGPT Copilot] authenticated browser endpoint is ready for Agent0; "
            f"published={published_host_state} cdp={cdp_endpoint}",
        )
        agent = _build_shared_agent(workspace)
        # WebCopilot is only the ingress listener.  Performing SESSION_ATTACH
        # here writes an Agent control turn into the watched conversation
        # before the observer starts and can hide a just-submitted human turn
        # on a same-length ChatGPT branch.  The request-scoped Agent1 worker
        # owns protocol bootstrap/attach on its isolated execution page.
        print("[WebGPT Copilot] Protocol Session: DEFERRED_TO_AGENT1", flush=True)

        print("[WebCopilot] READY", flush=True)
        print(f"[WebCopilot] Browser Mode    : {browser_mode}", flush=True)
        print(f"[WebCopilot] Conversation ID : {_conversation_id(target_url)}", flush=True)
        print(f"[WebCopilot] Title           : {title}", flush=True)
        print(f"[WebCopilot] Workspace       : {workspace}", flush=True)
        print("[WebCopilot] WAITING_SIGNAL", flush=True)

        # Startup/restart observation is owned by WebGPTObserver. The durable
        # conversation cursor identifies user-turn position, not message text,
        # so repeated identical requests remain distinct turns without replay.
        from agent_core.conversation_registry import ConversationRegistry
        from agent_core.webgpt_observer import WebGPTObserver
        registry = ConversationRegistry()
        registry.load()
        registry.upsert_binding(workspace, target_url)
        observer = WebGPTObserver(page, registry=registry, gpt_url=target_url)
        baseline_count = observer.baseline()
        print(f"[WebGPT Copilot] Startup cursor: {baseline_count} user turns.", flush=True)
        from agent_core.task_state import RemoteTaskQueue, TaskStateStore
        from agent_core.agent_gateway import AgentIngressGateway
        from agent_core.transport_sessions import TransportSessionRouter
        task_queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path()))
        ingress_gateway = AgentIngressGateway(
            task_queue=task_queue,
            session_router=TransportSessionRouter(remote_transport_sessions_path()),
        )
        tracked_tasks = _seed_webcopilot_tasks(task_queue, target_url)
        if tracked_tasks:
            recovery_started = _ensure_remote_dispatcher(Path(__file__).resolve().parent)
            _emit_webcopilot_event(
                "RECOVERY",
                f"[WebGPT Copilot] restoring {len(tracked_tasks)} active task(s) "
                f"from durable state; supervisor={'started' if recovery_started else 'reused'}",
            )
        while True:
            try:
                canonical_page = _reconcile_canonical_page(
                    context, target_url, page
                )
                if canonical_page is not page:
                    page = canonical_page
                    observer.page = page
                    scraper._page = page
                _poll_webcopilot_task_states(task_queue, tracked_tasks, page)
                # This process is explicitly bound to one WebCopilot
                # conversation, so every new non-empty user turn is an ingress
                # request.  The historical ``webcopilot`` prefix remains
                # accepted but is no longer required in this dedicated UI.
                observed = observer.poll_new_user_turn(prefix=None)
                if observed:
                    turn_index, raw_request = observed
                    try:
                        from agent_core.current_conversation_adapter import build_webcopilot_message
                        inbound = build_webcopilot_message(
                            raw_request,
                            conversation_url=target_url,
                            turn_index=turn_index,
                            conversation_title=title,
                            allow_plain=True,
                        )
                    except ValueError:
                        observer.acknowledge_user_turn(turn_index, raw_request)
                        continue
                    _emit_webcopilot_event(
                        "REQUEST_DETECTED",
                        f"[WebGPT Copilot] REQUEST_DETECTED turn={turn_index}: {inbound.text}",
                    )
                    accepted = ingress_gateway.accept(inbound, workspace=workspace)
                    task, created = accepted.task, accepted.created
                    observer.acknowledge_user_turn(turn_index, raw_request)
                    if created:
                        _present_webcopilot_notice(page, "LocalAgent 已收到任務，開始執行：" + inbound.text[:240], "info")
                    event = "TASK_ACCEPTED" if created else "TASK_DEDUPLICATED"
                    _emit_webcopilot_event(
                        event,
                        f"[WebGPT Copilot] {event} task_id={task.task_id} "
                        f"session={accepted.session_id}: {inbound.text}",
                        task.task_id,
                    )
                    tracked_tasks[task.task_id] = str(task.state)
                    if created:
                        started=_ensure_remote_dispatcher(Path(__file__).resolve().parent)
                        _present_webcopilot_notice(page, f"Queued for Agent0 / Agent1_n [{task.task_id}]", "info")
                        _emit_webcopilot_event(
                            "DISPATCHER_READY",
                            f"[WebGPT Copilot] queued for Agent0 worker: {task.task_id}; "
                            f"supervisor={'started' if started else 'reused'}",
                            task.task_id,
                        )
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n[WebGPT Copilot] stopped", flush=True)
                break
            except Exception as exc:
                print(f"[WebGPT Copilot][ERROR] {type(exc).__name__}: {exc}", flush=True)
                time.sleep(1)
    finally:
        if published_host_state:
            _release_webcopilot_host_state(Path(__file__).resolve().parent)
        if attached_pw is not None:
            attached_pw.stop()


if __name__ == "__main__":
    main()
