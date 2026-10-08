#!/usr/bin/env python3
"""Regression coverage for request-scoped protocol recovery freshness."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.web_runtime import WebLLMScraper  # noqa: E402
from agent_core.web_ui.base_adapter import BaseWebUIAdapter  # noqa: E402
from agent_core.web_ui.contracts import TurnRef  # noqa: E402


class FakeElement:
    def __init__(self, text: str, message_id: str = ""):
        self._text = text
        self._message_id = message_id

    def get_attribute(self, name: str):
        return self._message_id if name == "data-message-id" else ""

    def inner_text(self):
        return self._text


def source(scope: str, turn: str, fingerprint: str, response: str) -> dict:
    return {
        "scope_id": scope,
        "assistant_id_sha256": turn,
        "assistant_fingerprint": fingerprint,
        "response_sha256": response,
    }


def run() -> dict:
    rejected = source("scope-original", "turn-original", "fp-original", "sha-original")

    identical = WebLLMScraper._classify_protocol_recovery_readback(
        rejected,
        source("scope-repair", "turn-repair", "fp-repair", "sha-original"),
    )
    assert identical == {"fresh": False, "reason": "response_sha_unchanged"}

    same_scope = WebLLMScraper._classify_protocol_recovery_readback(
        rejected,
        source("scope-original", "turn-repair", "fp-repair", "sha-repair"),
    )
    assert same_scope == {"fresh": False, "reason": "request_scope_unchanged"}

    same_turn = WebLLMScraper._classify_protocol_recovery_readback(
        rejected,
        source("scope-repair", "turn-original", "fp-repair", "sha-repair"),
    )
    assert same_turn == {"fresh": False, "reason": "assistant_identity_unchanged"}

    same_fingerprint = WebLLMScraper._classify_protocol_recovery_readback(
        source("scope-original", "", "fp-original", "sha-original"),
        source("scope-repair", "", "fp-original", "sha-repair"),
    )
    assert same_fingerprint == {"fresh": False, "reason": "assistant_fingerprint_unchanged"}

    empty_sha = hashlib.sha256(b"").hexdigest()
    empty_fingerprint_with_changed_strong_evidence = (
        WebLLMScraper._classify_protocol_recovery_readback(
            source("scope-original", "turn-original", empty_sha, "sha-original"),
            source("scope-repair", "turn-repair", empty_sha, "sha-repair"),
        )
    )
    assert empty_fingerprint_with_changed_strong_evidence == {
        "fresh": True,
        "reason": "fresh_recovery_response",
    }
    empty_fingerprint_without_turn_ids = WebLLMScraper._classify_protocol_recovery_readback(
        source("scope-original", "", empty_sha, "sha-original"),
        source("scope-repair", "", empty_sha, "sha-repair"),
    )
    assert empty_fingerprint_without_turn_ids == {
        "fresh": True,
        "reason": "fresh_recovery_response",
    }

    adapter = BaseWebUIAdapter.__new__(BaseWebUIAdapter)
    element = FakeElement("new assistant response", "assistant-turn-2")
    turn = TurnRef(
        role="assistant",
        ordinal=2,
        structural_id="assistant-turn-2",
        content_digest="content-digest",
        raw_text="new assistant response",
        element=element,
    )
    assert adapter.element_fingerprint(turn) == adapter.element_fingerprint(element)
    assert adapter.element_fingerprint(turn) not in {"", empty_sha}
    assert adapter.element_fingerprint(FakeElement("")) == ""

    fresh = WebLLMScraper._classify_protocol_recovery_readback(
        rejected,
        source("scope-repair", "turn-repair", "fp-repair", "sha-repair"),
    )
    assert fresh == {"fresh": True, "reason": "fresh_recovery_response"}

    runtime_source = (ROOT / "agent_core" / "web_runtime.py").read_text(encoding="utf-8")
    assert "WEB_PROTOCOL_RECOVERY_STALE_READBACK" in runtime_source
    assert 'stage="protocol_recovery_stale_readback"' in runtime_source
    assert 'diagnostic["response_source"]' in runtime_source

    result = {
        "same_response_sha_rejected": True,
        "same_request_scope_rejected": True,
        "same_assistant_identity_rejected": True,
        "same_assistant_fingerprint_rejected": True,
        "empty_fingerprint_is_not_stale_evidence": True,
        "turn_ref_is_unwrapped_for_fingerprint": True,
        "fresh_recovery_response_accepted": True,
        "explicit_terminal_classification": True,
    }
    print("PROTOCOL_RECOVERY_FRESHNESS_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
