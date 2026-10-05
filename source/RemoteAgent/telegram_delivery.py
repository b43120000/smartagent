#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from RemoteAgent.telegram_artifacts import prepare_telegram_completion
from RemoteAgent.remote_feature_catalog import (
    security_approval_inline_keyboard,
    task_progress_inline_keyboard,
)

"""Render transport-neutral RemoteEvents for a Telegram private chat."""


TELEGRAM_TEXT_LIMIT_UTF16 = 4096
TELEGRAM_CHUNK_BODY_LIMIT_UTF16 = 3800


def _utf16_units(value: str) -> int:
    """Telegram measures text limits in UTF-16 code units."""
    return len(str(value).encode("utf-16-le")) // 2


def _max_prefix_index(value: str, unit_limit: int) -> int:
    used = 0
    for index, char in enumerate(value):
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > unit_limit:
            return index
        used += width
    return len(value)


def split_telegram_text(
    value: str, *, body_limit: int = TELEGRAM_CHUNK_BODY_LIMIT_UTF16
) -> list[str]:
    """Split long text at readable boundaries before a final hard boundary."""
    remaining = str(value or "")
    if _utf16_units(remaining) <= TELEGRAM_TEXT_LIMIT_UTF16:
        return [remaining]

    chunks: list[str] = []
    body_limit = max(256, min(int(body_limit), TELEGRAM_TEXT_LIMIT_UTF16 - 128))
    while _utf16_units(remaining) > body_limit:
        hard_index = _max_prefix_index(remaining, body_limit)
        window = remaining[:hard_index]
        minimum = max(1, hard_index // 2)
        cut = 0

        # Prefer complete paragraphs, then lines, sentences, and words.  A
        # single pathological line is the only case that reaches a hard cut.
        paragraph = window.rfind("\n\n", minimum)
        if paragraph >= 0:
            cut = paragraph + 2
        if not cut:
            line = window.rfind("\n", minimum)
            if line >= 0:
                cut = line + 1
        if not cut:
            sentence = max(window.rfind(mark, minimum) for mark in "。！？；.!?;")
            if sentence >= 0:
                cut = sentence + 1
        if not cut:
            word = max(window.rfind(" ", minimum), window.rfind("\t", minimum))
            if word >= 0:
                cut = word + 1
        if not cut:
            cut = hard_index

        chunks.append(remaining[:cut].rstrip("\r\n"))
        remaining = remaining[cut:].lstrip("\r\n")

    if remaining or not chunks:
        chunks.append(remaining)

    total = len(chunks)
    output = [f"📄 長文分段 {index}/{total}\n{chunk}" for index, chunk in enumerate(chunks, 1)]
    if any(_utf16_units(chunk) > TELEGRAM_TEXT_LIMIT_UTF16 for chunk in output):
        raise RuntimeError("telegram_chunk_limit_exceeded")
    return output


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
            return f"🟡 已接受（等待執行）\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        if event_type == "TASK_STARTED":
            return f"🔵 執行中\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        if event_type == "TASK_PROGRESS":
            summary = str(payload.get("summary", "") or "").strip()
            return (
                f"🔵 最新任務進度\n{summary}\n"
                f"event_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
            )
        if event_type == "TASK_COMPLETED":
            return f"🟢 已完成\n{str(payload.get('summary', ''))}\nevent_id: {event_id}\nrequest_id: {request_id}"
        if event_type == "TASK_FAILED":
            return f"🔴 執行失敗\n{str(payload.get('error', ''))}\nevent_id: {event_id}\nrequest_id: {request_id}"
        if event_type == "TASK_INTERRUPTED":
            return f"🟠 任務中斷\nevent_id: {event_id}\nrequest_id: {request_id}\ntask_id: {task_id}"
        if event_type == "SECURITY_CONFIRMATION_REQUIRED":
            kind = str(payload.get("approval_kind", "DELETE") or "DELETE").upper()
            if kind == "EXECUTION":
                return (
                    "🔐 需要允許執行一次\n"
                    f"程式：{payload.get('target', '')}\n"
                    f"SHA-256：{payload.get('executable_sha256', '')}\n"
                    f"工作目錄：{payload.get('cwd', '')}\n"
                    f"指令：{payload.get('command', '')}\n"
                    "此批准只可使用一次；指令、路徑、SHA-256 或工作目錄改變都會失效。\n"
                    f"approval_id: {payload.get('approval_id', '')}\n"
                    f"request_id: {request_id}\ntask_id: {task_id}"
                )
            permanent_scope = str(payload.get("permanent_scope", "") or "")
            scope_note = (f"永久允許範圍：{permanent_scope}\n只授權此第二層 Workspace，不授權其父層。\n" if permanent_scope else "")
            return (
                "🔐 需要確認刪除\n"
                f"目標：{payload.get('target', '')}\n"
                f"項目數：{payload.get('entry_count', 0)}\n"
                f"大小：{payload.get('total_bytes', 0)} bytes\n"
                "只有此對話可批准；路徑或內容改變後批准會失效。\n"
                f"{scope_note}"
                f"approval_id: {payload.get('approval_id', '')}\n"
                f"request_id: {request_id}\ntask_id: {task_id}"
            )
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
            if str(event.get("event_type", "")) == "TASK_COMPLETED":
                payload = prepare_telegram_completion(
                    request="",
                    summary=str(payload.get("summary", "")),
                    workspace=str(payload.get("workspace") or "."),
                    artifacts=list(payload.get("artifacts") or []),
                )
                event = dict(event)
                event["payload"] = payload
            artifacts = list(payload.get("artifacts") or [])
            prepared = []
            for row in artifacts:
                if not isinstance(row, dict): raise ValueError("telegram_artifact_invalid")
                workspace = Path(str(row.get("workspace") or payload.get("workspace") or "")).resolve()
                if not workspace.is_dir(): raise ValueError("telegram_artifact_workspace_unavailable")
                raw = Path(str(row.get("path") or "")); target = (raw if raw.is_absolute() else workspace / raw).resolve(); target.relative_to(workspace)
                if not target.is_file(): raise FileNotFoundError(str(target))
                prepared.append((target, str(row.get("kind") or "document").lower(), str(row.get("caption") or "")))
            # Every lifecycle event must stay attached to the Telegram message
            # that created this task.  reply_to_message_id describes what the
            # user message itself replied to and is not our delivery anchor.
            reply_to = int((route or {}).get("message_id", 0) or 0) or None
            replies = []
            messages = split_telegram_text(self.render(event))
            for index, text in enumerate(messages):
                markup = None
                if str(event.get("event_type", "")) == "SECURITY_CONFIRMATION_REQUIRED" and index == len(messages) - 1:
                    markup = security_approval_inline_keyboard(
                        str(payload.get("approval_id", "")),
                        allow_permanent=bool(str(payload.get("permanent_scope", "") or "").strip()),
                        approval_kind=str(payload.get("approval_kind", "DELETE") or "DELETE"),
                    )
                if str(event.get("event_type", "")) in {"TASK_STARTED", "TASK_PROGRESS"} and index == len(messages) - 1:
                    markup = task_progress_inline_keyboard(str(event.get("task_id", "")))
                result = self.client.send_message(
                    chat_id, text, reply_to_message_id=reply_to,
                    reply_markup=markup,
                )
                replies.append(str(result.get("message_id", "")))
            for target, kind, caption in prepared:
                sent = self.client.send_photo(chat_id, target, caption=caption) if kind in {"photo", "image"} else self.client.send_document(chat_id, target, caption=caption)
                replies.append(str(sent.get("message_id", "")))
            return {
                "delivered": True,
                "reply": ",".join(x for x in replies if x),
                "message_count": len(messages),
                "artifact_count": len(prepared),
            }
        except Exception as exc:
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}

    def reconcile_event(self, route: dict, event: dict) -> dict:
        """Prefer an event-id-visible duplicate over permanently losing status.

        Telegram's Bot API does not expose a reliable sent-message lookup for a
        crashed sender.  Every rendered status includes a stable event_id, so a
        stale uncertain delivery is safe to retry with visible deduplication.
        """
        del route, event
        return {
            "delivered": False,
            "retry_allowed": True,
            "reason": "telegram_delivery_outcome_unknown_retry_by_event_id",
        }


__all__ = [
    "TELEGRAM_TEXT_LIMIT_UTF16",
    "TelegramDeliveryAdapter",
    "split_telegram_text",
]
