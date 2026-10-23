#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared protocol bootstrap/session-attach lifecycle.

The lifecycle is protocol-agnostic.  SmartAgent and RemoteAgent provide their
own protocol_name/version/body while reusing the same negotiation format.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PROTOCOL_BOOTSTRAP_MARKER = "AGENT_PROTOCOL_BOOTSTRAP"
PROTOCOL_READY_MARKER = "AGENT_PROTOCOL_READY"
SESSION_ATTACH_MARKER = "AGENT_SESSION_ATTACH"
SESSION_READY_MARKER = "AGENT_SESSION_READY"

UNINITIALIZED = "UNINITIALIZED"
BOOTSTRAP = "BOOTSTRAP"
PROTOCOL_READY = "PROTOCOL_READY"
ARMED = "ARMED"
SESSION_ATTACH = "SESSION_ATTACH"
SESSION_READY = "SESSION_READY"
ACTIVE = "ACTIVE"
STALE = "STALE"

DEFAULT_DEBUG_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "debug_config.json"


def protocol_hash(protocol_body: str) -> str:
    normalized = str(protocol_body or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _marker_block(marker: str, payload: dict) -> str:
    return f"[{marker}]\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n[/{marker}]"


def _extract_marker_payload(text: str, marker: str, *, allow_smart_quotes: bool = False) -> dict | None:
    source = str(text or "")
    start_token = f"[{marker}]"
    end_token = f"[/{marker}]"
    start = source.find(start_token)
    if start < 0:
        return None
    start += len(start_token)
    end = source.find(end_token, start)
    if end < 0:
        return None
    body = source[start:end].strip()
    try:
        payload = json.loads(body)
    except Exception:
        if not allow_smart_quotes or not any(c in body for c in '“”'):
            return None
        try:
            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result: raise ValueError('duplicate_readiness_key')
                    result[key] = value
                return result
            payload = json.loads(body.translate(str.maketrans({'“':'"', '”':'"'})), object_pairs_hook=unique_keys)
        except (ValueError, TypeError):
            return None
        # Compatibility is limited to the ASCII identity envelope. Never repair
        # arbitrary prose, nested values or quoted content from normal replies.
        allowed = {'protocol_name','protocol_version','protocol_hash','session_id','handshake_id'}
        if not isinstance(payload, dict) or not set(payload).issubset(allowed): return None
        if type(payload.get('protocol_version')) is not int: return None
        if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', value)
               for key, value in payload.items() if key != 'protocol_version'): return None
    return payload if isinstance(payload, dict) else None


def _allow_missing_readiness_closing_marker(config_path: Path) -> bool:
    """Read the dangerous debug switch at decision time and fail closed."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    protocol = payload.get("protocol_readiness", {}) if isinstance(payload, dict) else {}
    return bool(
        isinstance(protocol, dict)
        and protocol.get("allow_missing_closing_marker") is True
    )


def _extract_unclosed_marker_payload(text: str, marker: str) -> dict | None:
    """Recover only an otherwise exact, JSON-only block whose close marker is absent."""
    source = str(text or "")
    start_token = f"[{marker}]"
    end_token = f"[/{marker}]"
    start = source.find(start_token)
    if start < 0 or end_token in source:
        return None
    if source[:start].strip():
        return None
    body = source[start + len(start_token):].lstrip()
    try:
        payload, end = json.JSONDecoder().raw_decode(body)
    except (ValueError, TypeError):
        return None
    if body[end:].strip() or not isinstance(payload, dict):
        return None
    return payload


@dataclass(frozen=True)
class ProtocolIdentity:
    protocol_name: str
    protocol_version: int
    protocol_hash: str


@dataclass
class SessionDecision:
    action: str
    session_id: str
    reason: str


class SessionProtocol:
    """Protocol-independent handshake builder/parser/state transition helper."""

    def __init__(
        self,
        protocol_name: str,
        protocol_version: int,
        protocol_body: str,
        *,
        debug_config_path: str | Path | None = None,
    ):
        name = str(protocol_name or "").strip()
        if not name:
            raise ValueError("protocol_name 不可為空")
        version = int(protocol_version)
        if version <= 0:
            raise ValueError("protocol_version 必須 > 0")
        self.protocol_body = str(protocol_body or "").strip()
        self.identity = ProtocolIdentity(name, version, protocol_hash(self.protocol_body))
        self.debug_config_path = Path(debug_config_path) if debug_config_path else DEFAULT_DEBUG_CONFIG_PATH

    def new_session_id(self) -> str:
        return "SES-" + uuid.uuid4().hex.upper()

    def decide(self, stored_state: dict | None, *, session_id: str | None = None) -> SessionDecision:
        state = stored_state or {}
        session_id = session_id or self.new_session_id()
        matches = bool(
            state.get("armed") is True
            and str(state.get("protocol_name", "")) == self.identity.protocol_name
            and int(state.get("protocol_version", 0) or 0) == self.identity.protocol_version
            and str(state.get("protocol_hash", "")) == self.identity.protocol_hash
        )
        if matches:
            return SessionDecision(SESSION_ATTACH, session_id, "armed_protocol_match")
        return SessionDecision(BOOTSTRAP, session_id, "unarmed_or_protocol_mismatch")

    def bootstrap_prompt(self, *, session_id: str) -> str:
        identity = {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "session_id": session_id,
        }
        ready = _marker_block(PROTOCOL_READY_MARKER, identity)
        return (
            f"[{PROTOCOL_BOOTSTRAP_MARKER}]\n"
            f"{json.dumps(identity, ensure_ascii=False, separators=(',', ':'))}\n"
            "The following protocol is the canonical conversation-level contract for this agent.\n"
            "Store and follow it for subsequent turns in this conversation.\n"
            "For THIS bootstrap handshake only, the handshake response rule below overrides any normal\n"
            "business-protocol response-format rule contained inside the protocol body.\n\n"
            "----- BEGIN CANONICAL PROTOCOL -----\n"
            f"{self.protocol_body}\n"
            "----- END CANONICAL PROTOCOL -----\n\n"
            "INITIALIZATION HANDSHAKE ONLY: normal smartagent_tool, report_progress,\n"
            "and turn_commit output rules do not apply to this one reply.\n"
            "Do not execute a tool and do not answer the end user in this handshake.\n"
            "Copy the entire readiness block, including the JSON identity and closing\n"
            "marker. Never stop after the opening marker.\n"
            "Reply with exactly this readiness block and nothing else:\n"
            f"{ready}\n"
            f"[/{PROTOCOL_BOOTSTRAP_MARKER}]"
        )

    def protocol_ready_repair_prompt(self, *, session_id: str) -> str:
        """Bounded format-only correction; cannot grant protocol readiness."""
        identity = {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "session_id": session_id,
        }
        return (
            "[AGENT_PROTOCOL_READINESS_REPAIR]\n"
            f"{json.dumps(identity, ensure_ascii=False, separators=(',', ':'))}\n"
            "The canonical protocol was already sent in the preceding bootstrap.\n"
            "This is one format-only correction, not a new task or a new session.\n"
            "Do not execute a tool and do not answer the end user in this handshake.\n"
            "Do not use smartagent_tool, report_progress, or turn_commit.\n"
            "Reply with exactly this readiness block and nothing else:\n"
            f"{_marker_block(PROTOCOL_READY_MARKER, identity)}\n"
            "[/AGENT_PROTOCOL_READINESS_REPAIR]"
        )

    def protocol_ready_compact_identity_line(self, *, session_id: str) -> str:
        """Exact, session-bound identity echo; marker-only can never pass."""
        return (
            f"READY_IDENTITY_V1|{self.identity.protocol_name}|"
            f"{self.identity.protocol_version}|{self.identity.protocol_hash}|{session_id}"
        )

    def protocol_ready_compact_challenge_prompt(self, *, session_id: str) -> str:
        """Last bounded fallback after TWO incomplete marker-only responses."""
        line = self.protocol_ready_compact_identity_line(session_id=session_id)
        return (
            "[AGENT_PROTOCOL_IDENTITY_CHALLENGE]\n"
            "The preceding bootstrap and its format repair returned only an opening marker.\n"
            "This is NOT a task; no action, smartagent_tool, JSON or markdown is allowed.\n"
            "Do not execute a tool and do not answer the end user in this handshake.\n"
            "Reply with exactly this one line and nothing else:\n"
            f"{line}"
        )

    def parse_protocol_ready_compact(self, response: str, *, session_id: str) -> bool:
        """A complete, exact identity is required even in compact form."""
        return str(response or "").strip() == self.protocol_ready_compact_identity_line(
            session_id=session_id
        )

    def protocol_ready_failure_reason(
        self, text: str, *, session_id: str, allow_smart_quotes: bool = False,
    ) -> str:
        """Diagnostic only; parse_protocol_ready remains the sole acceptance gate."""
        source = str(text or "")
        if f"[{PROTOCOL_READY_MARKER}]" not in source:
            return "READY_MARKER_MISSING"
        if (f"[/{PROTOCOL_READY_MARKER}]" not in source
                and not _allow_missing_readiness_closing_marker(self.debug_config_path)):
            return "READY_CLOSING_MARKER_MISSING"
        payload = _extract_marker_payload(
            source, PROTOCOL_READY_MARKER, allow_smart_quotes=allow_smart_quotes,
        )
        if payload is None and _allow_missing_readiness_closing_marker(self.debug_config_path):
            payload = _extract_unclosed_marker_payload(source, PROTOCOL_READY_MARKER)
        if payload is None:
            return "READY_PAYLOAD_INVALID"
        if not self._matches(payload, session_id=session_id):
            return "READY_IDENTITY_MISMATCH"
        return "READY_VALID"

    def session_attach_prompt(self, *, session_id: str, handshake_id: str = '') -> str:
        payload = {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "session_id": session_id,
        }
        if handshake_id:
            payload['handshake_id'] = handshake_id
        ready = _marker_block(SESSION_READY_MARKER, payload)
        return (
            f"[{SESSION_ATTACH_MARKER}]\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
            "This is a lightweight reconnect to the already-armed protocol in this conversation.\n"
            "Do not execute a tool and do not answer the end user in this handshake.\n"
            "If the exact protocol_name/version/hash is still available and understood, reply exactly:\n"
            f"{ready}\n"
            "If it is not available, do not fabricate readiness; reply with AGENT_SESSION_STALE.\n"
            f"[/{SESSION_ATTACH_MARKER}]"
        )

    def parse_protocol_ready(self, text: str, *, session_id: str, allow_smart_quotes: bool = False) -> bool:
        payload = _extract_marker_payload(text, PROTOCOL_READY_MARKER, allow_smart_quotes=allow_smart_quotes)
        if payload is None and _allow_missing_readiness_closing_marker(self.debug_config_path):
            payload = _extract_unclosed_marker_payload(text, PROTOCOL_READY_MARKER)
        return self._matches(payload, session_id=session_id)

    def parse_session_ready(self, text: str, *, session_id: str, handshake_id: str = '', allow_smart_quotes: bool = False) -> bool:
        payload = _extract_marker_payload(text, SESSION_READY_MARKER, allow_smart_quotes=allow_smart_quotes)
        if payload is None and _allow_missing_readiness_closing_marker(self.debug_config_path):
            payload = _extract_unclosed_marker_payload(text, SESSION_READY_MARKER)
        return self._matches(payload, session_id=session_id) and (not handshake_id or payload.get('handshake_id') == handshake_id)

    def _matches(self, payload: dict | None, *, session_id: str) -> bool:
        if not payload:
            return False
        return bool(
            str(payload.get("protocol_name", "")) == self.identity.protocol_name
            and int(payload.get("protocol_version", 0) or 0) == self.identity.protocol_version
            and str(payload.get("protocol_hash", "")) == self.identity.protocol_hash
            and str(payload.get("session_id", "")) == str(session_id)
        )

    def armed_state(self, *, session_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        return {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "armed": True,
            "session_id": session_id,
            "session_state": ACTIVE,
            "last_protocol_check": now,
            "last_session_attach": now,
        }

    def failed_state(self, *, session_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        return {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "armed": False,
            "session_id": session_id,
            "session_state": STALE,
            "last_protocol_check": now,
            "last_session_attach": 0.0,
        }


def run_session_protocol_self_tests() -> dict:
    proto = SessionProtocol("smart_agent", 3, "canonical protocol body")
    results = {}

    first = proto.decide({})
    results["new_conversation_bootstrap"] = first.action == BOOTSTRAP

    state = proto.armed_state(session_id="SES-OLD", now=10.0)
    reconnect = proto.decide(state, session_id="SES-NEW")
    results["armed_reconnect_attach"] = reconnect.action == SESSION_ATTACH

    wrong_version = dict(state, protocol_version=2)
    results["version_mismatch_rebootstrap"] = proto.decide(wrong_version).action == BOOTSTRAP

    good_ready_payload = {
        "protocol_name": proto.identity.protocol_name,
        "protocol_version": proto.identity.protocol_version,
        "protocol_hash": proto.identity.protocol_hash,
        "session_id": "SES-1",
    }
    good_ready = _marker_block(PROTOCOL_READY_MARKER, good_ready_payload)
    results["valid_protocol_ready"] = proto.parse_protocol_ready(good_ready, session_id="SES-1")

    bad_ready = _marker_block(PROTOCOL_READY_MARKER, {**good_ready_payload, "protocol_hash": "wrong"})
    results["bad_ready_not_armed"] = not proto.parse_protocol_ready(bad_ready, session_id="SES-1")

    good_session = _marker_block(SESSION_READY_MARKER, good_ready_payload)
    results["valid_session_ready"] = proto.parse_session_ready(good_session, session_id="SES-1")

    malformed = f"[{PROTOCOL_READY_MARKER}]\nnot-json\n[/{PROTOCOL_READY_MARKER}]"
    results["malformed_ready_rejected"] = not proto.parse_protocol_ready(malformed, session_id="SES-1")

    results["all_passed"] = all(results.values())
    return results
