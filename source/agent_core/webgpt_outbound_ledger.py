#!/usr/bin/env python3
"""Durable provenance for user turns submitted by local WebGPT automation."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .conversation_identity import conversation_id
from .process_file_lock import exclusive_process_lock
from .paths import source_root, webgpt_outbound_turns_path


ROOT = source_root()
DEFAULT_PATH = webgpt_outbound_turns_path()
_ATTACHMENT_LABELS = {"檔案", "文件", "file", "attachment", "顯示更多", "显示更多", "show more"}
_ATTACHMENT_NAME_RE = re.compile(
    r"^[^\r\n\\/]+\.(?:json|md|txt|csv|log|zip|pdf|docx?|xlsx?)$",
    re.IGNORECASE,
)
_PROTOCOL_PREFIXES = ("[REMOTE_AGENT_", "[WEBAGENT_", "[SMARTAGENT_", "[AGENT_")


def _normalized_lines(text: object) -> list[str]:
    value = str(text or "").replace("\ufeff", "").replace("\u200b", "")
    return [line.strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def protocol_payload_view(text: object) -> str:
    """Remove browser-only attachment chrome before fingerprinting a prompt."""
    lines = _normalized_lines(text)
    marker = next(
        (index for index, line in enumerate(lines) if line.startswith(_PROTOCOL_PREFIXES)),
        None,
    )
    if marker is not None:
        prelude = [line for line in lines[:marker] if line]
        if not prelude or all(
            line.casefold() in _ATTACHMENT_LABELS
            or _ATTACHMENT_NAME_RE.fullmatch(line)
            for line in prelude
        ):
            lines = lines[marker:]
    return "\n".join(lines).strip()


def content_fingerprint(text: object) -> str:
    return hashlib.sha256(protocol_payload_view(text).encode("utf-8", errors="replace")).hexdigest()


def record_outbound_turn(
    conversation_url: str,
    turn_index: int,
    text: object,
    *,
    path: Path = DEFAULT_PATH,
) -> None:
    """Record only identity metadata; prompt content is never persisted."""
    row = {
        "version": 1,
        "conversation_id": conversation_id(str(conversation_url or "")),
        "turn_index": int(turn_index),
        "content_sha256": content_fingerprint(text),
        "sent_at": time.time(),
    }
    if not row["conversation_id"] or not protocol_payload_view(text):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with exclusive_process_lock(lock_path, timeout_sec=2.0, label="WebGPT outbound ledger"):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        # Provenance is a defense in depth. A ledger I/O failure must not turn a
        # confirmed browser submit into an ambiguous request failure.
        return


def is_recorded_outbound_turn(
    conversation_url: str,
    turn_index: int,
    text: object,
    *,
    path: Path = DEFAULT_PATH,
    max_age_sec: float = 86400.0,
) -> bool:
    cid = conversation_id(str(conversation_url or ""))
    digest = content_fingerprint(text)
    try:
        rows = path.read_text(encoding="utf-8").splitlines()[-2048:]
    except (OSError, UnicodeError):
        return False
    now = time.time()
    for raw in reversed(rows):
        try:
            row = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            str(row.get("conversation_id", "")) == cid
            and int(row.get("turn_index", -1)) == int(turn_index)
            and str(row.get("content_sha256", "")) == digest
            and 0.0 <= now - float(row.get("sent_at", 0.0) or 0.0) <= float(max_age_sec)
        ):
            return True
    return False


__all__ = [
    "content_fingerprint", "is_recorded_outbound_turn", "protocol_payload_view",
    "record_outbound_turn",
]
