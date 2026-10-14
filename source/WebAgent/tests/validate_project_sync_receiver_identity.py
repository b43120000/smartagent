#!/usr/bin/env python3
"""Offline regression tests for project-sync browser receiver binding."""
from __future__ import annotations

import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.project_sync_protocol import ProjectSyncProtocolError
from agent_core.project_sync_receiver import browser_receiver_identity


class FakePage:
    def __init__(self, url: str):
        self.url = url


class FakeScraper:
    def __init__(self, actual_url: str, expected_url: str = ""):
        self._page = FakePage(actual_url)
        self._expected_execution_url = expected_url
        self.service = "chatgpt"


def expect_error(actual: str, expected: str, marker: str) -> None:
    try:
        browser_receiver_identity(FakeScraper(actual, expected))
    except ProjectSyncProtocolError as exc:
        assert marker in str(exc), str(exc)
        return
    raise AssertionError(f"expected {marker}")


def run() -> dict:
    project_url = (
        "https://chatgpt.com/g/g-p-bf0e669e5e7c53079bfea69b765f3a49-"
        "examplespace/c/e5b6e288-90e5-5f82-8ac2-cff9a281a2ef"
    )
    canonical = browser_receiver_identity(FakeScraper(project_url, project_url))
    assert canonical == "https://chatgpt.com/c/e5b6e288-90e5-5f82-8ac2-cff9a281a2ef"

    expect_error(
        project_url,
        "https://chatgpt.com/c/different-conversation",
        "project_sync_receiver_changed",
    )
    expect_error("https://chatgpt.com/", "", "project_sync_receiver_identity_unavailable")
    expect_error(
        "https://claude.ai/chat/16a81290-52f3-5e53-8328-092c5aefe555",
        "",
        "project_sync_receiver_provider_not_implemented:claude",
    )
    expect_error(
        "https://gemini.google.com/u/1/app/a3ed636af167597f",
        "",
        "project_sync_receiver_provider_not_implemented:gemini",
    )

    return {
        "chatgpt_project_url_canonicalized": True,
        "expected_conversation_enforced": True,
        "generic_page_rejected": True,
        "claude_gemini_fail_closed": True,
    }


if __name__ == "__main__":
    print(run())
