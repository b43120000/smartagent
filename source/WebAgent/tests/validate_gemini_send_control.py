"""Gemini-only send selection and diagnostic regressions; no live submission."""
from dataclasses import replace
from unittest.mock import patch

from agent_core.web_ui.providers.gemini.adapter import GeminiUIAdapter
from agent_core.web_ui.providers.gemini.profiles import GEMINI_WEB_2026_PROFILE
from WebAgent.tests.validate_gemini_provider_support import Page, Element, Locator


class SendPage(Page):
    def __init__(self, buttons):
        super().__init__()
        self.buttons = buttons

    def locator(self, selector):
        if selector == GEMINI_WEB_2026_PROFILE.send_selector:
            return Locator(self.buttons)
        return super().locator(selector)


def run():
    profile = GEMINI_WEB_2026_PROFILE
    assert "gem-icon-button > button[aria-label='傳送訊息']" in profile.send_selector
    assert "button[aria-label*='Send message' i]" in profile.send_selector
    assert "button[data-test-id='send-button']" in profile.send_selector
    with patch('agent_core.web_ui.providers.gemini.adapter._LOG') as log:
        page = SendPage([])
        adapter = GeminiUIAdapter(page)
        adapter.profile = profile  # Independent of locally calibrated profiles.
        assert adapter.probe_compatibility().supported  # Idle is allowed.
        assert adapter.send_control() is None
        assert adapter.send_control() is None
        assert log.warning.call_count == 1
        assert log.warning.call_args.args[1:] == (profile.name, profile.send_selector, 0, 0)
        hidden = Element(visible=False)
        shown = Element(attrs={'aria-label': '傳送訊息'})
        page.buttons = [shown, hidden]
        assert adapter.send_control() is shown
        page.buttons = [hidden]
        assert adapter.send_control() is hidden  # Not a readiness bypass.
        assert log.warning.call_count == 2
        page.buttons = []
        assert adapter.send_control() is None
        assert log.warning.call_count == 3
        # Explicit calibrated profiles remain authoritative (no builtin fallback).
        adapter.profile = replace(profile, name='calibrated', send_selector='button.custom')
        adapter._compatibility_report = None
        assert adapter.send_control() is None
        assert log.warning.call_args.args[1:3] == ('calibrated', 'button.custom')
    print('GEMINI_SEND_CONTROL_OK')


if __name__ == '__main__':
    run()
