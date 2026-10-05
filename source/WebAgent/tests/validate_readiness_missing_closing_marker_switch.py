#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline checks for the dangerous readiness missing-close debug switch."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.session_protocol import (  # noqa: E402
    PROTOCOL_READY_MARKER,
    SESSION_READY_MARKER,
    SessionProtocol,
)


def _write_switch(path: Path, enabled: bool) -> None:
    path.write_text(
        json.dumps(
            {"protocol_readiness": {"allow_missing_closing_marker": enabled}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _unclosed(marker: str, payload: dict, suffix: str = "") -> str:
    return f"[{marker}]\n{json.dumps(payload, separators=(',', ':'))}{suffix}"


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="readiness-debug-switch-") as temp:
        config_path = Path(temp) / "debug_config.json"
        proto = SessionProtocol("web_agent_direct", 8, "body", debug_config_path=config_path)
        payload = {
            "protocol_name": proto.identity.protocol_name,
            "protocol_version": proto.identity.protocol_version,
            "protocol_hash": proto.identity.protocol_hash,
            "session_id": "SES-TEST",
        }
        protocol_reply = _unclosed(PROTOCOL_READY_MARKER, payload)
        session_reply = _unclosed(SESSION_READY_MARKER, payload)

        _write_switch(config_path, False)
        assert not proto.parse_protocol_ready(protocol_reply, session_id="SES-TEST")
        assert not proto.parse_session_ready(session_reply, session_id="SES-TEST")

        _write_switch(config_path, True)
        assert proto.parse_protocol_ready(protocol_reply, session_id="SES-TEST")
        assert proto.parse_session_ready(session_reply, session_id="SES-TEST")

        assert not proto.parse_protocol_ready(
            _unclosed(PROTOCOL_READY_MARKER, payload, " trailing prose"),
            session_id="SES-TEST",
        )
        assert not proto.parse_protocol_ready(
            _unclosed(PROTOCOL_READY_MARKER, payload) + "\n{}",
            session_id="SES-TEST",
        )
        assert not proto.parse_protocol_ready(
            f"[{PROTOCOL_READY_MARKER}]\n{{\"protocol_name\":",
            session_id="SES-TEST",
        )
        assert not proto.parse_protocol_ready(
            _unclosed(PROTOCOL_READY_MARKER, {**payload, "protocol_hash": "wrong"}),
            session_id="SES-TEST",
        )
        assert not proto.parse_protocol_ready(
            _unclosed(PROTOCOL_READY_MARKER, {**payload, "session_id": "SES-OTHER"}),
            session_id="SES-TEST",
        )
        assert not proto.parse_protocol_ready(
            json.dumps(payload, separators=(",", ":")),
            session_id="SES-TEST",
        )

    return {
        "switch_off_rejects_missing_close": True,
        "switch_on_accepts_exact_json_only_readiness": True,
        "protocol_and_session_readiness_covered": True,
        "truncated_or_extra_content_rejected": True,
        "identity_mismatch_rejected": True,
    }


if __name__ == "__main__":
    print("READINESS_MISSING_CLOSING_MARKER_SWITCH_OK")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
