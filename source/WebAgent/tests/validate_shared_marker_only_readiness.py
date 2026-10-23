"""Provider-neutral marker-only bootstrap readiness regression tests.

Run from repo root:
  python source/WebAgent/tests/validate_shared_marker_only_readiness.py

No network or browser access is needed: send_prompt is the same shared
transport boundary used by all Web UI providers, including Other URL.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.session_protocol import SessionProtocol
from WebAgent.session import ensure_webagent_session


PROVIDERS = {
    "chatgpt": "https://chatgpt.com/c/marker-only-test",
    "gemini": "https://gemini.google.com/app/marker-only-test",
    "claude": "https://claude.ai/chat/marker-only-test",
    "m365": "https://m365.cloud.microsoft/chat/conversation/marker-only-test",
    "other_url": "https://custom.example.invalid/app/marker-only-test",
}
MARKER = "[AGENT_PROTOCOL_READY]"


def identity_reply(prompt: str, marker: str) -> str:
    identity = json.loads(prompt.splitlines()[1])
    return f"[{marker}]\n{json.dumps(identity, separators=(',', ':'))}\n[/{marker}]"


def check_parser() -> None:
    proto = SessionProtocol("web_agent_direct", 9, "test contract")
    session_id = "SES-MARKER-ONLY"
    assert not proto.parse_protocol_ready(MARKER, session_id=session_id)
    assert proto.parse_protocol_ready(MARKER, session_id=session_id, allow_marker_only=True)
    assert proto.parse_protocol_ready("  \n" + MARKER + "\n ", session_id=session_id, allow_marker_only=True)

    for text in (
        "AGENT_PROTOCOL_READY",  # missing brackets
        "prefix " + MARKER,
        MARKER + " suffix",
        MARKER + "\n{}",
        MARKER + "\n[/AGENT_PROTOCOL_READY]",
        "```text\n" + MARKER + "\n```",
        "[agent_protocol_ready]",
        "[AGENT_SESSION_READY]",
    ):
        assert not proto.parse_protocol_ready(text, session_id=session_id, allow_marker_only=True), text

    full = identity_reply(proto.bootstrap_prompt(session_id=session_id), "AGENT_PROTOCOL_READY")
    assert proto.parse_protocol_ready(full, session_id=session_id)
    assert proto.parse_protocol_ready(full, session_id=session_id, allow_marker_only=True)
    bad_identity = json.loads(proto.bootstrap_prompt(session_id=session_id).splitlines()[1])
    bad_identity["protocol_hash"] = "WRONG"
    malformed = "[AGENT_PROTOCOL_READY]\n" + json.dumps(bad_identity) + "\n[/AGENT_PROTOCOL_READY]"
    assert not proto.parse_protocol_ready(malformed, session_id=session_id, allow_marker_only=True)

    attach = proto.session_attach_prompt(session_id=session_id, handshake_id="HND-REAL")
    session_reply = identity_reply(attach, "AGENT_SESSION_READY")
    assert proto.parse_session_ready(session_reply, session_id=session_id, handshake_id="HND-REAL")
    assert not proto.parse_session_ready(session_reply, session_id=session_id, handshake_id="HND-OTHER")
    assert not proto.parse_session_ready("[AGENT_SESSION_READY]",
                                         session_id=session_id, handshake_id="HND-REAL")


def check_all_providers() -> None:
    for provider, url in PROVIDERS.items():
        with tempfile.TemporaryDirectory(prefix="smartagent-short-ready-") as directory:
            root = Path(directory)
            calls: list[str] = []

            def send(prompt: str, *, stage: str, **_kwargs) -> str:
                calls.append(stage)
                if stage == "協議初始化":
                    assert prompt.startswith("[AGENT_PROTOCOL_BOOTSTRAP]")
                    return "\n" + MARKER + "\n"
                assert stage == "協議附著確認"
                assert prompt.startswith("[AGENT_SESSION_ATTACH]")
                return identity_reply(prompt, "AGENT_SESSION_READY")

            options = dict(workspace=str(root), gpt_url=url,
                           state_dir=root / "state", send_prompt=send)
            first = ensure_webagent_session(**options)
            assert first["mode"] == "BOOTSTRAP", (provider, first)
            assert first["state"]["armed"] is True
            assert first["state"]["readiness_mode"] == "MARKER_ONLY"
            second = ensure_webagent_session(**options)
            assert second["mode"] == "SESSION_ATTACH", (provider, second)
            assert calls == ["協議初始化", "協議附著確認"], (provider, calls)

            # A short session-attach marker must NOT satisfy its nonce.
            # The Runtime instead attempts a normal fresh bootstrap.
            def short_attach(prompt: str, *, stage: str, **_kwargs) -> str:
                if stage == "協議附著確認":
                    return "[AGENT_SESSION_READY]"
                if stage == "協議初始化":
                    return MARKER
                raise AssertionError(stage)

            third = ensure_webagent_session(**{**options, "send_prompt": short_attach})
            assert third["mode"] == "BOOTSTRAP"
            assert third["reason"] == "session_attach_failed"
            assert third["state"]["readiness_mode"] == "MARKER_ONLY"

            # A structured FULL_IDENTITY response is still accepted.
            def full_bootstrap(prompt: str, *, stage: str, **_kwargs) -> str:
                if stage == "協議初始化":
                    return identity_reply(prompt, "AGENT_PROTOCOL_READY")
                return "[AGENT_SESSION_READY]"

            fourth = ensure_webagent_session(**{**options, "send_prompt": full_bootstrap})
            assert fourth["mode"] == "BOOTSTRAP"
            assert fourth["state"]["readiness_mode"] == "FULL_IDENTITY"


def check_rejections() -> None:
    for label, reply in {
        "embedded_in_prose": "The protocol is ready: " + MARKER,
        "trailing_text": MARKER + " extra",
        "in_code_fence": "```\n" + MARKER + "\n```",
        "unbracketed": "AGENT_PROTOCOL_READY",
        "wrong_marker": "[AGENT_SESSION_READY]",
    }.items():
        with tempfile.TemporaryDirectory(prefix="smartagent-short-reject-") as directory:
            root = Path(directory)

            def send(_prompt: str, *, stage: str, **_kwargs) -> str:
                assert stage == "協議初始化"
                return reply

            try:
                ensure_webagent_session(
                    workspace=str(root), gpt_url=PROVIDERS["other_url"],
                    state_dir=root / "state", send_prompt=send,
                )
            except RuntimeError as exc:
                assert "matching WEBAGENT protocol readiness" in str(exc), (label, exc)
            else:
                raise AssertionError(f"Rejected bootstrap unexpectedly armed: {label}")


def run() -> dict:
    check_parser()
    check_all_providers()
    check_rejections()
    return {
        "exact_marker_only_bootstrap": "PASS",
        "all_web_ui_provider_urls_including_other": list(PROVIDERS),
        "full_identity_compatibility": "PASS",
        "strict_session_attach_nonce": "PASS",
        "embedded_marker_and_malformed_rejection": "PASS",
    }


if __name__ == "__main__":
    print("SHARED_MARKER_ONLY_READINESS_PASS")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
