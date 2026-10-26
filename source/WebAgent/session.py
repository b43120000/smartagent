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
        first_marker_only = str(response or "").strip() == f"[{PROTOCOL_READY_MARKER}]"
        compact_identity_ready = False
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
                    # Two distinct, completed exact marker-only responses
                    # trigger ONE short identity challenge. Marker-only itself
                    # never authorizes ARMED or bypasses any identity field.
                    if (first_marker_only
                            and str(repaired or "").strip() == f"[{PROTOCOL_READY_MARKER}]"):
                        challenge_reply = send_prompt(
                            session.protocol_ready_compact_challenge_prompt(
                                session_id=decision.session_id
                            ),
                            stage="協議初始化精簡身分確認",
                        )
                        compact_identity_ready = session.parse_protocol_ready_compact(
                            challenge_reply, session_id=decision.session_id,
                        )
                        print(
                            f"[{display_name}] Bootstrap compact identity "
                            f"{'MATCHED' if compact_identity_ready else 'REJECTED'}; "
                            f"response_chars={len(str(challenge_reply or ''))}.",
                            flush=True,
                        )
                        if not compact_identity_ready:
                            reason_code = "READY_COMPACT_IDENTITY_MISMATCH"
        # The legacy marker-only flag is retained for call compatibility only.
        # A complete matching identity is mandatory, including for Claude.
        if not (full_identity_ready or compact_identity_ready):
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
        state["readiness_mode"] = (
            "COMPACT_FULL_IDENTITY" if compact_identity_ready else "FULL_IDENTITY"
        )
        state["readiness_identity_verified"] = True
        registry.set_protocol_state(workspace, gpt_url, WEBAGENT_PROTOCOL_NAME, state)
        print(
            f"[{display_name}] 協議初始化成功，mode={state['readiness_mode']} "
            "matching protocol and session identity.",
            flush=True,
        )
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
