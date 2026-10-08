#!/usr/bin/env python3
"""Deterministic acceptance checks for planner-assisted UI calibration."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from agent_core.ui_calibration import (
    PLAN_SCHEMA,
    CalibrationError,
    _progress_percent,
    _start_scraper,
    parse_planner_response,
)
from agent_core.web_runtime import WebLLMScraper
from agent_core.web_ui.profile_store import (
    ProfileValidationError,
    build_profile_document,
    load_active_document,
    profile_values,
    publish_profile,
    rollback_profile,
    validate_selector_payload,
)


SELECTORS = {
    "composer_selector": "div[contenteditable='true'][role='textbox']",
    "send_selector": "button[data-testid='send-button']",
    "user_turn_selector": "[data-message-author-role='user']",
    "user_content_selector": ".whitespace-pre-wrap",
    "assistant_turn_selector": "[data-message-author-role='assistant']",
    "final_content_selector": ".markdown",
    "stop_selectors": ["button[data-testid='stop-button']"],
    "busy_selectors": ["[aria-busy='true']"],
}


def _expect_error(error_type, callback, contains: str) -> None:
    try:
        callback()
    except error_type as exc:
        assert contains in str(exc), str(exc)
    else:
        raise AssertionError(f"expected {error_type.__name__}: {contains}")


def run() -> dict:
    shared_playwright = object()
    ownership_probe = WebLLMScraper.__new__(WebLLMScraper)
    ownership_probe._pw = None
    ownership_probe._owns_playwright = True
    ownership_probe._bind_playwright(shared_playwright)
    assert ownership_probe._pw is shared_playwright
    assert ownership_probe._owns_playwright is False

    assert _progress_percent(4, 10) == 40
    assert _progress_percent(0, 8) == 0
    assert _progress_percent(8, 8) == 100

    import agent_core.web_runtime as web_runtime_module

    original_scraper = web_runtime_module.WebLLMScraper

    class FakeScraper:
        def __init__(self, service, headless):
            self.service = service
            self.headless = headless
            self.cfg = {"url": "default"}
            self.started_with = None

        def start(self, show_browser, *, playwright=None):
            self.started_with = (show_browser, playwright)

    try:
        web_runtime_module.WebLLMScraper = FakeScraper
        target_probe = _start_scraper(
            "gemini", "https://gemini.google.com/app/test",
            playwright=shared_playwright,
        )
    finally:
        web_runtime_module.WebLLMScraper = original_scraper
    assert target_probe.cfg["url"] == "https://gemini.google.com/app/test"
    assert target_probe.started_with == (True, shared_playwright)

    plan = {
        "schema": PLAN_SCHEMA,
        "provider": "chatgpt",
        "selectors": SELECTORS,
        "notes": "bounded selector data only",
    }
    parsed = parse_planner_response(
        "planner preface that is ignored\n" + json.dumps(plan),
        expected_provider="chatgpt",
    )
    assert parsed["selectors"]["stop_selectors"] == ("button[data-testid='stop-button']",)

    unsafe = dict(SELECTORS)
    unsafe["composer_selector"] = "xpath=//textarea"
    _expect_error(
        ProfileValidationError,
        lambda: validate_selector_payload("chatgpt", unsafe),
        "unsafe_selector:composer_selector",
    )
    extra = dict(plan)
    extra["python"] = "do_not_execute()"
    _expect_error(
        CalibrationError,
        lambda: parse_planner_response(json.dumps(extra), expected_provider="chatgpt"),
        "planner_unknown_fields:python",
    )
    gemini_selectors = validate_selector_payload("gemini", SELECTORS)
    assert gemini_selectors["composer_selector"] == SELECTORS["composer_selector"]

    with tempfile.TemporaryDirectory(prefix="smartagent-ui-profile-") as temp:
        root = Path(temp)
        rejected = build_profile_document(
            "chatgpt", SELECTORS,
            source_url="https://chatgpt.com/c/target",
            planner_url="https://chatgpt.com/c/planner",
            validation={"passed": False},
        )
        _expect_error(
            ProfileValidationError,
            lambda: publish_profile(rejected, root=root),
            "profile_not_validated",
        )
        assert load_active_document("chatgpt", root=root) is None

        accepted = build_profile_document(
            "chatgpt", SELECTORS,
            source_url="https://chatgpt.com/c/target",
            planner_url="https://chatgpt.com/c/planner",
            validation={
                "passed": True,
                "user_turn_confirmed": True,
                "assistant_turn_confirmed": True,
                "final_content_confirmed": True,
            },
        )
        active = publish_profile(accepted, root=root)
        assert active.is_file()
        assert len(list((root / "chatgpt" / "history").glob("*.json"))) == 1
        loaded = load_active_document("chatgpt", root=root)
        assert loaded and loaded["name"] == accepted["name"]
        values = profile_values("chatgpt", name=accepted["name"], root=root)
        assert values and values["composer_selector"] == SELECTORS["composer_selector"]
        assert values["stop_selectors"] == ("button[data-testid='stop-button']",)

        # A malformed replacement is never allowed to overwrite active.json.
        before = active.read_bytes()
        malformed = dict(accepted)
        malformed["selectors"] = dict(accepted["selectors"])
        malformed["selectors"]["send_selector"] = "javascript:alert(1)"
        _expect_error(
            ProfileValidationError,
            lambda: publish_profile(malformed, root=root),
            "unsafe_selector:send_selector",
        )
        assert active.read_bytes() == before

        second = build_profile_document(
            "chatgpt", {**SELECTORS, "send_selector": "button[type='submit']"},
            source_url="https://chatgpt.com/c/target",
            planner_url="https://chatgpt.com/c/planner",
            validation={"passed": True},
        )
        publish_profile(second, root=root)
        rollback_profile("chatgpt", root=root)
        rolled_back = load_active_document("chatgpt", root=root)
        assert rolled_back and rolled_back["name"] == accepted["name"]

    result = {
        "component_progress_percent": True,
        "strict_planner_schema": True,
        "shared_playwright_driver": True,
        "unsafe_selector_rejected": True,
        "gemini_profile_schema_supported": True,
        "publish_requires_live_pass": True,
        "atomic_active_profile": True,
        "history_preserved": True,
        "rollback_previous_valid_profile": True,
    }
    print("UI_CALIBRATION_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
