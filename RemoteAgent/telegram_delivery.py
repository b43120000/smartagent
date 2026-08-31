#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

"""Render transport-neutral RemoteEvents for a Telegram private chat."""


class TelegramDeliveryAdapter:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def render(event: dict) -> str:
        event_id = str(event.get("event_id", ""))
        event_type = str(event.get("event_type", ""))
        request_id = str(event.get("request_id", ""))
        task_id = str(event.get("task_id", ""))
        status = str(event.get("status", ""))
        payload = dict(event.get("payload") or {})
        if event_type == "TASK_ACCEPTED":
            return f"🟡 已接受\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        if event_type == "TASK_STARTED":
            return f"🔵 執行中\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        if event_type == "TASK_COMPLETED":
            return f"🟢 已完成\n{str(payload.get('summary', ''))}\nevent_id: {event_id}\nrequest_id: {request_id}"
        if event_type == "TASK_FAILED":
            return f"🔴 執行失敗\n{str(payload.get('error', ''))}\nevent_id: {event_id}\nrequest_id: {request_id}"
        if event_type == "TASK_INTERRUPTED":
            return f"🟠 任務中斷\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        return f"RemoteAgent {event_type} status={status}\nrequest_id: {request_id}"

    def deliver_event(self, route: dict, event: dict) -> dict:
        if str((route or {}).get("transport", "")).upper() != "TELEGRAM":
            return {"delivered": False, "reason": "telegram_wrong_transport"}
        try:
            chat_id = int((route or {}).get("chat_id", 0) or 0)
        except (TypeError, ValueError):
            chat_id = 0
        if not chat_id:
            return {"delivered": False, "reason": "missing_chat_id"}
        try:
            payload = dict(event.get("payload") or {})
            artifacts = list(payload.get("artifacts") or [])
            prepared = []
            for row in artifacts:
                if not isinstance(row, dict): raise ValueError("telegram_artifact_invalid")
                workspace = Path(str(row.get("workspace") or payload.get("workspace") or "")).resolve()
                if not workspace.is_dir(): raise ValueError("telegram_artifact_workspace_unavailable")
                raw = Path(str(row.get("path") or "")); target = (raw if raw.is_absolute() else workspace / raw).resolve(); target.relative_to(workspace)
                if not target.is_file(): raise FileNotFoundError(str(target))
                prepared.append((target, str(row.get("kind") or "document").lower(), str(row.get("caption") or "")))
            reply_to = int((route or {}).get("message_id", 0) or 0) or None
            result = self.client.send_message(chat_id, self.render(event), reply_to_message_id=reply_to)
            replies = [str(result.get("message_id", ""))]
            for target, kind, caption in prepared:
                sent = self.client.send_photo(chat_id, target, caption=caption) if kind in {"photo", "image"} else self.client.send_document(chat_id, target, caption=caption)
                replies.append(str(sent.get("message_id", "")))
            return {"delivered": True, "reply": ",".join(x for x in replies if x), "artifact_count": len(prepared)}
        except Exception as exc:
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}
