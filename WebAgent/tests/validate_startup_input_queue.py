#!/usr/bin/env python3
"""Verify startup capture queues humans and ignores controller submissions."""
from __future__ import annotations

import inspect
import json

from playwright.sync_api import sync_playwright

from WebAgent import controller
from WebAgent.browser_bridge import BrowserInputBridge


def run() -> dict:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<textarea id="prompt-textarea"></textarea>'
            '<button data-testid="send-button">Send</button>'
        )
        bridge = BrowserInputBridge(page)
        bridge.install()

        page.locator("#prompt-textarea").fill("queued while bootstrap is running")
        page.locator("button[data-testid=send-button]").click()
        captured = bridge.pop()
        assert captured and captured["text"] == "queued while bootstrap is running"
        assert page.locator("#prompt-textarea").input_value() == ""

        page.locator("#prompt-textarea").fill("internal protocol bootstrap")
        page.evaluate("() => { window.__webAgentAutomationSubmit = true; }")
        page.locator("button[data-testid=send-button]").click()
        page.evaluate("() => { window.__webAgentAutomationSubmit = false; }")
        assert bridge.pop() is None
        browser.close()

    source = inspect.getsource(controller.main)
    install_at = source.index("bridge.install(reset_legacy=True)")
    session_at = source.index("session = ensure_webagent_session(")
    assert install_at < session_at
    return {
        "human_input_is_queued": True,
        "captured_composer_is_cleared": True,
        "automation_submit_is_not_captured": True,
        "bridge_installs_before_session_bootstrap": True,
    }


if __name__ == "__main__":
    result = run()
    print("WEBAGENT_STARTUP_INPUT_QUEUE_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
