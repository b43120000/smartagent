#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression coverage for recovery-turn reload/readback."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.web_runtime import WebLLMScraper, WebScraperStageError


class FakeElement:
    def __init__(self, element_id: str, text: str):
        self.element_id = element_id
        self.text = text

    def get_attribute(self, name: str):
        return self.element_id if name == "data-message-id" else None

    def inner_text(self):
        return self.text


class FakePage:
    def __init__(self, before: dict, after: dict, *, url: str):
        self.before = before
        self.after = after
        self.url = url
        self.reloaded = 0
        self.goto_calls = []
        self.after_reload = False

    def reload(self, *, wait_until: str, timeout: int):
        assert wait_until == "domcontentloaded"
        assert timeout == 60000
        self.reloaded += 1
        self.after_reload = True

    def goto(self, url: str, *, wait_until: str, timeout: int):
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url

    def query_selector_all(self, selector: str):
        state = self.after if self.after_reload else self.before
        return list(state.get(selector, []))


def state(*, users: list, assistants: list, responses: list) -> dict:
    return {"user": users, "assistant": assistants, "response": responses}


def make_scraper(before: dict, after: dict):
    scraper = WebLLMScraper.__new__(WebLLMScraper)
    scraper.cfg = {
        "user_turn_selector": "user",
        "assistant_turn_selector": "assistant",
        "response_selector": "response",
    }
    scraper._page = FakePage(
        before,
        after,
        url="https://chatgpt.com/c/readback-test",
    )
    scraper._claimed_conversation_id = "readback-test"
    scraper._execution_page_lease_owned = True
    scraper._activity_observer_installed = True
    scraper._protocol_recovery_readback_grace_sec = 0.01
    scraper._run_control_hook = lambda: False
    scraper._check_cancel_requested = lambda _stage: None
    scraper._is_generation_active = lambda: False
    scraper._logs = []
    scraper._log_stage = lambda stage, detail="": scraper._logs.append((stage, detail))
    return scraper


def run() -> dict:
    old_user = FakeElement("u-old", "old user")
    old_assistant = FakeElement("a-old", "old assistant")
    old_response = FakeElement("r-old", "old response")
    before = state(
        users=[old_user],
        assistants=[old_assistant],
        responses=[old_response],
    )

    # A full fresh assistant turn is accepted only together with the already
    # submitted recovery user turn.
    fresh_user = FakeElement("u-recovery", "recovery prompt")
    fresh_assistant = FakeElement("a-recovery", "recovery answer")
    fresh_response = FakeElement("r-recovery", "recovery response")
    scraper = make_scraper(
        before,
        state(
            users=[old_user, fresh_user],
            assistants=[old_assistant, fresh_assistant],
            responses=[old_response, fresh_response],
        ),
    )
    snapshot = scraper._snapshot_turn_state()
    found = scraper._reload_and_recheck_protocol_recovery(snapshot)
    assert found is fresh_assistant
    assert scraper._page.reloaded == 1
    assert scraper._page.goto_calls == []
    assert scraper._activity_observer_installed is False

    # A response-only DOM variant is also accepted, while the caller remains
    # responsible for extracting and validating its matching turn_commit.
    response_only = make_scraper(
        before,
        state(
            users=[old_user, fresh_user],
            assistants=[old_assistant],
            responses=[old_response, fresh_response],
        ),
    )
    assert response_only._reload_and_recheck_protocol_recovery(
        response_only._snapshot_turn_state()
    ) is None

    # Reload must not make an old assistant answer eligible merely because the
    # recovery user turn exists.
    stale = make_scraper(
        before,
        state(
            users=[old_user, fresh_user],
            assistants=[old_assistant],
            responses=[old_response],
        ),
    )
    try:
        stale._reload_and_recheck_protocol_recovery(stale._snapshot_turn_state())
        raise AssertionError("stale assistant response was accepted")
    except WebScraperStageError as exc:
        assert exc.stage == "protocol_recovery_assistant_stalled"
        assert exc.safe_to_retry is False
    assert stale._page.reloaded == 1

    # The same one-reload/read-only mechanism also covers the original request
    # before any protocol-repair turn exists, with a distinct terminal error.
    initial_stale = make_scraper(
        before,
        state(
            users=[old_user, fresh_user],
            assistants=[old_assistant],
            responses=[old_response],
        ),
    )
    try:
        initial_stale._reload_and_recheck_protocol_recovery(
            initial_stale._snapshot_turn_state(),
            context="initial_request",
        )
        raise AssertionError("initial assistant stall was accepted")
    except WebScraperStageError as exc:
        assert exc.stage == "assistant_started_stalled_after_readback"
        assert "WEB_ASSISTANT_START_STALLED_AFTER_READBACK" in str(exc)
        assert exc.safe_to_retry is False
    assert initial_stale._page.reloaded == 1
    assert any(
        stage == "assistant_start_readback_reload"
        for stage, _detail in initial_stale._logs
    )

    # Conversation ownership is checked before reload.
    wrong_target = make_scraper(before, before)
    wrong_target._claimed_conversation_id = "different-conversation"
    try:
        wrong_target._reload_and_recheck_protocol_recovery(
            wrong_target._snapshot_turn_state()
        )
        raise AssertionError("wrong conversation was reloaded")
    except WebScraperStageError as exc:
        assert exc.stage == "protocol_recovery_readback_target"
    assert wrong_target._page.reloaded == 0

    # If reload reveals active generation, resume the existing progress-aware
    # waiter without any send operation.
    active = make_scraper(
        before,
        state(
            users=[old_user, fresh_user],
            assistants=[old_assistant],
            responses=[old_response],
        ),
    )
    active._is_generation_active = lambda: True
    sentinel = object()
    active._wait_for_new_assistant_turn = lambda current_snapshot: sentinel
    assert active._reload_and_recheck_protocol_recovery(
        active._snapshot_turn_state()
    ) is sentinel

    helper_source = inspect.getsource(
        WebLLMScraper._reload_and_recheck_protocol_recovery
    )
    assert "_write_prompt_to_composer" not in helper_source
    assert "_submit_verified_prompt" not in helper_source
    assert "no_prompt_resubmit=true" in helper_source

    wait_source = inspect.getsource(WebLLMScraper._wait_for_response_complete)
    assert 'exc.stage != "assistant_started_stalled"' in wait_source
    assert "_reload_and_recheck_protocol_recovery(snapshot)" in wait_source
    assert 'context="initial_request"' in wait_source
    assert "assistant_start_readback_armed" in wait_source

    result = {
        "same_conversation_reload_once": True,
        "fresh_assistant_readback_accepted": True,
        "response_only_dom_supported": True,
        "stale_assistant_rejected": True,
        "initial_assistant_stall_readback": True,
        "conversation_ownership_preserved": True,
        "active_generation_resumes_waiter": True,
        "no_prompt_resubmit": True,
        "assistant_start_stall_scope": True,
    }
    print("PROTOCOL_RECOVERY_READBACK_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
