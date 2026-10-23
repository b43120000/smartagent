#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebAgent-owned protocol bootstrap and session state."""
from __future__ import annotations

from pathlib import Path
import uuid
from typing import Callable
from urllib.parse import urlsplit

from agent_core.conversation_registry import ConversationRegistry
from agent_core.session_protocol import (
    BOOTSTRAP,
    PROTOCOL_READY_MARKER,
    SessionProtocol,
)

from .protocol import WEBAGENT_PROTOCOL_BODY, WEBAGENT_PROTOCOL_NAME, WEBAGENT_PROTOCOL_VERSION


def ensure_webagent_session(
    *,
    workspace: str,
    gpt_url: str,
    state_dir: str | Path,
    send_prompt: Callable[..., str],
    display_name: str = "WebAgent",
    allow_scoped_marker_only_bootstrap: bool = False,
) -> dict:
    state_root = Path(state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    registry = ConversationRegistry(state_root / "conversations.json")
    registry.load()
    registry.upsert_binding(workspace, gpt_url, purpose="webagent_direct")
    session = SessionProtocol(
        WEBAGENT_PROTOCOL_NAME,
        WEBAGENT_PROTOCOL_VERSION,
        WEBAGENT_PROTOCOL_BODY,
    )
    stored = registry.get_protocol_state(workspace, gpt_url, WEBAGENT_PROTOCOL_NAME)
    decision = session.decide(stored)
    claude_quotes = urlsplit(gpt_url).hostname == 'claude.ai'
    def bootstrap(reason: str) -> dict:
        print(
            f"[{display_name}] Session mode: BOOTSTRAP ({reason})；即將注入 WebAgent/smartagent_tool 協議。",
            flush=True,
        )
        response = send_prompt(
            session.bootstrap_prompt(session_id=decision.session_id),
            stage="協議初始化",
        )
        full_identity_ready = session.parse_protocol_ready(
            response, session_id=decision.session_id, allow_smart_quotes=claude_quotes
        )
        # The browser must finish its UI/response stability gate before this
        # parser runs. Correct an incomplete FORMAT at most once, without
        # replaying the full bootstrap or executing any task action.
        # An otherwise complete identity mismatch is never retried.
        if not full_identity_ready:
            reason_code = session.protocol_ready_failure_reason(
                response, session_id=decision.session_id,
                allow_smart_quotes=claude_quotes,
            )
            print(
                f"[{display_name}] Bootstrap readiness 未通過；"
                f"reason={reason_code} response_chars={len(str(response or ''))}.",
                flush=True,
            )
            if reason_code in {
                "READY_MARKER_MISSING", "READY_CLOSING_MARKER_MISSING",
                "READY_PAYLOAD_INVALID",
            }:
                repaired = send_prompt(
                    session.protocol_ready_repair_prompt(session_id=decision.session_id),
                    stage="協議初始化格式修復",
                )
                full_identity_ready = session.parse_protocol_ready(
                    repaired, session_id=decision.session_id,
                    allow_smart_quotes=claude_quotes,
                )
                if not full_identity_ready:
                    reason_code = session.protocol_ready_failure_reason(
                        repaired, session_id=decision.session_id,
                        allow_smart_quotes=claude_quotes,
                    )
                    print(
                        f"[{display_name}] Bootstrap readiness 格式修復仍未通過；"
                        f"reason={reason_code} response_chars={len(str(repaired or ''))}.",
                        flush=True,
                    )
        # The legacy marker-only flag is retained for call compatibility only.
        # A complete matching identity is mandatory, including for Claude.
        if not full_identity_ready:
            registry.set_protocol_state(
                workspace,
                gpt_url,
                WEBAGENT_PROTOCOL_NAME,
                session.failed_state(session_id=decision.session_id),
            )
            raise RuntimeError(
                "WebGPT 未回傳 matching WEBAGENT protocol readiness; reason="
                + reason_code
            )
        state = session.armed_state(session_id=decision.session_id)
        state["readiness_mode"] = "FULL_IDENTITY"
        registry.set_protocol_state(workspace, gpt_url, WEBAGENT_PROTOCOL_NAME, state)
        print(f"[{display_name}] 協議初始化成功，ChatGPT 已回傳 matching readiness。", flush=True)
        return {"mode": BOOTSTRAP, "reason": reason, "state": state}

    if decision.action == BOOTSTRAP:
        return bootstrap(decision.reason)

    print(
        f"[{display_name}] Session mode: SESSION_ATTACH；正在確認此對話保留既有 WebAgent 協議。",
        flush=True,
    )
    handshake_id = 'HND-' + uuid.uuid4().hex.upper()
    response = send_prompt(
        session.session_attach_prompt(session_id=decision.session_id, handshake_id=handshake_id),
        stage="協議附著確認",
    )
    if session.parse_session_ready(response, session_id=decision.session_id, handshake_id=handshake_id, allow_smart_quotes=claude_quotes):
        state = session.armed_state(session_id=decision.session_id)
        registry.set_protocol_state(workspace, gpt_url, WEBAGENT_PROTOCOL_NAME, state)
        print(f"[{display_name}] 協議附著成功。", flush=True)
        return {"mode": decision.action, "reason": decision.reason, "state": state}
    decision.action = BOOTSTRAP
    return bootstrap("session_attach_failed")
