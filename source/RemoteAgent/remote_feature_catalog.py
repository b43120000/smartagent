#!/usr/bin/env python3
from __future__ import annotations

from agent_core.remote_binding_manager import LIST_SKILLS_COMMAND, MANAGER_COMMAND

FEATURE_QUERY_COMMAND = "查詢遠端功能"
INTERRUPT_COMMAND = "任務中斷"
STATUS_COMMAND = "查看現在工作狀態"
SNAPSHOT_COMMAND = "snapshot webgpt"
SNAPSHOT_COMMAND_ALIASES = frozenset({
    SNAPSHOT_COMMAND,
    "web 截圖回傳",
    "web截圖回傳",
})


def canonical_remote_control(raw: str) -> str:
    value = " ".join(str(raw or "").strip().casefold().split())
    return SNAPSHOT_COMMAND if value in SNAPSHOT_COMMAND_ALIASES else value

REMOTE_FEATURES = (
    (1, "訊號接受器", "已實作"),
    (2, MANAGER_COMMAND, "已實作"),
    (3, "Web 截圖回傳", "已實作"),
    (4, "重新整理", "已實作"),
    (5, "接續上一個工作或指定 ACK ID 工作", "已實作"),
    (6, STATUS_COMMAND, "已實作"),
    (7, INTERRUPT_COMMAND, "已實作"),
    (8, "重啟連線", "已實作"),
    (9, LIST_SKILLS_COMMAND, "已實作"),
)

# These controls already have deterministic software-owned execution paths.
# Keep callback payloads short and opaque; Telegram limits callback_data to
# 64 bytes and the receiver must never execute arbitrary callback text.
REMOTE_CONTROL_CALLBACK_PREFIX = "remote_control:"
SECURITY_APPROVAL_CALLBACK_PREFIX = "security_approval:"
SECURITY_REJECT_CALLBACK_PREFIX = "security_reject:"
SECURITY_WORKSPACE_DELETE_CALLBACK_PREFIX = "security_delete_workspace:"
TASK_PROGRESS_CALLBACK_PREFIX = "task_progress:"
REMOTE_CONTROL_BUTTONS = (
    (MANAGER_COMMAND, MANAGER_COMMAND, "binding_manager"),
    (LIST_SKILLS_COMMAND, LIST_SKILLS_COMMAND, "list_skills"),
    ("Web 截圖回傳", SNAPSHOT_COMMAND, "snapshot"),
    ("重新整理", "重新整理", "refresh"),
    (STATUS_COMMAND, STATUS_COMMAND, "status"),
    (INTERRUPT_COMMAND, INTERRUPT_COMMAND, "interrupt"),
    ("重啟連線", "重啟連線", "reconnect"),
)


def remote_feature_reply_keyboard() -> dict:
    """Persistent bottom keyboard that opens the remote feature menu."""
    return {
        "keyboard": [[{"text": FEATURE_QUERY_COMMAND}]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "輸入工作，或查詢遠端功能",
    }


def remote_feature_inline_keyboard() -> dict:
    """Inline controls shown under the feature catalogue response."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": label,
                    "callback_data": REMOTE_CONTROL_CALLBACK_PREFIX + callback,
                }
            ]
            for label, _command, callback in REMOTE_CONTROL_BUTTONS
        ]
    }


def task_progress_inline_keyboard(task_id: str) -> dict:
    """Task-scoped progress button for running/progress status bubbles."""
    value = str(task_id or "").strip().upper()
    if not value.startswith("TASK-") or len(value) > 48:
        raise ValueError("invalid_task_progress_callback_id")
    return {
        "inline_keyboard": [[{
            "text": "檢查當前進度",
            "callback_data": TASK_PROGRESS_CALLBACK_PREFIX + value,
        }]]
    }


def command_for_remote_callback(data: str) -> str | None:
    value = str(data or "").strip()
    if value.startswith(SECURITY_APPROVAL_CALLBACK_PREFIX):
        return "security_approve_once " + value[len(SECURITY_APPROVAL_CALLBACK_PREFIX):]
    if value.startswith(SECURITY_REJECT_CALLBACK_PREFIX):
        return "security_reject_once " + value[len(SECURITY_REJECT_CALLBACK_PREFIX):]
    if value.startswith(SECURITY_WORKSPACE_DELETE_CALLBACK_PREFIX):
        return "security_workspace_delete " + value[len(SECURITY_WORKSPACE_DELETE_CALLBACK_PREFIX):]
    if value.startswith(TASK_PROGRESS_CALLBACK_PREFIX):
        task_id = value[len(TASK_PROGRESS_CALLBACK_PREFIX):].strip().upper()
        if task_id.startswith("TASK-") and len(task_id) <= 48:
            return "查看任務進度 " + task_id
        return None
    if not value.startswith(REMOTE_CONTROL_CALLBACK_PREFIX):
        return None
    callback = value[len(REMOTE_CONTROL_CALLBACK_PREFIX):]
    return next(
        (command for _label, command, key in REMOTE_CONTROL_BUTTONS if key == callback),
        None,
    )


def security_approval_inline_keyboard(approval_id: str, *, allow_permanent: bool = False, approval_kind: str = "DELETE") -> dict:
    value = str(approval_id or "")
    kind = str(approval_kind or "DELETE").upper()
    approve_label = "允許執行一次" if kind == "EXECUTION" else "確認刪除一次"
    rows = [[
        {"text": approve_label, "callback_data": SECURITY_APPROVAL_CALLBACK_PREFIX + value},
        {"text": "拒絕", "callback_data": SECURITY_REJECT_CALLBACK_PREFIX + value},
    ]]
    if allow_permanent and kind == "DELETE":
        rows.append([{
            "text": "永久允許此 Workspace 刪除",
            "callback_data": SECURITY_WORKSPACE_DELETE_CALLBACK_PREFIX + value,
        }])
    return {"inline_keyboard": rows}


def render_remote_feature_list() -> str:
    lines = ["RemoteAgent 遠端功能："]
    lines.extend(f"{index}. {name}【{status}】" for index, name, status in REMOTE_FEATURES)
    return "\n".join(lines)


__all__ = [
    "FEATURE_QUERY_COMMAND", "INTERRUPT_COMMAND", "LIST_SKILLS_COMMAND",
    "MANAGER_COMMAND", "REMOTE_CONTROL_BUTTONS",
    "REMOTE_CONTROL_CALLBACK_PREFIX", "REMOTE_FEATURES", "SNAPSHOT_COMMAND",
    "STATUS_COMMAND", "SECURITY_APPROVAL_CALLBACK_PREFIX", "SECURITY_REJECT_CALLBACK_PREFIX",
    "SECURITY_WORKSPACE_DELETE_CALLBACK_PREFIX",
    "TASK_PROGRESS_CALLBACK_PREFIX",
    "SNAPSHOT_COMMAND_ALIASES", "canonical_remote_control",
    "command_for_remote_callback", "remote_feature_inline_keyboard",
    "remote_feature_reply_keyboard", "render_remote_feature_list", "security_approval_inline_keyboard",
    "task_progress_inline_keyboard",
]
