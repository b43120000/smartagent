#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RemoteAgent control-plane protocol.

Stage 4 deliberately keeps this protocol separate from SmartAgent's execution
protocol. The RemoteAgent supervisor/control plane may parse messages produced
by this module, but it must never execute ``smartagent_tool`` envelopes.
Likewise, LocalAgent / Remote Worker continue to use ``agent_core.smartagent_protocol``
and do not treat RemoteAgent control messages as executable tools.

Transport (strict/exclusive):

```remoteagent_control
{"type":"REMOTE_AGENT_REQUEST", ...}
```

Multiple control blocks may be emitted in one response, but no prose may appear
outside the blocks. Bare JSON is intentionally not executable control traffic.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

REMOTE_FENCE = "remoteagent_control"
REMOTE_PROTOCOL_NAME = "remote_agent"
REMOTE_PROTOCOL_VERSION = 1

REMOTE_AGENT_REQUEST = "REMOTE_AGENT_REQUEST"
REMOTE_AGENT_STATUS = "REMOTE_AGENT_STATUS"
REMOTE_AGENT_QUEUED = "REMOTE_AGENT_QUEUED"
REMOTE_AGENT_WORKER_STARTED = "REMOTE_AGENT_WORKER_STARTED"
REMOTE_AGENT_COMPLETED = "REMOTE_AGENT_COMPLETED"
REMOTE_AGENT_DISCONNECTED = "REMOTE_AGENT_DISCONNECTED"
REMOTE_AGENT_FAILED = "REMOTE_AGENT_FAILED"
REMOTE_AGENT_CANCEL = "REMOTE_AGENT_CANCEL"
REMOTE_AGENT_RETRY = "REMOTE_AGENT_RETRY"

REMOTE_MESSAGE_TYPES = {
    REMOTE_AGENT_REQUEST,
    REMOTE_AGENT_STATUS,
    REMOTE_AGENT_QUEUED,
    REMOTE_AGENT_WORKER_STARTED,
    REMOTE_AGENT_COMPLETED,
    REMOTE_AGENT_DISCONNECTED,
    REMOTE_AGENT_FAILED,
    REMOTE_AGENT_CANCEL,
    REMOTE_AGENT_RETRY,
}
REMOTE_ENVELOPE_MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class RemoteProtocolDiagnostic:
    marker: str
    reason: str
    detail: str = ""
    block_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {"marker": self.marker, "reason": self.reason, "detail": self.detail}
        if self.block_index is not None:
            value["block_index"] = self.block_index
        return value


def _diag(reason: str, detail: str = "", block_index: int | None = None) -> dict[str, Any]:
    return RemoteProtocolDiagnostic(
        marker="[REMOTE_PROTOCOL_REJECTED]",
        reason=reason,
        detail=detail,
        block_index=block_index,
    ).as_dict()


def _extract_fenced_payloads(text: str) -> tuple[list[str], bool]:
    stripped = str(text or "").strip()
    if not stripped:
        return [], False
    opening = re.compile(r"```remoteagent_control(?:[ \t]+[^\r\n`]*)?[ \t]*\r?\n", re.I)
    closing = re.compile(r"\r?\n?[ \t]*```")
    payloads: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        match = opening.search(stripped, pos)
        if not match:
            break
        close = closing.search(stripped, match.end())
        if not close:
            payloads.append(stripped[match.end():].strip())
            spans.append((match.start(), len(stripped)))
            return payloads, False
        payloads.append(stripped[match.end():close.start()].strip())
        spans.append((match.start(), close.end()))
        pos = close.end()
    if not spans:
        return [], False
    residue_parts: list[str] = []
    cursor = 0
    for start, end in spans:
        residue_parts.append(stripped[cursor:start])
        cursor = end
    residue_parts.append(stripped[cursor:])
    return payloads, not bool("".join(residue_parts).strip())


def _extract_dom_payloads(text: str) -> tuple[list[str], bool]:
    stripped = str(text or "").strip()
    if not stripped:
        return [], False
    lines = stripped.splitlines()
    header = re.compile(r"^[ \t]*remoteagent_control(?:[ \t]+[^\r\n`]*)?[ \t]*$", re.I)
    indexes = [idx for idx, line in enumerate(lines) if header.fullmatch(line)]
    if not indexes:
        return [], False
    prefix = "\n".join(lines[:indexes[0]]).strip()
    payloads: list[str] = []
    for pos, idx in enumerate(indexes):
        next_idx = indexes[pos + 1] if pos + 1 < len(indexes) else len(lines)
        payloads.append("\n".join(lines[idx + 1:next_idx]).strip())
    return payloads, not bool(prefix)


def _extract_transport(text: str) -> tuple[list[str], bool, str]:
    payloads, exclusive = _extract_fenced_payloads(text)
    if payloads:
        return payloads, exclusive, "fenced"
    payloads, exclusive = _extract_dom_payloads(text)
    if payloads:
        return payloads, exclusive, "dom"
    return [], False, "none"


def _require_string(message: dict[str, Any], field: str, *, allow_empty: bool = False) -> str | None:
    value = message.get(field)
    if not isinstance(value, str):
        return f"field={field} expected=str actual={type(value).__name__}"
    if not allow_empty and not value.strip():
        return f"field={field} must be non-empty"
    return None


def validate_remote_message(message: object, raw_payload: str = "", block_index: int = 1) -> tuple[bool, dict[str, Any] | None]:
    if len(raw_payload.encode("utf-8")) > REMOTE_ENVELOPE_MAX_BYTES:
        return False, _diag("envelope_too_large", f"payload_bytes={len(raw_payload.encode('utf-8'))}", block_index)
    if not isinstance(message, dict):
        return False, _diag("envelope_not_object", f"decoded_type={type(message).__name__}", block_index)
    msg_type = message.get("type")
    if not isinstance(msg_type, str) or msg_type not in REMOTE_MESSAGE_TYPES:
        return False, _diag("unknown_message_type", f"type={msg_type!r}", block_index)
    if message.get("protocol", REMOTE_PROTOCOL_NAME) != REMOTE_PROTOCOL_NAME:
        return False, _diag("wrong_protocol", f"protocol={message.get('protocol')!r}", block_index)
    version = message.get("protocol_version", REMOTE_PROTOCOL_VERSION)
    if type(version) is not int or version != REMOTE_PROTOCOL_VERSION:
        return False, _diag("unsupported_protocol_version", f"protocol_version={version!r}", block_index)

    if msg_type == REMOTE_AGENT_REQUEST:
        for field in ("request_id", "request"):
            problem = _require_string(message, field)
            if problem:
                return False, _diag("missing_or_invalid_field", problem, block_index)
        for field in ("conversation_url", "workspace"):
            if field in message:
                problem = _require_string(message, field)
                if problem:
                    return False, _diag("invalid_optional_field", problem, block_index)
        if "origin_turn_fingerprint" in message:
            problem = _require_string(message, "origin_turn_fingerprint")
            if problem:
                return False, _diag("invalid_optional_field", problem, block_index)
        for field in ("requested_mode", "requested_carrier"):
            if field in message:
                problem = _require_string(message, field)
                if problem:
                    return False, _diag("invalid_optional_field", problem, block_index)
    elif msg_type in {REMOTE_AGENT_CANCEL, REMOTE_AGENT_RETRY}:
        problem = _require_string(message, "target_request_id")
        if problem:
            return False, _diag("missing_or_invalid_field", problem, block_index)
    else:
        for field in ("request_id", "task_id"):
            problem = _require_string(message, field)
            if problem:
                return False, _diag("missing_or_invalid_field", problem, block_index)
        if msg_type == REMOTE_AGENT_STATUS:
            problem = _require_string(message, "status")
            if problem:
                return False, _diag("missing_or_invalid_field", problem, block_index)
        if msg_type == REMOTE_AGENT_FAILED:
            problem = _require_string(message, "error")
            if problem:
                return False, _diag("missing_or_invalid_field", problem, block_index)
    return True, None


def analyze_remote_transport(text: str) -> dict[str, Any]:
    report: dict[str, Any] = {"intended": False, "transport_kind": "none", "messages": [], "diagnostics": []}
    stripped = str(text or "").strip()
    if not stripped:
        return report
    payloads, exclusive, kind = _extract_transport(stripped)
    report["transport_kind"] = kind
    if not payloads:
        # Explicitly ignore SmartAgent transport, ordinary prose and bare JSON.
        return report
    report["intended"] = True
    if not exclusive:
        report["diagnostics"].append(_diag("transport_not_exclusive", "content exists outside remoteagent_control block(s)"))
        return report
    messages: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(payloads, 1):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            diagnostics.append(_diag("json_decode_error", f"line={exc.lineno} column={exc.colno} message={exc.msg}", index))
            continue
        valid, error = validate_remote_message(decoded, raw, index)
        if not valid:
            diagnostics.append(error or _diag("validation_failed", block_index=index))
            continue
        if decoded not in messages:
            messages.append(decoded)
    report["diagnostics"] = diagnostics
    report["messages"] = [] if diagnostics else messages
    return report


def parse_remote_messages(text: str) -> list[dict[str, Any]]:
    return analyze_remote_transport(text)["messages"]


def format_remote_message(message: dict[str, Any]) -> str:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    valid, error = validate_remote_message(message, raw, 1)
    if not valid:
        raise ValueError(f"invalid RemoteAgent message: {error}")
    return f"```{REMOTE_FENCE}\n{raw}\n```"


def _base_message(msg_type: str) -> dict[str, Any]:
    return {"type": msg_type, "protocol": REMOTE_PROTOCOL_NAME, "protocol_version": REMOTE_PROTOCOL_VERSION}


def new_request(*, conversation_url: str = "", workspace: str = "", request: str, request_id: str | None = None,
                origin_turn_fingerprint: str | None = None, requested_mode: str = "",
                requested_carrier: str = "") -> dict[str, Any]:
    msg = {
        **_base_message(REMOTE_AGENT_REQUEST),
        "request_id": request_id or f"RR-{uuid.uuid4().hex[:16].upper()}",
        "request": str(request),
    }
    if conversation_url:
        msg["conversation_url"] = str(conversation_url)
    if workspace:
        msg["workspace"] = str(workspace)
    if origin_turn_fingerprint:
        msg["origin_turn_fingerprint"] = str(origin_turn_fingerprint)
    if requested_mode:
        msg["requested_mode"] = str(requested_mode)
    if requested_carrier:
        msg["requested_carrier"] = str(requested_carrier)
    valid, error = validate_remote_message(msg, json.dumps(msg, ensure_ascii=False), 1)
    if not valid:
        raise ValueError(f"invalid RemoteAgent request: {error}")
    return msg


def new_status(*, request_id: str, task_id: str, status: str, detail: str = "") -> dict[str, Any]:
    msg = {**_base_message(REMOTE_AGENT_STATUS), "request_id": str(request_id), "task_id": str(task_id), "status": str(status)}
    if detail:
        msg["detail"] = str(detail)
    return msg


def new_event(msg_type: str, *, request_id: str, task_id: str, **fields: Any) -> dict[str, Any]:
    if msg_type not in REMOTE_MESSAGE_TYPES - {REMOTE_AGENT_REQUEST, REMOTE_AGENT_STATUS}:
        raise ValueError(f"unsupported event type: {msg_type}")
    msg = {**_base_message(msg_type), "request_id": str(request_id), "task_id": str(task_id), **fields}
    valid, error = validate_remote_message(msg, json.dumps(msg, ensure_ascii=False), 1)
    if not valid:
        raise ValueError(f"invalid RemoteAgent event: {error}")
    return msg


def run_remote_protocol_self_tests() -> dict[str, Any]:
    request = new_request(request_id="RR-TEST-1", conversation_url="https://chatgpt.com/c/test", workspace="C:/workspace", request="list files")
    request_text = format_remote_message(request)
    status_text = format_remote_message(new_status(request_id="RR-TEST-1", task_id="TASK-1", status="RUNNING"))
    smartagent_text = '```smartagent_tool\n{"tool":"list_directory","action_id":"A-1","path":"C:/workspace"}\n```'
    mixed_text = "prose\n" + request_text
    results: dict[str, bool] = {
        "accept_remote_request": len(parse_remote_messages(request_text)) == 1,
        "accept_remote_status": len(parse_remote_messages(status_text)) == 1,
        "ignore_smartagent_tool": len(parse_remote_messages(smartagent_text)) == 0,
        "smartagent_not_remote_intent": analyze_remote_transport(smartagent_text)["intended"] is False,
        "ignore_plain_text": len(parse_remote_messages("hello")) == 0,
        "reject_mixed_prose": len(parse_remote_messages(mixed_text)) == 0 and any(
            d.get("reason") == "transport_not_exclusive" for d in analyze_remote_transport(mixed_text)["diagnostics"]
        ),
        "round_trip_request": parse_remote_messages(request_text)[0] == request,
    }
    results["all_passed"] = all(results.values())
    return results


__all__ = [
    "REMOTE_FENCE", "REMOTE_PROTOCOL_NAME", "REMOTE_PROTOCOL_VERSION",
    "REMOTE_AGENT_REQUEST", "REMOTE_AGENT_STATUS", "REMOTE_AGENT_QUEUED",
    "REMOTE_AGENT_WORKER_STARTED", "REMOTE_AGENT_COMPLETED", "REMOTE_AGENT_DISCONNECTED",
    "REMOTE_AGENT_FAILED", "REMOTE_MESSAGE_TYPES", "analyze_remote_transport",
    "REMOTE_AGENT_CANCEL", "REMOTE_AGENT_RETRY",
    "parse_remote_messages", "validate_remote_message", "format_remote_message",
    "new_request", "new_status", "new_event", "run_remote_protocol_self_tests",
]
