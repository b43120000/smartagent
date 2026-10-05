#!/usr/bin/env python3
"""Priority software controls that do not depend on Agent 0 execution."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .paths import remote_control_output_root, remote_tasks_path


class RemoteControlDispatcher:
    """Execute Telegram controls outside the Agent/WebGPT request pipeline."""

    def __init__(
        self,
        *,
        root: Path,
        python: str,
        runtime_log: Any,
        control_plane: Any,
        session_state: Callable[[], dict[str, Any]],
        request_restart: Callable[[Any], Any],
        request_close: Callable[[Any], Any],
        runtime_apply_binding: Callable[[dict], None] | None = None,
        binding_update_allowed: Callable[[], bool] | None = None,
        run_command: Callable[..., Any] | None = None,
        terminate_process_tree: Callable[[int], bool] | None = None,
    ) -> None:
        self.root = Path(root)
        self.python = str(python)
        self.runtime_log = runtime_log
        self.control_plane = control_plane
        self._session_state = session_state
        self._request_restart = request_restart
        self._request_close = request_close
        self._runtime_apply_binding = runtime_apply_binding
        self._binding_update_allowed = binding_update_allowed
        self._run_command = run_command
        self._terminate_process_tree = terminate_process_tree

    def handle(self, message=None):
        from .remote_binding_manager import (
            LIST_SKILLS_COMMAND,
            MANAGER_COMMAND,
            RemoteBindingManager,
        )
        from RemoteAgent.remote_feature_catalog import (
            INTERRUPT_COMMAND,
            STATUS_COMMAND,
            canonical_remote_control,
        )
        from RemoteAgent.remote_restart import RECONNECT_COMMAND

        raw_text = str(getattr(message, "text", "") or "").strip()
        binding_manager = RemoteBindingManager(self.root)
        chat_id = int(
            (getattr(message, "reply_context", {}) or {}).get("chat_id", 0) or 0
        )
        progress_match = re.fullmatch(
            r"查看任務進度\s+(TASK-[A-F0-9]{20})", raw_text, re.IGNORECASE
        )
        if progress_match:
            return self._run_task_progress_control(
                message, progress_match.group(1).upper()
            )
        workspace_delete_match = re.fullmatch(
            r"security_workspace_delete\s+(APR-[A-F0-9]{20})", raw_text, re.IGNORECASE
        )
        if workspace_delete_match:
            from .security_approval import SecurityApprovalError, SecurityApprovalLedger
            try:
                record = SecurityApprovalLedger(self.root).grant_workspace_for_approval(
                    workspace_delete_match.group(1).upper(),
                    chat_id=str(chat_id), actor=f"telegram:{chat_id}",
                )
                return f"已永久允許此 Workspace 內的刪除：{record.get('permanent_scope', '')}"
            except SecurityApprovalError as exc:
                return f"永久刪除授權失敗：{exc}"
        approval_match = re.fullmatch(r"(確認刪除|拒絕刪除|security_approve_once|security_reject_once)\s+(APR-[A-F0-9]{20})", raw_text, re.IGNORECASE)
        if approval_match:
            from .security_approval import SecurityApprovalError, SecurityApprovalLedger
            try:
                verb = approval_match.group(1).casefold()
                approve = verb in {"確認刪除", "security_approve_once"}
                record = SecurityApprovalLedger(self.root).decide(
                    approval_match.group(2).upper(), approve=approve,
                    chat_id=str(chat_id), actor=f"telegram:{chat_id}",
                )
                if record["state"] == "APPROVED":
                    return "已確認，等待執行一次。"
                return "已拒絕。"
            except SecurityApprovalError as exc:
                return f"🔴 安全確認無效：{exc}"
        if binding_manager.is_update(raw_text):
            if self._binding_update_allowed is not None and not self._binding_update_allowed():
                return (
                    "⚠️ 目前有遠端任務正在執行或等待執行，設定未變更。\n"
                    "請先使用「任務中斷」，再重新送出設定。"
                )
            if self._runtime_apply_binding is None:
                return "🔴 設定未更新：runtime binding apply unavailable"
            try:
                return binding_manager.apply(
                    raw_text, chat_id=chat_id,
                    runtime_apply=self._runtime_apply_binding,
                )
            except Exception as exc:
                return f"🔴 設定未更新\n{type(exc).__name__}: {str(exc)[:500]}"
        text = canonical_remote_control(raw_text)
        if text == MANAGER_COMMAND.casefold():
            try:
                return binding_manager.begin(chat_id=chat_id)
            except Exception as exc:
                return f"🔴 無法開啟設定管理器\n{type(exc).__name__}: {str(exc)[:500]}"
        if text == LIST_SKILLS_COMMAND.casefold():
            try:
                return binding_manager.render_skills()
            except Exception as exc:
                return f"🔴 無法列出 skill\n{type(exc).__name__}: {str(exc)[:500]}"
        if text == RECONNECT_COMMAND.casefold():
            self.runtime_log.write(
                "CONNECT",
                component="remote_control_dispatcher",
                stage="RUNTIME_RESTART_REQUESTED",
            )
            response = self._request_restart(message)
            return response or (
                "已開始完整重啟；新 listener 就緒後會再回覆「已重新連線」。"
            )
        if text == "關閉任務":
            return self._request_close(message)
        if text == INTERRUPT_COMMAND.casefold():
            return self._interrupt_current_task(message)
        if text == STATUS_COMMAND.casefold():
            return self._run_status_control(message)
        if text in {"snapshot webgpt", "refresh", "重新整理"}:
            return self._run_browser_control(text)
        return self._run_queued_control(text, message)

    def _active_telegram_task(self, message: Any):
        from .task_state import (
            TASK_PAUSED,
            TASK_PAUSING,
            TASK_QUEUED,
            TASK_RESUMING,
            TASK_RUNNING,
            RemoteTaskQueue,
            TaskStateStore,
        )

        chat_id = int(
            (getattr(message, "reply_context", {}) or {}).get("chat_id", 0)
            or 0
        )
        if not chat_id:
            return None, None
        queue = RemoteTaskQueue(
            TaskStateStore(remote_tasks_path(self.root))
        )
        active_states = {
            TASK_QUEUED, TASK_RUNNING, TASK_PAUSING, TASK_PAUSED, TASK_RESUMING,
        }
        with queue.store.process_lock():
            queue.store.load()
            tasks = [
                task
                for task in queue.store.all()
                if task.state in active_states
                and str((task.metadata or {}).get("transport", "")).upper()
                == "TELEGRAM"
                and int((task.reply_route or {}).get("chat_id", 0) or 0)
                == chat_id
            ]
        running = [task for task in tasks if task.state == TASK_RUNNING]
        candidates = running or tasks
        target = max(candidates, key=lambda task: (task.created_at, task.task_id)) if candidates else None
        return queue, target

    def _terminate_task_process(self, pid: int) -> bool:
        pid = int(pid or 0)
        if pid <= 0 or pid == os.getpid():
            return False
        if self._terminate_process_tree is not None:
            return bool(self._terminate_process_tree(pid))
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True

    def _interrupt_current_task(self, message: Any) -> dict[str, Any]:
        try:
            queue, task = self._active_telegram_task(message)
            if queue is None or task is None:
                return {
                    "message": "目前這個 Telegram 對話沒有正在執行或等待中的任務。"
                }
            original_state = str(task.state)
            task = queue.cancel_task(
                task.task_id, reason="telegram_interrupt_requested"
            )
            if task.state == "COMPLETED":
                return {"message": "任務已在中斷指令生效前完成，因此沒有覆寫完成結果。"}
            if task.state in {"FAILED", "INTERRUPTED", "CANCELLED"} and not (
                original_state == "QUEUED" and task.state == "CANCELLED"
            ):
                return {"message": "這項任務已經結束，目前不需要再次中斷。"}
            if original_state == "QUEUED" and task.state == "CANCELLED":
                return {
                    "message": (
                        "已取消等待中的任務；它不會啟動 Agent 或送到 WebGPT。"
                        "Telegram listener 會繼續等待下一個指令。"
                    )
                }

            browser_result = self._run_browser_control("cancel webgpt")
            worker_pid = int((task.metadata or {}).get("worker_pid", 0) or 0)
            heartbeat_at = float((task.metadata or {}).get("heartbeat_at", 0.0) or 0.0)
            terminated = False
            termination_error = ""
            if worker_pid and heartbeat_at and time.time() - heartbeat_at <= 20.0:
                try:
                    terminated = self._terminate_task_process(worker_pid)
                except Exception as exc:
                    termination_error = f"{type(exc).__name__}: {exc}"
            terminal = queue.mark_cancelled(
                task.task_id, reason="telegram_interrupt_completed"
            )
            if terminal.state == "COMPLETED":
                return {"message": "任務已在中斷指令生效前完成，因此沒有覆寫完成結果。"}
            stopped = bool(browser_result.get("stopped", False))
            details = []
            if stopped:
                details.append("WebGPT 回覆已停止")
            if terminated:
                details.append("執行中的 worker 已關閉")
            if termination_error:
                self.runtime_log.write(
                    "ERROR",
                    component="remote_control_dispatcher",
                    stage="TASK_INTERRUPT_PROCESS",
                    task_id=task.task_id,
                    request_id=task.request_id,
                    error=termination_error,
                )
            suffix = "；".join(details) if details else "取消狀態已記錄"
            self.runtime_log.write(
                "CONNECT",
                component="remote_control_dispatcher",
                stage="TASK_INTERRUPTED",
                task_id=task.task_id,
                request_id=task.request_id,
                worker_pid=worker_pid,
                worker_terminated=terminated,
                webgpt_stopped=stopped,
            )
            return {
                "message": (
                    f"已中斷目前任務（request_id: {task.request_id}）。{suffix}。"
                    "Telegram listener 與目前 WebGPT 對話窗會保留，"
                    "可以繼續接收下一個指令。"
                )
            }
        except Exception as exc:
            self.runtime_log.write(
                "ERROR",
                component="remote_control_dispatcher",
                stage="TASK_INTERRUPT",
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "message": f"任務中斷失敗：{type(exc).__name__}: {str(exc)[:500]}"
            }

    def _run_browser_control(self, command: str) -> dict[str, Any]:
        state = dict(self._session_state() or {})
        endpoint = str(state.get("cdp_endpoint", "") or "").strip()
        target_url = str(state.get("conversation_url", "") or "").strip()
        if not endpoint or not target_url:
            return {
                "message": (
                    "RemoteAgent control unavailable: "
                    "目前沒有可操作的 WebGPT browser session。"
                )
            }

        control_id = "CTRL-" + uuid.uuid4().hex.upper()
        output = remote_control_output_root(self.root) / f"{control_id}.png"
        stage = (
            "SNAPSHOT" if command == "snapshot webgpt"
            else "STATUS" if command == "查看現在工作狀態"
            else "CANCEL" if command == "cancel webgpt"
            else "REFRESH"
        )
        self.runtime_log.write(
            "CONNECT",
            component="remote_control_dispatcher",
            stage=f"{stage}_PARALLEL_STARTED",
            control_id=control_id,
        )
        timeout_sec = 40 if command in {
            "snapshot webgpt", "查看現在工作狀態", "cancel webgpt",
        } else 75
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        try:
            completed = (self._run_command or subprocess.run)(
                [
                    self.python,
                    "-m",
                    "RemoteAgent.browser_control_worker",
                    "--cdp",
                    endpoint,
                    "--url",
                    target_url,
                    "--command",
                    command,
                    "--output",
                    str(output),
                ],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=child_env,
                timeout=timeout_sec,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode:
                detail = str(completed.stderr or completed.stdout or "")[-1000:]
                raise RuntimeError(
                    detail or f"control_worker_exit:{completed.returncode}"
                )
            stdout = completed.stdout
            if not isinstance(stdout, str) or not stdout.strip():
                raise RuntimeError(
                    "control_worker_empty_stdout:"
                    f"stdout_type={type(stdout).__name__};"
                    f"stderr={str(completed.stderr or '')[-500:]}"
                )
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise RuntimeError("control_worker_invalid_result")
            self.runtime_log.write(
                "CONNECT",
                component="remote_control_dispatcher",
                stage=f"{stage}_PARALLEL_COMPLETED",
                control_id=control_id,
            )
            return result
        except Exception as exc:
            self.runtime_log.write(
                "ERROR",
                component="remote_control_dispatcher",
                stage=f"{stage}_PARALLEL_FAILED",
                control_id=control_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "message": (
                    f"RemoteAgent control failed: {type(exc).__name__}: "
                    f"{str(exc)[-500:]}"
                )
            }

    def _latest_telegram_attachment_names(self, message: Any) -> list[str]:
        """Find attachments on the latest task from the requesting chat."""
        try:
            from .task_state import TaskStateStore

            chat_id = int(
                (getattr(message, "reply_context", {}) or {}).get("chat_id", 0)
                or 0
            )
            if not chat_id:
                return []
            store = TaskStateStore(remote_tasks_path(self.root))
            with store.process_lock():
                store.load()
                tasks = [
                    task
                    for task in store.all()
                    if str((task.metadata or {}).get("transport", "")).upper()
                    == "TELEGRAM"
                    and int((task.reply_route or {}).get("chat_id", 0) or 0)
                    == chat_id
                ]
            if not tasks:
                return []
            latest = max(tasks, key=lambda task: (task.created_at, task.task_id))
            names: list[str] = []
            for attachment in list((latest.metadata or {}).get("attachments") or []):
                if not isinstance(attachment, dict):
                    continue
                name = str(attachment.get("file_name", "") or "").strip()
                if not name:
                    path = str(attachment.get("local_path", "") or "").strip()
                    name = os.path.basename(path) if path else ""
                if name and name not in names:
                    names.append(name)
            return names
        except Exception as exc:
            self.runtime_log.write(
                "ERROR",
                component="remote_control_dispatcher",
                stage="STATUS_ATTACHMENT_LOOKUP",
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    def _execution_status_for_task(self, task: Any) -> dict[str, Any]:
        if task is None:
            return {}
        try:
            from .execution_telemetry import refresh_snapshot
            data = refresh_snapshot(task.workspace, task.task_id)
            if data and str(data.get("task_id", "")) != str(task.task_id):
                return {}
            return data
        except Exception as exc:
            self.runtime_log.write(
                "ERROR", component="remote_control_dispatcher",
                stage="STATUS_EXECUTION_LOOKUP", task_id=str(getattr(task, "task_id", "")),
                error=f"{type(exc).__name__}: {exc}",
            )
            return {}

    def _latest_telegram_task_for_status(self, message: Any):
        queue, active = self._active_telegram_task(message)
        if active is not None:
            return queue, active
        chat_id = int(
            (getattr(message, "reply_context", {}) or {}).get("chat_id", 0) or 0
        )
        if not chat_id:
            return queue, None
        from .task_state import RemoteTaskQueue, TaskStateStore
        queue = queue or RemoteTaskQueue(TaskStateStore(remote_tasks_path(self.root)))
        with queue.store.process_lock():
            queue.store.load()
            candidates = [
                task for task in queue.store.all()
                if str((task.metadata or {}).get("transport", "")).upper() == "TELEGRAM"
                and int((task.reply_route or {}).get("chat_id", 0) or 0) == chat_id
            ]
        latest = max(candidates, key=lambda task: (task.created_at, task.task_id)) if candidates else None
        return queue, latest

    def _run_task_progress_control(
        self, message: Any, task_id: str
    ) -> dict[str, Any]:
        """Return one task's ledger without accessing the browser session."""
        from .task_progress import format_telegram_status_view, read_progress
        from .task_state import RemoteTaskQueue, TaskStateStore

        chat_id = int(
            (getattr(message, "reply_context", {}) or {}).get("chat_id", 0) or 0
        )
        queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(self.root)))
        with queue.store.process_lock():
            queue.store.load()
            task = queue.store.get(task_id)
        if (
            task is None
            or str((task.metadata or {}).get("transport", "")).upper() != "TELEGRAM"
            or int((task.reply_route or {}).get("chat_id", 0) or 0) != chat_id
        ):
            return {"message": "找不到這個對話可查詢的任務進度。"}

        ledger = read_progress(task_id, root=self.root)
        if ledger is None:
            state = str(getattr(task.state, "value", task.state))
            terminal = state in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
            note = (
                "任務已結束，暫存 Progress 紀錄已清除。"
                if terminal
                else "模型尚未寫入第一筆可讀取的 Progress。"
            )
            return {
                "message": (
                    f"{note}\nrequest_id: {task.request_id}\n"
                    f"task_id: {task.task_id}\ntask_state: {state}"
                ),
                "generation_active": not terminal,
                "status_source": "task_progress_ledger",
            }

        state = str(getattr(task.state, "value", task.state))
        return {
            "message": (
                format_telegram_status_view(ledger)
                + f"\n\nrequest_id: {task.request_id}"
                + f"\ntask_id: {task.task_id}\ntask_state: {state}"
            ),
            "generation_active": ledger.runtime_state == "PROCESSING",
            "status_source": "task_progress_ledger",
        }

    @staticmethod
    def _format_status_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remain = divmod(seconds, 3600)
        minutes, secs = divmod(remain, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

    def _run_status_control(self, message: Any) -> dict[str, Any]:
        import time
        _queue, task = self._latest_telegram_task_for_status(message)
        execution = self._execution_status_for_task(task)
        if task is not None:
            from .task_progress import format_telegram_status_view, read_progress
            progress = read_progress(task.task_id, root=self.root)
            if progress is not None:
                task_state = getattr(getattr(task, "state", ""), "value", getattr(task, "state", ""))
                text = format_telegram_status_view(progress)
                text += f"\n\nTask\nID: {task.task_id}\nState: {task_state}"
                if execution:
                    started = float(execution.get("started_at", 0) or 0)
                    text += f"\n\nCMD\nState: {execution.get('state', 'UNKNOWN')}"
                    if execution.get("pid"):
                        text += f"\nPID: {execution['pid']}"
                    elif execution.get("worker_pid"):
                        text += f"\nWorker PID: {execution['worker_pid']}"
                    if started:
                        text += f"\nElapsed: {self._format_status_duration(time.time() - started)}"
                names = self._latest_telegram_attachment_names(message)
                if names:
                    text += "\n\nTelegram 附件：" + "、".join(names)
                return {
                    "message": text,
                    "generation_active": progress.runtime_state == "PROCESSING",
                    "status_source": "task_progress_ledger",
                }
        result = self._run_browser_control("查看現在工作狀態")
        browser_available = "last_message" in result
        active = bool(result.get("generation_active", False)) if browser_available else False

        if browser_available:
            last_message = str(result.get("last_message", "") or "").strip()
            role = str(result.get("last_role", "") or "").strip().lower()
            if not last_message:
                text = "目前 WebGPT 對話窗內還沒有可回報的訊息。"
            elif role == "assistant":
                state = "目前仍在產生回覆" if active else "已完成回覆"
                text = f"目前 WebGPT {state}，最後一則可見訊息如下：\n\n{last_message}"
            elif role == "user":
                text = f"目前對話窗最後一則訊息是送給 WebGPT 的內容：\n\n{last_message}"
            else:
                text = f"目前對話窗最後一則可見訊息如下：\n\n{last_message}"
        else:
            unavailable = "目前沒有可操作的 WebGPT browser session" in str(result.get("message", "") or "")
            if task is None and not execution:
                if unavailable:
                    return {"message": "目前沒有開啟中的 WebGPT 對話窗，因此沒有可回報的工作訊息。"}
                return result
            text = "WebGPT：目前沒有可操作的 browser session。" if unavailable else "WebGPT：目前無法取得 browser 狀態。"

        if task is not None:
            task_state = getattr(getattr(task, "state", ""), "value", getattr(task, "state", ""))
            text += f"\n\nTask\nID: {task.task_id}\nState: {task_state}"

        if execution:
            now = time.time()
            started = float(execution.get("started_at", 0) or 0)
            last_output = float(execution.get("last_output_at", started) or started)
            text += f"\n\nCMD\nState: {execution.get('state', 'UNKNOWN')}"
            if execution.get("pid"):
                text += f"\nPID: {execution['pid']}"
            elif execution.get("worker_pid"):
                text += f"\nWorker PID: {execution['worker_pid']}"
            if started:
                text += f"\nElapsed: {self._format_status_duration(now - started)}"
            if last_output:
                text += f"\nIdle for: {self._format_status_duration(now - last_output)}"
            if execution.get("exit_code") is not None:
                text += f"\nExit code: {execution['exit_code']}"
            command = str(execution.get("command", "") or "").strip()
            if command:
                text += "\nCommand: " + command[:500]
            stdout_tail = str(execution.get("stdout_tail", "") or "").strip()
            stderr_tail = str(execution.get("stderr_tail", "") or "").strip()
            tail = stdout_tail
            if stderr_tail:
                tail += ("\n" if tail else "") + "[stderr]\n" + stderr_tail
            if tail:
                text += "\nLast output:\n" + tail[-8192:]

        names = self._latest_telegram_attachment_names(message)
        if names:
            text += (
                "\n\n這項工作在 Telegram 中附有檔案："
                + "、".join(names)
                + "。這次只回報檔名，不會重新上傳檔案。"
            )
        return {"message": text, "generation_active": active}

    def _run_queued_control(self, command: str, message: Any) -> dict[str, Any]:
        try:
            source_id = str(getattr(message, "source_message_id", "") or "")
            request = self.control_plane.submit(
                command, source_message_id=source_id
            )
            self.runtime_log.write(
                "CONNECT",
                component="remote_control_dispatcher",
                stage="CONTROL_RECEIVED",
                control=command,
                control_id=request["request_id"],
            )
            terminal = self.control_plane.wait(
                request["request_id"], timeout_sec=75.0
            )
            if str(terminal.get("status", "")).upper() == "DONE":
                return dict(terminal.get("result") or {})
            return {
                "message": "RemoteAgent control failed: "
                + str(terminal.get("error", "unknown"))[:500]
            }
        except Exception as exc:
            return {
                "message": f"RemoteAgent control failed: {type(exc).__name__}: {exc}"
            }
