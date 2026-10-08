#!/usr/bin/env python3
from __future__ import annotations

"""One-shot Telegram -> WebAgent Direct execution worker.

The Telegram receiver and durable queue remain transport owners.  This worker
only owns one request-scoped WebGPT protocol loop and writes the terminal result
back to the transport-neutral RemoteEvent outbox.
"""

import argparse
import os
import re
import threading
import time
from pathlib import Path

from agent_core.conversation_registry import ConversationRegistry
from agent_core.remote_events import RemoteEventStore
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.paths import (install_root, remote_events_path, remote_execution_page_lock_path, remote_control_output_root, remote_runtime_log_path, remote_skill_context_root, remote_tasks_path, remote_webagent_state_root, remote_workers_root)
from agent_core.task_transport import resolve_task_execution_chatgpt_url
from agent_core.plan_ledger import PlanLedger, publish_completed_plan
from RemoteAgent.telegram_artifacts import prepare_telegram_completion


ROOT = Path(__file__).resolve().parents[1]


class RemoteTaskCancelled(RuntimeError):
    pass


def _telegram_get_file_fast_result(request: str, workspace: str):
    match = re.fullmatch(r"\s*/get\s+(.+?)\s*", str(request or ""), re.IGNORECASE)
    if not match:
        return "", None
    raw = match.group(1).strip().strip("`\"'")
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", "\\")):
        raise ValueError("/get only accepts workspace-relative paths")
    root = Path(workspace).resolve()
    target = (root / raw).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("/get path escapes workspace") from exc
    if not target.is_file():
        raise FileNotFoundError(f"/get file not found: {relative}")
    kind = "photo" if target.suffix.lower() in {".jpg", ".jpeg", ".png"} else "document"
    artifact = {"path": str(target), "kind": kind, "name": target.name, "workspace": str(root)}
    return f"Prepared Telegram file: {relative.as_posix()}", artifact


def _attachment_paths(task) -> list[str]:
    paths: list[str] = []
    for item in list((task.metadata or {}).get("attachments") or []):
        if not isinstance(item, dict):
            continue
        value = str(item.get("local_path", "") or "").strip()
        if value:
            paths.append(value)
    return paths


def _event_sink(runtime_log: RemoteRuntimeLog, task, *, root: Path, events: RemoteEventStore):
    def emit(event: str, **fields) -> None:
        detail = dict(fields)
        # WebAgent browser events legitimately carry their own logical stage
        # (for example "bootstrap").  Keep it as detail instead of colliding
        # with RemoteRuntimeLog's top-level stage keyword.
        if "stage" in detail:
            detail["operation_stage"] = detail.pop("stage")
        detail.setdefault("task_id", task.task_id)
        detail.setdefault("request_id", task.request_id)
        runtime_log.write(
            "STATUS",
            component="telegram_webagent_worker",
            stage=str(event),
            **detail,
        )
        if (
            event == "task_progress_updated"
            and str((task.metadata or {}).get("transport", "")).upper() == "TELEGRAM"
        ):
            try:
                from agent_core.task_progress import (
                    format_telegram_status_view,
                    read_progress,
                )
                ledger = read_progress(task.task_id, root=root)
                if ledger is None:
                    return
                progress_event = events.emit(
                    "TASK_PROGRESS", task, status="RUNNING",
                    payload={"summary": format_telegram_status_view(ledger)},
                )
                runtime_log.write(
                    "STATUS",
                    component="telegram_webagent_worker",
                    stage="TASK_PROGRESS_QUEUED",
                    task_id=task.task_id,
                    request_id=task.request_id,
                    event_id=progress_event.event_id,
                )
            except Exception as exc:
                # A Telegram status notification is observability only; never
                # interrupt the active model/tool loop when its outbox fails.
                runtime_log.write(
                    "ERROR",
                    component="telegram_webagent_worker",
                    stage="TASK_PROGRESS_QUEUE_FAILED",
                    task_id=task.task_id,
                    request_id=task.request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
    return emit


def preserve_completed_page(page, scraper, attached_pw, expected_url: str) -> dict:
    """Release worker ownership without closing the shared WebGPT page."""
    from agent_core.conversation_identity import conversation_id

    preserved_url = str(getattr(page, "url", "") or "")
    page_closed = bool(
        getattr(page, "closed", False)
        or (callable(getattr(page, "is_closed", None)) and page.is_closed())
    )
    if page_closed or conversation_id(preserved_url) != conversation_id(expected_url):
        raise RuntimeError(
            "remote_completion_page_not_preserved: "
            f"expected={expected_url}; actual={preserved_url}"
        )
    remaining_owners = scraper.release_conversation_owner()
    if attached_pw is not None:
        attached_pw.stop()
    return {
        "conversation_url": preserved_url,
        "remaining_owners": int(remaining_owners or 0),
    }


def run_task(*, task_id: str, token: str, cdp: str, root: Path = ROOT) -> int:
    from agent_core.protocol_manifest import require_protocol_manifest
    # ``root`` may be redirected to a temporary state directory by tests, but
    # the executable protocol source always lives under this package root.
    require_protocol_manifest(install_root())
    if cdp:
        os.environ["SMARTAGENT_CHATGPT_CDP"] = str(cdp)
    os.environ["SMARTAGENT_ATTACH_CDP"] = "1"
    os.environ["SMARTAGENT_REMOTE_WORKER_TASK"] = str(task_id)

    root = Path(root).resolve()
    worker_dir = remote_workers_root(root) / str(task_id)
    runtime_log = RemoteRuntimeLog(
        remote_runtime_log_path(root),
        mirror_console=True,
        mirror_paths=[worker_dir / "runtime.jsonl"],
        snapshot_path=worker_dir / "lifecycle.json",
    )
    queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
    events = RemoteEventStore(remote_events_path(root))
    task = queue.adopt_dispatched(task_id, token, worker_pid=os.getpid())
    protocol_version = int((task.metadata or {}).get("protocol_version", 9) or 0)
    protocol_family = str((task.metadata or {}).get("protocol_family", "SMARTAGENT_V9"))
    if protocol_version not in {8, 9}:
        raise RuntimeError("remote_task_protocol_version_mismatch")
    if protocol_family not in {"SMARTAGENT_V8", "SMARTAGENT_V9"}:
        raise RuntimeError("remote_task_protocol_family_mismatch")
    source_transport = str((task.metadata or {}).get("transport", "")).upper()
    if source_transport not in {"TELEGRAM", "LOCAL_TEST"}:
        raise RuntimeError("telegram_webagent_worker_rejects_non_telegram_task")
    from agent_core.task_progress import (
        delete_progress,
        initialize_progress,
        read_progress,
        set_runtime_state,
    )
    initialize_progress(
        task.task_id,
        request_id=task.request_id,
        goal=task.request,
        root=root,
    )

    stop_heartbeat = threading.Event()
    cancel_seen = threading.Event()
    scraper_holder = {}

    def heartbeat() -> None:
        last_heartbeat = 0.0
        while not stop_heartbeat.wait(0.5):
            try:
                if queue.cancellation_requested(task.task_id):
                    cancel_seen.set()
                    active_scraper = scraper_holder.get("scraper")
                    if active_scraper is not None:
                        try:
                            active_scraper.cancel_current_generation()
                        except Exception as exc:
                            runtime_log.write(
                                "ERROR", component="telegram_webagent_worker",
                                stage="CANCEL_WEBGPT", task_id=task.task_id,
                                request_id=task.request_id,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                    return
                now = time.monotonic()
                if now - last_heartbeat >= 5.0:
                    queue.heartbeat(task.task_id, worker_pid=os.getpid())
                    last_heartbeat = now
            except Exception as exc:
                runtime_log.write(
                    "ERROR",
                    component="telegram_webagent_worker",
                    stage="HEARTBEAT",
                    task_id=task.task_id,
                    request_id=task.request_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    threading.Thread(
        target=heartbeat,
        name=f"remote-webagent-heartbeat-{task.request_id}",
        daemon=True,
    ).start()
    events.emit("TASK_STARTED", task, status="RUNNING", payload={})
    runtime_log.write(
        "TASK_STARTED",
        component="telegram_webagent_worker",
        stage="ADOPTED",
        task_id=task.task_id,
        request_id=task.request_id,
        cdp=cdp,
    )

    attached_pw = None
    started_at = time.time()
    try:
        if queue.cancellation_requested(task.task_id):
            raise RemoteTaskCancelled("telegram_interrupt_requested")
        ingress_metadata = dict(
            (task.metadata or {}).get("ingress_metadata") or {}
        )
        selected_skill = str(
            ingress_metadata.get("selected_skill", "") or ""
        ).strip()
        skill_context = None
        skill_attachments: list[str] = []
        if selected_skill:
            from agent_core.remote_binding import active, load
            from agent_core.remote_skill_manager import RemoteSkillManager

            binding = active() or load(root)
            package = RemoteSkillManager(binding["skill_path"]).package(
                selected_skill,
                output_dir=remote_skill_context_root(root),
                request_id=task.request_id,
            )
            skill_context = package.prompt_metadata()
            skill_attachments.append(package.bundle_path)
            runtime_log.write(
                "CONNECT", component="telegram_webagent_worker",
                stage="SKILL_CONTEXT_READY", task_id=task.task_id,
                request_id=task.request_id, skill=package.name,
                skill_sha256=package.sha256, skill_files=list(package.files),
            )
        if source_transport == "TELEGRAM":
            fast_summary, fast_artifact = _telegram_get_file_fast_result(task.request, task.workspace)
            if fast_artifact is not None:
                terminal_payload = prepare_telegram_completion(
                    request=task.request,
                    summary=fast_summary,
                    workspace=task.workspace,
                    artifacts=[fast_artifact],
                )
                fast_summary = terminal_payload["summary"]
                elapsed = round(time.time() - started_at, 3)
                if queue.cancellation_requested(task.task_id):
                    raise RemoteTaskCancelled("telegram_interrupt_requested")
                completed = queue.complete(task.task_id, result={
                    "response": fast_summary, "worker_pid": os.getpid(),
                    "elapsed_sec": elapsed, "protocol_engine": "telegram_get_file_fast_path",
                    "outbound_artifacts": terminal_payload["artifacts"],
                    "artifact_rejections": terminal_payload["artifact_rejections"],
                })
                events.emit(
                    "TASK_COMPLETED", completed, status="COMPLETED",
                    payload=terminal_payload,
                )
                set_runtime_state(task.task_id, "COMPLETED", root=root)
                delete_progress(task.task_id, root=root)
                runtime_log.write("TASK_COMPLETED", component="telegram_webagent_worker",
                    stage="TELEGRAM_GET_FILE_COMPLETED", task_id=task.task_id,
                    request_id=task.request_id, elapsed_sec=elapsed)
                return 0

        registry = ConversationRegistry()
        registry.load()
        requested_execution_url = resolve_task_execution_chatgpt_url(task, registry=registry)

        # Import only after the request-scoped CDP endpoint has been published.
        from WebAgent.browser_bridge import (
            adopt_page,
            execution_page_lease,
            open_or_attach_browser,
        )
        from WebAgent.browser_client import WebAgentBrowserClient
        from agent_core.remote_control_plane import RemoteControlPlane, execute_page_control
        from WebAgent.controller import live_planner
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        from WebAgent.session import ensure_webagent_session
        from WebAgent.tool_context import WebAgentToolContext

        with execution_page_lease(
            timeout_sec=180.0,
            label=f"RemoteAgent worker {task.request_id}",
            marker_path=remote_execution_page_lock_path(root),
        ):
            page, context, attached_pw, browser_mode = open_or_attach_browser(
                requested_execution_url, reuse_remote_agent_page=True
            )
            execution_url = str(getattr(page, "url", "") or "")
            scraper = adopt_page(page, context, attached_pw)
            scraper_holder["scraper"] = scraper
            # The CDP page is shared by legacy controllers. Bind this worker to
            # the requested conversation and fail closed if attach returned a
            # different tab instead of silently sending there.
            scraper._expected_execution_url = requested_execution_url
            from agent_core.conversation_identity import conversation_id
            if conversation_id(execution_url) != conversation_id(requested_execution_url):
                runtime_log.write(
                    "ERROR", component="telegram_webagent_worker",
                    stage="EXECUTION_PAGE_MISMATCH", task_id=task.task_id,
                    request_id=task.request_id, expected_url=requested_execution_url,
                    actual_url=execution_url,
                )
                raise RuntimeError(
                    f"remote_execution_page_changed: expected={requested_execution_url}; actual={execution_url}"
                )
            control_plane = RemoteControlPlane(root)

            def service_priority_control():
                request = control_plane.claim(f"worker:{os.getpid()}:{task.request_id}")
                if request is None:
                    return False
                cid = str(request.get("request_id", ""))
                command = str(request.get("command", "") or "")
                runtime_log.write("CONNECT", component="telegram_webagent_worker", stage="CONTROL_PREEMPT_REQUESTED", task_id=task.task_id, request_id=task.request_id, control_id=cid, control=command)
                runtime_log.write("CONNECT", component="telegram_webagent_worker", stage="CONTROL_PAGE_LEASE_ACQUIRED", task_id=task.task_id, request_id=task.request_id, control_id=cid)
                try:
                    if command in {"refresh", "重新整理"}:
                        runtime_log.write("CONNECT", component="telegram_webagent_worker", stage="REFRESH_STARTED", task_id=task.task_id, request_id=task.request_id, control_id=cid)
                    result = execute_page_control(page, execution_url, request, output_dir=remote_control_output_root(root))
                    stage = "SNAPSHOT_COMPLETED" if command == "snapshot webgpt" else "REFRESH_COMPLETED"
                    runtime_log.write("CONNECT", component="telegram_webagent_worker", stage=stage, task_id=task.task_id, request_id=task.request_id, control_id=cid)
                    control_plane.complete(cid, result=result)
                    runtime_log.write("CONNECT", component="telegram_webagent_worker", stage="CONTROL_RESUME", task_id=task.task_id, request_id=task.request_id, control_id=cid)
                except Exception as exc:
                    control_plane.complete(cid, error=f"{type(exc).__name__}: {exc}")
                    runtime_log.write("ERROR", component="telegram_webagent_worker", stage="CONTROL_RESUME", task_id=task.task_id, request_id=task.request_id, control_id=cid, error=f"{type(exc).__name__}: {exc}")
                return True

            scraper._execution_page_lease_owned = True
            scraper._control_hook = service_priority_control
            sink = _event_sink(runtime_log, task, root=root, events=events)
            client = WebAgentBrowserClient(
                scraper, event_sink=sink, display_name="RemoteAgent"
            )

            def check_task_cancelled() -> None:
                if cancel_seen.is_set() or queue.cancellation_requested(task.task_id):
                    raise RemoteTaskCancelled("telegram_interrupt_requested")

            runtime_log.write(
                "CONNECT",
                component="telegram_webagent_worker",
                stage="EXECUTION_ROUTE",
                task_id=task.task_id,
                request_id=task.request_id,
                execution_url=execution_url,
                requested_execution_url=requested_execution_url,
                browser_mode=browser_mode,
            )
            ensure_webagent_session(
                workspace=task.workspace,
                gpt_url=execution_url,
                state_dir=remote_webagent_state_root(root),
                send_prompt=client.ask,
                display_name="RemoteAgent",
            )
            tool_context = WebAgentToolContext(
                task.workspace, interface_name="remote"
            )
            reply_route = dict(getattr(task, "reply_route", {}) or (task.metadata or {}).get("reply_route", {}) or {})
            approval_chat_id = str(reply_route.get("chat_id", "") or "")

            def notify_security_approval(record: dict, manifest: dict) -> None:
                events.emit(
                    "SECURITY_CONFIRMATION_REQUIRED", task,
                    status="WAITING_APPROVAL",
                    payload=(
                        {
                            "approval_kind": "EXECUTION",
                            "approval_id": record["approval_id"],
                            "target": manifest["target"],
                            "command": manifest.get("command", ""),
                            "cwd": manifest.get("cwd", ""),
                            "executable_sha256": manifest.get("executable_sha256", ""),
                            "command_sha256": manifest.get("command_sha256", ""),
                            "manifest_digest": manifest["manifest_digest"],
                        }
                        if str(manifest.get("approval_kind", "")).upper() == "EXECUTION"
                        else {
                            "approval_kind": "DELETE",
                            "approval_id": record["approval_id"],
                            "target": manifest["target"],
                            "entry_count": manifest["entry_count"],
                            "total_bytes": manifest["total_bytes"],
                            "manifest_digest": manifest["manifest_digest"],
                            "permanent_scope": str(manifest.get("permanent_scope", "") or ""),
                        }
                    ),
                )
                runtime_log.write(
                    "STATUS", component="telegram_webagent_worker",
                    stage="SECURITY_CONFIRMATION_REQUIRED",
                    task_id=task.task_id, request_id=task.request_id,
                    approval_id=record["approval_id"], target=manifest["target"],
                    approval_kind=str(manifest.get("approval_kind", "DELETE") or "DELETE"),
                )

            tool_context.configure_security_approval(
                notify_security_approval, chat_id=approval_chat_id,
                timeout_sec=float(os.environ.get("SMARTAGENT_SECURITY_APPROVAL_TIMEOUT_SEC", "300")),
            )
            tool_context.security_approval_ledger_root = root
            tool_context.security_approval_cancel_check = check_task_cancelled
            loop = WebAgentProtocolLoop(
                task.workspace,
                lambda prompt, expected, attachments: live_planner(
                    client, prompt, expected, attachments
                ),
                event_sink=sink,
                display_name="RemoteAgent",
                tool_context=tool_context,
                cancel_check=check_task_cancelled,
                progress_root=root,
            )
            from agent_core.project_sync_receiver import browser_receiver_identity
            loop.tools.configure_project_sync_receiver(lambda: browser_receiver_identity(scraper))
            final_content = loop.run(
                task.request,
                request_id=task.request_id,
                task_id=task.task_id,
                task_epoch=task.task_epoch,
                initial_attachments=_attachment_paths(task) + skill_attachments,
                skill_context=skill_context,
                source_tag=(
                    "REMOTEAGENT_TELEGRAM"
                    if source_transport == "TELEGRAM"
                    else "REMOTEAGENT_TELEGRAM_SIMULATION"
                ),
            )
            if cancel_seen.is_set() or queue.cancellation_requested(task.task_id):
                raise RemoteTaskCancelled("telegram_interrupt_requested")
            # Completion handoff barrier: the v8 response is already UI-idle
            # and commit-accepted at this point.  Release only this worker's
            # ownership and disconnect its Playwright client while the shared
            # browser/page remains open.  Telegram completion is emitted only
            # after this barrier succeeds.
            handoff = preserve_completed_page(
                page, scraper, attached_pw, requested_execution_url
            )
            attached_pw = None
            preserved_url = str(handoff["conversation_url"])
            runtime_log.write(
                "STATUS", component="telegram_webagent_worker",
                stage="WEBGPT_TURN_FINISHED", task_id=task.task_id,
                request_id=task.request_id, conversation_url=preserved_url,
                protocol_version=9,
            )
            runtime_log.write(
                "CONNECT", component="telegram_webagent_worker",
                stage="PAGE_PRESERVED", task_id=task.task_id,
                request_id=task.request_id, conversation_url=preserved_url,
                remaining_owners=int(handoff["remaining_owners"]),
            )
        elapsed = round(time.time() - started_at, 3)
        progress_snapshot = read_progress(task.task_id, root=root)
        final_content, plan_id = publish_completed_plan(
            ledger=PlanLedger.from_task_store(remote_tasks_path(root)),
            task=task,
            final_summary=final_content,
            progress=progress_snapshot,
        )
        if plan_id:
            runtime_log.write(
                "STATUS",
                component="telegram_webagent_worker",
                stage="PLAN_PUBLISHED",
                task_id=task.task_id,
                request_id=task.request_id,
                plan_id=plan_id,
            )
        terminal_payload = prepare_telegram_completion(
            request=task.request,
            summary=final_content,
            workspace=task.workspace,
            artifacts=loop.tools.take_outbound_artifacts(),
        )
        terminal_payload["plan_id"] = plan_id
        final_content = terminal_payload["summary"]
        if cancel_seen.is_set() or queue.cancellation_requested(task.task_id):
            raise RemoteTaskCancelled("telegram_interrupt_requested")
        completed = queue.complete(
            task.task_id,
            result={
                "response": final_content,
                "worker_pid": os.getpid(),
                "elapsed_sec": elapsed,
                "protocol_engine": "web_agent_direct",
                "ack_ids": list(loop.accepted_ack_ids),
                "protocol_family": "SMARTAGENT_V9",
                "protocol_version": 9,
                "task_id": task.task_id,
                "task_epoch": task.task_epoch,
                "intent_digest": loop.intent_digest,
                "action_result_ledger": dict(loop.action_result_ledger),
                "uploaded_attachments": list(loop.sent_attachment_paths),
                "outbound_artifacts": terminal_payload["artifacts"],
                "artifact_rejections": terminal_payload["artifact_rejections"],
                "task_outcome": loop.terminal_outcome,
                "verification_status": loop.tools.last_verification_status,
                "execution_state": loop.execution_state,
                "protocol_state": loop.protocol_state,
                "terminal_candidate": dict(loop.terminal_candidate),
                "plan_id": plan_id,
                "source_plan_id": str((task.metadata or {}).get("source_plan_id", "") or ""),
            },
        )
        events.emit(
            "TASK_COMPLETED",
            completed,
            status="COMPLETED",
            payload=terminal_payload,
        )
        set_runtime_state(task.task_id, "COMPLETED", root=root)
        delete_progress(task.task_id, root=root)
        runtime_log.write(
            "TASK_COMPLETED",
            component="telegram_webagent_worker",
            stage="FINAL_TO_TELEGRAM",
            task_id=task.task_id,
            request_id=task.request_id,
            elapsed_sec=elapsed,
        )
        print(f"[RemoteAgent] 任務完成，等待 Telegram 回傳: {task.request_id}", flush=True)
        return 0
    except BaseException as exc:
        if isinstance(exc, RemoteTaskCancelled) or cancel_seen.is_set():
            set_runtime_state(
                task.task_id, "INTERRUPTED",
                reason="telegram_interrupt_requested",
                root=root,
            )
            cancelled = queue.mark_cancelled(
                task.task_id, reason="telegram_interrupt_completed"
            )
            if str((cancelled.metadata or {}).get("cancel_reason", "")) != "telegram_interrupt_requested":
                events.emit(
                    "TASK_INTERRUPTED", cancelled, status="CANCELLED",
                    payload={"error": "任務已由 Telegram 中斷。"},
                )
            runtime_log.write(
                "STATUS", component="telegram_webagent_worker",
                stage="TASK_CANCELLED", task_id=task.task_id,
                request_id=task.request_id,
            )
            return 0
        progress = read_progress(task.task_id, root=root)
        # A protocol loop may deliberately pause after detecting semantic
        # stagnation.  Preserve that durable status for Telegram progress
        # inspection instead of overwriting it with generic INTERRUPTED.
        if progress is None or progress.runtime_state not in {"INTERRUPTED", "PAUSED"}:
            set_runtime_state(
                task.task_id, "INTERRUPTED",
                reason=f"{type(exc).__name__}: {exc}",
                root=root,
            )
        loop_execution = getattr(loop, "execution_state", "UNKNOWN") if "loop" in locals() else "UNKNOWN"
        loop_protocol = getattr(loop, "protocol_state", "UNKNOWN") if "loop" in locals() else "UNKNOWN"
        loop_outcome = getattr(loop, "terminal_outcome", "UNKNOWN") if "loop" in locals() else "UNKNOWN"
        loop_candidate = dict(getattr(loop, "terminal_candidate", {}) or {}) if "loop" in locals() else {}
        failure_payload = {
            "error": f"{type(exc).__name__}: {exc}",
            "execution_state": loop_execution,
            "protocol_state": loop_protocol,
            "task_outcome": loop_outcome,
            "terminal_candidate": loop_candidate,
        }
        protocol_only_interruption = (
            loop_protocol == "INTERRUPTED"
            and loop_execution == "SUCCEEDED"
        )
        if protocol_only_interruption:
            interrupted = queue.interrupt_running(
                task.task_id,
                failure_payload["error"],
                result={
                    **failure_payload,
                    "action_result_ledger": dict(getattr(loop, "action_result_ledger", {}) or {}),
                },
            )
            if interrupted is not None:
                events.emit(
                    "TASK_INTERRUPTED", interrupted, status="INTERRUPTED",
                    payload=failure_payload,
                )
        else:
            failed = queue.fail_running(task.task_id, failure_payload["error"])
            if failed is not None:
                events.emit(
                    "TASK_FAILED", failed, status="FAILED", payload=failure_payload,
                )
        runtime_log.write(
            "ERROR",
            component="telegram_webagent_worker",
            stage="EXECUTION_FAILED",
            task_id=task.task_id,
            request_id=task.request_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        stop_heartbeat.set()
        try:
            if 'scraper' in locals(): scraper.release_conversation_owner()
        except Exception:
            pass
        if attached_pw is not None:
            try:
                attached_pw.stop()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteAgent Telegram WebAgent worker")
    parser.add_argument("--remote-worker-task", required=True)
    parser.add_argument("--remote-worker-token", required=True)
    parser.add_argument("--remote-cdp", default="")
    args = parser.parse_args(argv)
    return run_task(
        task_id=args.remote_worker_task,
        token=args.remote_worker_token,
        cdp=args.remote_cdp,
    )


if __name__ == "__main__":
    raise SystemExit(main())
