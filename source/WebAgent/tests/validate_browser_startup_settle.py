"""Offline test for the doubled WebDirect pre-session startup delay."""
from __future__ import annotations

import unittest

from WebAgent.controller import (
    INITIAL_SESSION_SETTLE_SEC,
    wait_for_initial_session_settle,
)


class FakePage:
    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed


class BrowserStartupSettleTests(unittest.TestCase):
    def test_startup_delay_is_double_two_second_baseline(self):
        page = FakePage("https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc")
        waits = []
        wait_for_initial_session_settle(
            page, "12345678-1234-1234-1234-123456789abc",
            sleep_fn=waits.append,
        )
        self.assertEqual(INITIAL_SESSION_SETTLE_SEC, 4.0)
        self.assertEqual(waits, [4.0])

    def test_conversation_identity_changed_after_wait_is_rejected(self):
        page = FakePage("https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc")
        def switch_page(_delay):
            page.url = "https://chatgpt.com/c/ffffffff-ffff-ffff-ffff-ffffffffffff"
        with self.assertRaisesRegex(RuntimeError, "conversation_changed_after_settle"):
            wait_for_initial_session_settle(
                page, "12345678-1234-1234-1234-123456789abc",
                sleep_fn=switch_page,
            )

    def test_closed_page_after_wait_is_rejected(self):
        page = FakePage("https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc")
        def close_page(_delay):
            page.closed = True
        with self.assertRaisesRegex(RuntimeError, "page_closed_after_settle"):
            wait_for_initial_session_settle(
                page, "12345678-1234-1234-1234-123456789abc",
                sleep_fn=close_page,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
