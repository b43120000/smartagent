#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline verification for WebAgent-owned browser send and session bootstrap."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.session_protocol import (
    PROTOCOL_BOOTSTRAP_MARKER,
    PROTOCOL_READY_MARKER,
    SESSION_ATTACH_MARKER,
    SESSION_READY_MARKER,
)
from agent_core.webgpt_rate_governor import WebGPTRateLimited
from WebAgent.browser_client import WebAgentBrowserClient
from WebAgent.session import ensure_webagent_session


class FakeLease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class FakeGovernor:
    def __init__(self):
        self.attempts = 0
        self.successes = 0
        self.lease = FakeLease()

    def acquire(self, *, wait: bool, conversation_key: str = ""):
        assert wait is False
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("busy")
        if self.attempts == 2:
            raise WebGPTRateLimited(0.1)
        return self.lease

    def record_success(self):
        self.successes += 1


class FakeScraper:
    def __init__(self):
        self._rate_governor = None
        self._rate_submit_lease = None
        self.calls = []

    def ask(self, prompt, **kwargs):
        assert self._rate_submit_lease is not None
        self.calls.append((prompt, kwargs))
        return "CLIENT_OK"


def marker_reply(prompt: str) -> str:
    lines = prompt.splitlines()
    payload = json.loads(lines[1])
    if lines[0] == f"[{PROTOCOL_BOOTSTRAP_MARKER}]":
        marker = PROTOCOL_READY_MARKER
    elif lines[0] == f"[{SESSION_ATTACH_MARKER}]":
        marker = SESSION_READY_MARKER
    else:
        raise AssertionError(lines[0])
    return f"[{marker}]\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n[/{marker}]"


def run() -> dict:
    fake_time = [0.0]

    def sleep(seconds: float):
        fake_time[0] += float(seconds)

    governor = FakeGovernor()
    scraper = FakeScraper()
    client = WebAgentBrowserClient(
        scraper,
        governor=governor,
        sleep=sleep,
        clock=lambda: fake_time[0],
    )
    assert client.ask("probe", stage="測試協議") == "CLIENT_OK"
    assert governor.attempts == 3
    assert governor.successes == 1
    assert governor.lease.released
    assert scraper._rate_submit_lease is None

    stages = []
    with tempfile.TemporaryDirectory(prefix="webagent-session-") as temp:
        root = Path(temp)

        def send_prompt(prompt: str, *, stage: str, **_kwargs) -> str:
            stages.append(stage)
            return marker_reply(prompt)

        first = ensure_webagent_session(
            workspace=str(root),
            gpt_url="https://chatgpt.com/c/webagent-startup-test",
            state_dir=root / "state",
            send_prompt=send_prompt,
        )
        second = ensure_webagent_session(
            workspace=str(root),
            gpt_url="https://chatgpt.com/c/webagent-startup-test",
            state_dir=root / "state",
            send_prompt=send_prompt,
        )
        assert first["mode"] == "BOOTSTRAP"
        assert second["mode"] == "SESSION_ATTACH"
        assert stages == ["協議初始化", "協議附著確認"]

    marker_only_results = {}
    for short_reply, accepted in (
        (PROTOCOL_READY_MARKER, False),
        (f"[{PROTOCOL_READY_MARKER}]", True),
        (f"\n  [{PROTOCOL_READY_MARKER}]\n", True),
    ):
        with tempfile.TemporaryDirectory(prefix="webagent-short-ready-") as temp:
            root = Path(temp)

            def short_send_prompt(prompt: str, *, stage: str, **_kwargs) -> str:
                assert stage == "協議初始化"
                assert prompt.startswith(f"[{PROTOCOL_BOOTSTRAP_MARKER}]")
                return short_reply

            if accepted:
                result = ensure_webagent_session(
                    workspace=str(root),
                    gpt_url="https://chatgpt.com/c/webagent-short-ready-test",
                    state_dir=root / "state",
                    send_prompt=short_send_prompt,
                    allow_scoped_marker_only_bootstrap=True,
                )
                assert result["state"]["armed"] is True
                assert result["state"]["readiness_mode"] == "MARKER_ONLY"
            else:
                try:
                    ensure_webagent_session(
                        workspace=str(root),
                        gpt_url="https://chatgpt.com/c/webagent-short-ready-test",
                        state_dir=root / "state",
                        send_prompt=short_send_prompt,
                        allow_scoped_marker_only_bootstrap=True,
                    )
                except RuntimeError as exc:
                    assert "matching WEBAGENT protocol readiness" in str(exc)
                else:
                    raise AssertionError("Bare unbracketed marker must not count")
            marker_only_results[short_reply] = accepted

    with tempfile.TemporaryDirectory(prefix="webagent-short-ready-negative-") as temp:
        root = Path(temp)

        def unsafe_short_send_prompt(prompt: str, *, stage: str, **_kwargs) -> str:
            return f"[{PROTOCOL_READY_MARKER}] extra"

        try:
            ensure_webagent_session(
                workspace=str(root),
                gpt_url="https://chatgpt.com/c/webagent-short-ready-negative",
                state_dir=root / "state",
                send_prompt=unsafe_short_send_prompt,
                allow_scoped_marker_only_bootstrap=True,
            )
        except RuntimeError as exc:
            assert "matching WEBAGENT protocol readiness" in str(exc)
        else:
            raise AssertionError("marker-only compatibility accepted trailing text")

    # All Web UI providers now share the default exact-marker bootstrap
    # compatibility, independently of the legacy flag.
    with tempfile.TemporaryDirectory(prefix="webagent-short-ready-default-") as temp:
        root = Path(temp)

        def default_send_prompt(prompt: str, *, stage: str, **_kwargs) -> str:
            return f"[{PROTOCOL_READY_MARKER}]"

        accepted = ensure_webagent_session(
            workspace=str(root),
            gpt_url="https://chatgpt.com/c/webagent-short-ready-default",
            state_dir=root / "state",
            send_prompt=default_send_prompt,
        )
        assert accepted["state"]["readiness_mode"] == "MARKER_ONLY"

    return {
        "webagent_owned_sender": True,
        "busy_lock_is_observable": True,
        "rate_delay_is_observable": True,
        "bootstrap_sent": True,
        "session_attach_sent": True,
        "marker_only_exact_bracketed_accepted": marker_only_results,
        "marker_only_with_extra_text_rejected": True,
        "marker_only_default_enabled_across_providers": True,
        "local_agent_started": False,
    }


if __name__ == "__main__":
    result = run()
    print("WEBAGENT_STARTUP_PROTOCOL_FLOW_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
