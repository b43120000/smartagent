#!/usr/bin/env python3
from __future__ import annotations

"""One-shot Telegram -> WebAgent Direct execution worker.

The Telegram receiver and durable queue remain transport owners.  This worker
only owns one request-scoped WebGPT protocol loop and writes the terminal result
back to the transport-neutral RemoteEvent outbox.
"""

import argparse
import os
import threading
import time
from pathlib import Path

from agent_core.conversation_registry import ConversationRegistry
from agent_core.remote_events import RemoteEventStore
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.task_transport import resolve_task_execution_chatgpt_url


ROOT = Path(__file__).resolve().parents[1]


def _attachment_paths(task) -> list[str]:
    paths: list[str] = []
    for item in list((task.metadata or {}).get("attachments") or []):
        if not isinstance(item, dict):
            continue
        value = str(item.get("local_path", "") or "").strip()
        if value:
            paths.append(value)
    return paths


def _event_sink(runtime_log: RemoteRuntimeLog, task):
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
    return emit


def run_task(*, task_id: str, token: str, cdp: str, root: Path = ROOT) -> int:
    if cdp:
        os.environ["SMARTAGENT_CHATGPT_CDP"] = str(cdp)
    os.environ["SMARTAGENT_ATTACH_CDP"] = "1"
    os.environ["SMARTAGENT_REMOTE_WORKER_TASK"] = str(task_id)

    root = Path(root).resolve()
    base = root / ".agents"
    worker_dir = base / "remote_workers" / str(task_id)
    runtime_log = RemoteRuntimeLog(
        base / "remote_runtime.jsonl",
        mirror_console=True,
        mirror_paths=[worker_dir / "runtime.jsonl"],
        snapshot_path=worker_dir / "lifecycle.json",
    )
    queue = RemoteTaskQueue(TaskStateStore(base / "remote_tasks.json"))
    events = RemoteEventStore(base / "remote_events.json")
    task = queue.adopt_dispatched(task_id, token, worker_pid=os.getpid())
    source_transport = str((task.metadata or {}).get("transport", "")).upper()
    if source_transport not in {"TELEGRAM", "LOCAL_TEST"}:
        raise RuntimeError("telegram_webagent_worker_rejects_non_telegram_task")

    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(5.0):
            try:
                queue.heartbeat(task.task_id, worker_pid=os.getpid())
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
        registry = ConversationRegistry()
        registry.load()
        execution_url = resolve_task_execution_chatgpt_url(task, registry=registry)

        # Import only after the request-scoped CDP endpoint has been published.
        from WebAgent.browser_bridge import (
            adopt_page,
            execution_page_lease,
            open_or_attach_browser,
        )
        from WebAgent.browser_client import WebAgentBrowserClient
        from WebAgent.controller import live_planner
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        from WebAgent.session import ensure_webagent_session

        with execution_page_lease(
            timeout_sec=180.0,
            label=f"RemoteAgent worker {task.request_id}",
            marker_path=base / "remote_execution_page.lock",
        ):
            page, context, attached_pw, browser_mode = open_or_attach_browser(execution_url)
            scraper = adopt_page(page, context, attached_pw)
            sink = _event_sink(runtime_log, task)
            client = WebAgentBrowserClient(
                scraper, event_sink=sink, display_name="RemoteAgent"
            )
            runtime_log.write(
                "CONNECT",
                component="telegram_webagent_worker",
                stage="EXECUTION_ROUTE",
                task_id=task.task_id,
                request_id=task.request_id,
                execution_url=execution_url,
                browser_mode=browser_mode,
            )
            ensure_webagent_session(
                workspace=task.workspace,
                gpt_url=execution_url,
                state_dir=base / "remote_webagent_state",
                send_prompt=client.ask,
                display_name="RemoteAgent",
            )
            loop = WebAgentProtocolLoop(
                task.workspace,
                lambda prompt, expected, attachments: live_planner(
                    client, prompt, expected, attachments
                ),
                event_sink=sink,
                display_name="RemoteAgent",
            )
            final_content = loop.run(
                task.request,
                request_id=task.request_id,
                initial_attachments=_attachment_paths(task),
                source_tag=(
                    "REMOTEAGENT_TELEGRAM"
                    if source_transport == "TELEGRAM"
                    else "REMOTEAGENT_TELEGRAM_SIMULATION"
                ),
            )
        elapsed = round(time.time() - started_at, 3)
        completed = queue.complete(
            task.task_id,
            result={
                "response": final_content,
                "worker_pid": os.getpid(),
                "elapsed_sec": elapsed,
                "protocol_engine": "web_agent_direct",
            },
        )
        events.emit(
            "TASK_COMPLETED",
            completed,
            status="COMPLETED",
            payload={"summary": final_content},
        )
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
        failed = queue.fail_running(task.task_id, f"{type(exc).__name__}: {exc}")
        if failed is not None:
            events.emit(
                "TASK_FAILED",
                failed,
                status="FAILED",
                payload={"error": failed.error},
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
