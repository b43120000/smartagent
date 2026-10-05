#!/usr/bin/env python3
from __future__ import annotations

"""Inject one offline Telegram-shaped update into the live RemoteAgent path."""

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from agent_core.agent_gateway import AgentIngressGateway
from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.paths import (remote_events_path, remote_runtime_log_path, remote_supervisor_state_path, remote_tasks_path, remote_test_runs_root, remote_transport_sessions_path, telegram_config_path)
from RemoteAgent.local_test_delivery import LocalTestDeliveryAdapter
from RemoteAgent.telegram_transport import (
    TelegramOffsetStore,
    TelegramReceiver,
    TelegramReceiverConfig,
)


ROOT = Path(__file__).resolve().parents[1]

try:
    # Direct PowerShell test runs may still use a legacy Windows code page.
    # The BAT switches to UTF-8, while this fallback prevents status emoji from
    # aborting an otherwise valid diagnostic run.
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass


def _workspace_from_saved_config(root: Path) -> str:
    path = telegram_config_path(root)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        workspace = Path(str(payload.get("workspace", "") or "")).resolve()
        return str(workspace) if workspace.is_dir() else ""
    except (OSError, ValueError, TypeError):
        return ""


def _supervisor_ready(root: Path) -> tuple[bool, str]:
    path = remote_supervisor_state_path(root)
    if not path.is_file():
        return False, "找不到 RemoteAgent supervisor 狀態"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid", 0) or 0)
        age = max(0.0, time.time() - float(payload.get("heartbeat_at", 0.0) or 0.0))
        if str(payload.get("status", "")).upper() != "RUNNING":
            return False, f"supervisor status={payload.get('status')}"
        if age > 15.0:
            return False, f"supervisor heartbeat 已過期 {age:.1f} 秒"
        if pid <= 0:
            return False, "supervisor PID 無效"
        if not _process_alive(pid):
            return False, f"supervisor PID {pid} 不存在"
        return True, f"pid={pid} heartbeat_age={age:.1f}s"
    except Exception as exc:
        return False, f"狀態讀取失敗: {type(exc).__name__}: {exc}"


def _process_alive(pid: int) -> bool:
    if int(pid or 0) <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class _OneUpdateClient:
    def __init__(self, update: dict):
        self.update = dict(update)

    def get_updates(self, *, offset: int, timeout: int):
        del timeout
        return [self.update] if int(self.update["update_id"]) >= int(offset) else []

    def send_message(self, chat_id: int, text: str, *, reply_to_message_id=None):
        # Normal task status uses LOCAL_TEST delivery.  This method exists only
        # to satisfy TelegramReceiver's transport interface without networking.
        return {
            "message_id": 1,
            "chat_id": int(chat_id),
            "text": str(text),
            "reply_to_message_id": reply_to_message_id,
        }


class _LocalReplyAdapter:
    def __init__(self, base, *, conversation_key: str, outbox: Path):
        self.base = base
        self.conversation_key = str(conversation_key)
        self.outbox = Path(outbox)

    def is_authorized(self, **identity):
        return self.base.is_authorized(**identity)

    def normalize(self, update):
        message, reason = self.base.normalize(update)
        if message is None:
            return None, reason
        route = {
            **dict(message.reply_context or {}),
            "transport": "LOCAL_TEST",
            "outbox_path": str(self.outbox),
        }
        return replace(
            message,
            transport="LOCAL_TEST",
            endpoint="local-telegram-simulation",
            conversation_key=self.conversation_key,
            reply_context=route,
            metadata={**dict(message.metadata or {}), "local_simulation": True},
        ), reason


def _print_new_events(outbox: Path, seen: set[str]) -> None:
    if not outbox.is_file():
        return
    for line in outbox.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        event_id = str((row.get("event") or {}).get("event_id", "") or "")
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        print("\n" + str(row.get("text", "") or ""), flush=True)


def _tail_request_log(root: Path, request_id: str, limit: int = 20) -> list[str]:
    path = remote_runtime_log_path(root)
    if not path.is_file():
        return []
    matches = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if request_id in line]
    return matches[-max(1, int(limit)):]


def send_and_wait(
    *,
    request: str,
    workspace: str,
    timeout_sec: float,
    root: Path = ROOT,
    result_sink: dict | None = None,
) -> int:
    root = Path(root).resolve()
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"Workspace 不存在: {workspace_path}")
    ready, detail = _supervisor_ready(root)
    if not ready:
        print(f"[RemoteAgent Test][ERROR] {detail}", flush=True)
        print("請先重新啟動 launch_remote_agent.bat，看到 WAITING_SIGNAL 後再執行本腳本。", flush=True)
        return 2

    run_token = "LOCAL-" + uuid.uuid4().hex[:12].upper()
    run_dir = remote_test_runs_root(root) / run_token
    outbox = run_dir / "events.jsonl"
    update_id = int(time.time() * 1000)
    conversation_key = f"telegram://local-test/{run_token}"
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "from": {"id": 1, "is_bot": False},
            "chat": {"id": 1, "type": "private"},
            "text": str(request).strip(),
        },
    }

    queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
    event_store = RemoteEventStore(remote_events_path(root))
    ingress = AgentIngressGateway(
        task_queue=queue,
        session_router=TransportSessionRouter(remote_transport_sessions_path(root)),
        event_store=event_store,
        runtime_log=RemoteRuntimeLog(remote_runtime_log_path(root)),
    )
    delivery = DeliveryManager(
        event_store,
        {"LOCAL_TEST": LocalTestDeliveryAdapter(root)},
    )

    def deliver_accepted(task) -> None:
        event = event_store.emit("TASK_ACCEPTED", task, status="QUEUED", payload={})
        result = delivery.deliver(event)
        if not result.get("delivered"):
            raise RuntimeError(str(result.get("reason", "local_test_accept_delivery_failed")))

    receiver = TelegramReceiver(
        config=TelegramReceiverConfig(
            enabled=True,
            bot_token="LOCAL-TEST-NO-NETWORK",
            allowed_user_ids=(1,),
            allowed_chat_ids=(1,),
            workspace=str(workspace_path),
            poll_timeout_sec=1,
            retry_delay_sec=0.01,
        ),
        client=_OneUpdateClient(update),
        offset_store=TelegramOffsetStore(run_dir / "telegram_offset.json"),
        ingress=ingress,
        runtime_log=RemoteRuntimeLog(remote_runtime_log_path(root)),
        accepted_handler=deliver_accepted,
    )
    receiver.adapter = _LocalReplyAdapter(
        receiver.adapter,
        conversation_key=conversation_key,
        outbox=outbox,
    )

    accepted = receiver.poll_once()
    if len(accepted) != 1:
        raise RuntimeError(f"local Telegram simulation expected one task, got {len(accepted)}")
    task = accepted[0]
    if result_sink is not None:
        result_sink.update(
            {
                "request_id": task.request_id,
                "task_id": task.task_id,
                "terminal_state": "",
                "completion_text": "",
            }
        )
    print("[RemoteAgent Test] 本機 Telegram 模擬訊息已送入正式接收流程。", flush=True)
    print(f"[RemoteAgent Test] request_id={task.request_id}", flush=True)
    print(f"[RemoteAgent Test] task_id={task.task_id}", flush=True)
    print(f"[RemoteAgent Test] workspace={workspace_path}", flush=True)
    print(f"[RemoteAgent Test] outbox={outbox}", flush=True)

    seen: set[str] = set()
    deadline = time.monotonic() + max(5.0, float(timeout_sec))
    while time.monotonic() < deadline:
        delivery.retry_ready(transports={"LOCAL_TEST"})
        _print_new_events(outbox, seen)
        with queue.store.process_lock():
            queue.store.load()
            current = queue.store.get(task.task_id)
        if current is not None and current.state in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
            delivery.retry_ready(transports={"LOCAL_TEST"})
            _print_new_events(outbox, seen)
            print(f"\n[RemoteAgent Test] terminal_state={current.state}", flush=True)
            if result_sink is not None:
                ledger = dict(getattr(current, "result_ledger", {}) or {})
                final_value = ledger.get("final", "")
                if isinstance(final_value, dict):
                    final_payload = final_value
                    final_value = final_payload.get("response", "")
                else:
                    final_payload = {}
                result_sink.update(
                    {
                        "terminal_state": current.state,
                        "completion_text": str(final_value or ""),
                        "ack_ids": list(final_payload.get("ack_ids") or []),
                        "uploaded_attachments": list(
                            final_payload.get("uploaded_attachments") or []
                        ),
                        "verification_status": str(
                            final_payload.get("verification_status", "") or ""
                        ),
                    }
                )
            return 0 if current.state == "COMPLETED" else 1
        time.sleep(1.0)

    print(f"\n[RemoteAgent Test][TIMEOUT] {timeout_sec:.0f} 秒內未完成。", flush=True)
    with queue.store.process_lock():
        queue.store.load()
        current = queue.store.get(task.task_id)
    print(f"[RemoteAgent Test] task_state={getattr(current, 'state', 'MISSING')}", flush=True)
    for line in _tail_request_log(root, task.request_id):
        print(line, flush=True)
    print(f"[RemoteAgent Test] 完整 outbox: {outbox}", flush=True)
    print(f"[RemoteAgent Test] 完整總 log: {remote_runtime_log_path(root)}", flush=True)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Telegram sender for RemoteAgent")
    parser.add_argument("--request", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        print("REMOTEAGENT_LOCAL_TELEGRAM_SENDER_PARSE_OK")
        return 0
    request = str(args.request or "").strip()
    if not request:
        request = input("[RemoteAgent Test] 請輸入要模擬的 Telegram 任務: ").strip()
    if not request:
        raise SystemExit("任務不可為空")
    workspace = str(args.workspace or "").strip() or _workspace_from_saved_config(ROOT)
    if not workspace:
        raise SystemExit("找不到已保存的 RemoteAgent workspace；請先執行 Edit_workspace.bat。")
    return send_and_wait(
        request=request,
        workspace=workspace,
        timeout_sec=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
