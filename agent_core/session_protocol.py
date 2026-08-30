#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared protocol bootstrap/session-attach lifecycle.

The lifecycle is protocol-agnostic.  SmartAgent and RemoteAgent provide their
own protocol_name/version/body while reusing the same negotiation format.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

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


def protocol_hash(protocol_body: str) -> str:
    normalized = str(protocol_body or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _marker_block(marker: str, payload: dict) -> str:
    return f"[{marker}]\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n[/{marker}]"


def _extract_marker_payload(text: str, marker: str) -> dict | None:
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
        return None
    return payload if isinstance(payload, dict) else None


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

    def __init__(self, protocol_name: str, protocol_version: int, protocol_body: str):
        name = str(protocol_name or "").strip()
        if not name:
            raise ValueError("protocol_name 不可為空")
        version = int(protocol_version)
        if version <= 0:
            raise ValueError("protocol_version 必須 > 0")
        self.protocol_body = str(protocol_body or "").strip()
        self.identity = ProtocolIdentity(name, version, protocol_hash(self.protocol_body))

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
            "Do not execute a tool and do not answer the end user in this handshake.\n"
            "Reply with exactly this readiness block and nothing else:\n"
            f"{ready}\n"
            f"[/{PROTOCOL_BOOTSTRAP_MARKER}]"
        )

    def session_attach_prompt(self, *, session_id: str) -> str:
        payload = {
            "protocol_name": self.identity.protocol_name,
            "protocol_version": self.identity.protocol_version,
            "protocol_hash": self.identity.protocol_hash,
            "session_id": session_id,
        }
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

    def parse_protocol_ready(self, text: str, *, session_id: str) -> bool:
        payload = _extract_marker_payload(text, PROTOCOL_READY_MARKER)
        return self._matches(payload, session_id=session_id)

    def parse_session_ready(self, text: str, *, session_id: str) -> bool:
        payload = _extract_marker_payload(text, SESSION_READY_MARKER)
        return self._matches(payload, session_id=session_id)

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
