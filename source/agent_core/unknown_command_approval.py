"""One-action, human-controlled execution approval for UNKNOWN commands.

This is a *second* gate after ordinary scope/schema checks. The approval
does not grant filesystem/network permission or waive command_security.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Mapping

from .security_approval import SecurityApprovalLedger, SecurityApprovalError

APPROVAL_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 0.5


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def request_unknown_command_approval(loop, action: Mapping, *,
                                     timeout_sec: float = APPROVAL_TIMEOUT_SECONDS,
                                     clock=None, sleep=None) -> dict:
    """Return a non-authorizing decision receipt; never execute the action.

    Successful approval is consumed once and bound to exact command, action,
    request, task, working directory and original Workspace. On timeout,
    preserve only an audit/continuation reference, never a durable permission.
    """
    clock = clock or time.time
    sleep = sleep or time.sleep
    tools = loop.tools
    command = str(action.get("command") or "")
    action_id = str(action.get("action_id") or "")
    root = str(getattr(tools, "workspace_root", "") or "")
    chat = str(getattr(tools, "security_approval_chat_id", "") or "")
    request_id = str(loop.run_id or "")
    task_id = str(loop.task_id or "")
    cwd = str(action.get("working_directory") or root)
    digest = _digest({
        "task_id": task_id, "request_id": request_id,
        "task_epoch": str(loop.task_epoch or ""), "action": dict(action),
        "cwd": cwd, "workspace": root,
    })
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    manifest = {
        "schema": "SMARTAGENT_UNKNOWN_COMMAND_APPROVAL_V1",
        "approval_kind": "UNKNOWN_COMMAND",
        "command": command, "command_sha256": command_sha256,
        "cwd": cwd, "target": cwd, "risk_class": "UNKNOWN",
        "timeout_sec": int(timeout_sec),
    }
    manifest["manifest_digest"] = _digest(manifest)
    binding = {
        "request_id": request_id, "task_id": task_id,
        "action_id": action_id, "action_digest": digest,
        "target": cwd, "manifest_digest": manifest["manifest_digest"],
        "chat_id": chat, "permanent_scope": "",
        "workspace_root": root, "approval_kind": "UNKNOWN_COMMAND",
    }
    if not all((request_id, task_id, action_id, root)):
        return {
            "state": "UNAVAILABLE", "reason": "approval_identity_missing",
            "approval_id": "", "command_sha256": command_sha256,
        }
    notifier = getattr(tools, "_security_approval_notifier", None)
    if not callable(notifier) or not chat:
        return {
            "state": "UNAVAILABLE", "reason": "human_approval_channel_unavailable",
            "approval_id": "", "command_sha256": command_sha256,
        }
    ledger = SecurityApprovalLedger(
        getattr(tools, "security_approval_ledger_root", root)
    )
    record = ledger.request(binding, ttl_sec=timeout_sec)
    approval_id = record["approval_id"]
    try:
        # For Telegram this emits the existing inline approve/reject buttons.
        tools.notify_security_approval(record, manifest)
    except Exception as exc:
        return {
            "state": "UNAVAILABLE", "reason": "approval_notification_failed:"
            + type(exc).__name__, "approval_id": approval_id,
            "command_sha256": command_sha256,
        }
    deadline = min(float(record["expires_at"]), clock() + timeout_sec)
    while clock() < deadline:
        cancel = getattr(tools, "security_approval_cancel_check", None)
        if callable(cancel):
            cancel()
        current = ledger.get(approval_id)
        state = str((current or {}).get("state", ""))
        if state == "APPROVED":
            # The ledger checks expiry and all binding fields atomically.
            try:
                ledger.consume(approval_id, binding)
            except SecurityApprovalError as exc:
                return {
                    "state": "DENIED", "reason": str(exc),
                    "approval_id": approval_id, "command_sha256": command_sha256,
                }
            return {
                "state": "APPROVED", "reason": "approved_once_consumed",
                "approval_id": approval_id, "command_sha256": command_sha256,
            }
        if state in {"REJECTED", "CANCELLED", "EXPIRED", "CONSUMED"}:
            return {
                "state": "DENIED", "reason": "approval_state_" + state.lower(),
                "approval_id": approval_id, "command_sha256": command_sha256,
            }
        sleep(min(POLL_INTERVAL_SECONDS, max(0., deadline - clock())))
    return {
        "state": "TIMEOUT", "reason": "human_approval_timeout_600s",
        "approval_id": approval_id, "command_sha256": command_sha256,
    }


__all__ = ["APPROVAL_TIMEOUT_SECONDS", "request_unknown_command_approval"]
