"""Shared text/result budgets for every SmartAgent WebGPT transport."""
from __future__ import annotations

import os
import re


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


INLINE_RESULT_MAX_BYTES = _positive_env("SMARTAGENT_INLINE_RESULT_MAX_BYTES", 32 * 1024)
ROUND_INLINE_MAX_BYTES = _positive_env("SMARTAGENT_ROUND_INLINE_MAX_BYTES", 128 * 1024)
PROTOCOL_RESPONSE_MAX_BYTES = _positive_env("SMARTAGENT_PROTOCOL_RESPONSE_MAX_BYTES", 32 * 1024)
WEBGPT_PROMPT_HARD_MAX_BYTES = _positive_env("SMARTAGENT_WEBGPT_PROMPT_HARD_MAX_BYTES", 512 * 1024)
RESULT_ATTACHMENT_MAX_BYTES = _positive_env("SMARTAGENT_RESULT_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)
RESULT_PREVIEW_BYTES = _positive_env("SMARTAGENT_RESULT_PREVIEW_BYTES", 8 * 1024)


class PromptBudgetExceeded(ValueError):
    def __init__(self, *, size_bytes: int, limit_bytes: int):
        self.size_bytes = int(size_bytes)
        self.limit_bytes = int(limit_bytes)
        super().__init__(
            "WEB_PROMPT_BUDGET_EXCEEDED "
            f"prompt_bytes={self.size_bytes} hard_limit={self.limit_bytes} "
            "recommended_transport=JSON_ATTACHMENT"
        )


def utf8_size(value: object) -> int:
    return len(str(value or "").encode("utf-8", errors="replace"))


_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|telegram[_-]?token|password)\s*[:=]\s*[^\s,;]{8,}", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,})\b"),
)


def contains_sensitive_text(value: object) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def is_low_value_bulk(tool_call: dict, result: object) -> bool:
    if str(tool_call.get("tool", "")) != "run_command":
        return False
    if utf8_size(result) < 256 * 1024:
        return False
    command = str(tool_call.get("command", "") or "").lower().replace("/", "\\")
    broad_search = r"findstr \s" in command or "rg " in command
    agent_state = ".agents\\" in command or "--hidden" in command
    return bool(broad_search and agent_state)


def bounded_preview(value: object, max_bytes: int = RESULT_PREVIEW_BYTES) -> str:
    raw = str(value or "").encode("utf-8", errors="replace")
    limit = max(256, int(max_bytes))
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace")
    half = limit // 2
    head = raw[:half].decode("utf-8", errors="replace")
    tail = raw[-half:].decode("utf-8", errors="replace")
    return head + "\n...[payload preview truncated]...\n" + tail


def ensure_webgpt_prompt_budget(prompt: object) -> int:
    size = utf8_size(prompt)
    if size > WEBGPT_PROMPT_HARD_MAX_BYTES:
        raise PromptBudgetExceeded(size_bytes=size, limit_bytes=WEBGPT_PROMPT_HARD_MAX_BYTES)
    return size


__all__ = [
    "INLINE_RESULT_MAX_BYTES", "ROUND_INLINE_MAX_BYTES", "PROTOCOL_RESPONSE_MAX_BYTES",
    "WEBGPT_PROMPT_HARD_MAX_BYTES", "RESULT_ATTACHMENT_MAX_BYTES",
    "PromptBudgetExceeded", "utf8_size", "contains_sensitive_text",
    "is_low_value_bulk", "bounded_preview", "ensure_webgpt_prompt_budget",
]
