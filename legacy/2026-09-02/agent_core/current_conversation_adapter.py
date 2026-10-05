#!/usr/bin/env python3
from __future__ import annotations
import hashlib
import re
import time
from dataclasses import dataclass

from .transport_message import NormalizedInboundMessage

WEBGPT_COPILOT_SOURCE = "WEBGPT_COPILOT"
_INTERNAL_WEBGPT_PREFIXES = (
    "[AGENT_", "[SMARTAGENT", "[REMOTE_AGENT_", "RUN_ID=", "RESULT_ID=",
)

@dataclass(frozen=True)
class ConversationTaskRequest:
    request: str
    source: str
    conversation_url: str
    conversation_title: str = ""


def is_internal_webcopilot_turn(text: str) -> bool:
    value = str(text or "").lstrip()
    return not value or value.startswith(_INTERNAL_WEBGPT_PREFIXES)


def extract_webcopilot_request(text: str, *, allow_plain: bool = False) -> str:
    if is_internal_webcopilot_turn(text):
        return ""
    match = re.match(r"(?is)^\s*webcopilot\b[\s:：,，-]*(.*)$", str(text or ""))
    if match:
        return match.group(1).strip()
    return str(text or "").strip() if allow_plain else ""


def build_conversation_task(text: str, *, conversation_url: str, conversation_title: str = "") -> ConversationTaskRequest:
    request = extract_webcopilot_request(text)
    if not request:
        raise ValueError("empty_webcopilot_request")
    return ConversationTaskRequest(
        request=request,
        source=WEBGPT_COPILOT_SOURCE,
        conversation_url=str(conversation_url or ""),
        conversation_title=str(conversation_title or ""),
    )


def build_webcopilot_message(
    text: str,
    *,
    conversation_url: str,
    turn_index: int,
    conversation_title: str = "",
    received_at: float | None = None,
    allow_plain: bool = False,
) -> NormalizedInboundMessage:
    """Adapt one WebCopilot user turn to the shared Agent ingress contract."""
    request = extract_webcopilot_request(text, allow_plain=allow_plain)
    if not request:
        raise ValueError("empty_webcopilot_request")
    content_fingerprint = hashlib.sha256(
        request.encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    source_message_id = f"user-turn:{int(turn_index)}:{content_fingerprint}"
    identity = hashlib.sha256(
        f"WEBGPT_COPILOT|chatgpt.com|{conversation_url}|{source_message_id}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()
    return NormalizedInboundMessage(
        transport=WEBGPT_COPILOT_SOURCE,
        endpoint="chatgpt.com",
        conversation_key=str(conversation_url or ""),
        sender_id="local_web_user",
        source_message_id=source_message_id,
        text=request,
        received_at=float(time.time() if received_at is None else received_at),
        idempotency_key=identity,
        reply_context={"conversation_url": str(conversation_url or "")},
        metadata={
            "request_id": f"WEBCOPILOT-{int(turn_index)}",
            "turn_index": int(turn_index),
            "raw_user_text": str(text or "")[:4000],
            "conversation_title": str(conversation_title or ""),
        },
    )

__all__ = [
    "WEBGPT_COPILOT_SOURCE", "ConversationTaskRequest", "extract_webcopilot_request",
    "is_internal_webcopilot_turn", "build_conversation_task", "build_webcopilot_message",
]
