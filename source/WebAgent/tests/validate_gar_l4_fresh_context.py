"""Offline GAR L4 browser-context admission tests; fake DOM, no network.

PYTHONPATH=source python -B source/WebAgent/tests/validate_gar_l4_fresh_context.py
"""
from __future__ import annotations
import types
import unittest
from unittest.mock import patch

from WebAgent.fresh_context import ExistingConversationFreshAdapter


class Page:
    def __init__(self, url):
        self.url = url
    def is_closed(self):
        return False


class Scraper:
    instances = []
    def __init__(self, service="chatgpt"):
        self.service = service
        self._page = None
        self._browser = None
        self._expected_execution_url = ""
        self._execution_page_lease_owned = False
        self._pending_submit_context = None
        self._released = 0
        self._claimed = []
        self.__class__.instances.append(self)
    def _claim_conversation_owner(self, url):
        self._claimed.append(url)
    def release_conversation_owner(self):
        self._released += 1
    def _is_generation_active(self):
        return False


class Registry:
    references_count = 0
    def __init__(self, root):
        pass
    def references(self, url):
        return self.__class__.references_count


class Client:
    bootstraps = []
    def __init__(self, scraper, governor=None, event_sink=None, display_name=""):
        self.scraper = scraper
        self.governor = governor
        self.event_sink = event_sink
        self.display_name = display_name
    def ask(self, prompt, **kwargs):
        self.__class__.bootstraps.append((self.scraper, prompt, kwargs))
        return "READY-MATCH"


class Protocol:
    def __init__(self, name, version, body):
        pass
    def bootstrap_prompt(self, session_id):
        return "BOOTSTRAP:" + session_id
    def parse_protocol_ready(self, reply, session_id):
        return reply == "READY-MATCH" and session_id.startswith("GAR-L4-")


def make():
    old = Scraper()
    old._page = Page("https://chatgpt.com/c/old")
    old._execution_page_lease_owned = True
    fresh = Page("https://chatgpt.com/c/fresh")
    old._browser = types.SimpleNamespace(pages=[old._page, fresh])
    client = Client(old)
    adapter = ExistingConversationFreshAdapter(
        client=client, workspace="sandbox", session_root="sandbox",
        target_url=fresh.url,
    )
    return old, fresh, adapter


def prepare(adapter):
    with patch("agent_core.web_runtime.WebLLMScraper", Scraper), \
         patch("WebAgent.browser_client.WebAgentBrowserClient", Client), \
         patch("agent_core.conversation_ownership.ConversationOwnershipRegistry", Registry), \
         patch("agent_core.session_protocol.SessionProtocol", Protocol):
        return adapter.prepare(
            request_id="RR-IT", task_id="TASK-IT",
            task_epoch="EPOCH-IT", owner="WEB_COPILOT",
        )


class FreshBrowserAdmissionTests(unittest.TestCase):
    def setUp(self):
        Scraper.instances.clear()
        Client.bootstraps.clear()
        Registry.references_count = 0

    def test_distinct_preopened_page_bootstraps_without_navigation(self):
        old, fresh, adapter = make()
        binding = prepare(adapter)
        self.assertEqual(binding.proof.original_conversation_id, "old")
        self.assertEqual(binding.proof.fresh_conversation_id, "fresh")
        self.assertTrue(binding.proof.protocol_identity_verified)
        self.assertEqual(old._page.url, "https://chatgpt.com/c/old")
        self.assertEqual(len(old._browser.pages), 2)
        self.assertEqual(len(Client.bootstraps), 1)
        self.assertTrue(Client.bootstraps[0][1].startswith("BOOTSTRAP:GAR-L4-"))
        self.assertEqual(binding.planner("test", {}, []), "READY-MATCH")
        binding.close()
        binding.close()
        self.assertEqual(Scraper.instances[-1]._released, 1)
        self.assertEqual(old._released, 0)
        self.assertEqual(old._page.url, "https://chatgpt.com/c/old")

    def test_reject_missing_or_matching_target_without_submission(self):
        for target in ("", "https://chatgpt.com/", "https://chatgpt.com/c/old",
                       "https://claude.ai/chat/other"):
            with self.subTest(target=target):
                old, fresh, adapter = make()
                adapter.target_url = target
                with self.assertRaises(RuntimeError):
                    prepare(adapter)
                self.assertEqual(len(Client.bootstraps), 0)

    def test_reject_no_lease_and_active_or_uncertain_send(self):
        for setup in ("no_lease", "pending", "active"):
            with self.subTest(setup=setup):
                old, fresh, adapter = make()
                if setup == "no_lease":
                    old._execution_page_lease_owned = False
                if setup == "pending":
                    old._pending_submit_context = {"attempted": True}
                if setup == "active":
                    old._is_generation_active = lambda: True
                with self.assertRaises(RuntimeError):
                    prepare(adapter)
                self.assertEqual(len(Client.bootstraps), 0)

    def test_target_with_existing_owner_is_refused(self):
        old, fresh, adapter = make()
        Registry.references_count = 1
        with self.assertRaisesRegex(RuntimeError, "ALREADY_OWNED"):
            prepare(adapter)
        self.assertEqual(Client.bootstraps, [])

    def test_target_missing_or_ambiguous(self):
        for case in ("missing", "duplicate"):
            with self.subTest(case=case):
                old, fresh, adapter = make()
                if case == "missing":
                    old._browser.pages = [old._page]
                else:
                    old._browser.pages.append(Page(fresh.url))
                with self.assertRaisesRegex(RuntimeError, "NOT_UNIQUELY_OWNED"):
                    prepare(adapter)
                self.assertEqual(len(Client.bootstraps), 0)

    def test_bootstrap_identity_failure_releases_only_fresh_owner(self):
        old, fresh, adapter = make()
        class WrongProtocol(Protocol):
            def parse_protocol_ready(self, reply, session_id):
                return False
        with patch("agent_core.web_runtime.WebLLMScraper", Scraper), \
             patch("WebAgent.browser_client.WebAgentBrowserClient", Client), \
             patch("agent_core.conversation_ownership.ConversationOwnershipRegistry", Registry), \
             patch("agent_core.session_protocol.SessionProtocol", WrongProtocol):
            with self.assertRaisesRegex(RuntimeError, "PROTOCOL_IDENTITY_UNVERIFIED"):
                adapter.prepare(
                    request_id="RR-IT", task_id="TASK-IT",
                    task_epoch="EPOCH-IT", owner="WEB_COPILOT",
                )
        self.assertEqual(old._released, 0)
        self.assertEqual(Scraper.instances[-1]._released, 1)

    def test_fail_closed_on_page_switch_after_bootstrap(self):
        old, fresh, adapter = make()
        binding = prepare(adapter)
        fresh.url = "https://chatgpt.com/c/switched"
        with self.assertRaisesRegex(RuntimeError, "PAGE_IDENTITY_CHANGED"):
            binding.planner("some prompt", {}, [])
        self.assertEqual(len(Client.bootstraps), 1)
        binding.close()

    def test_one_adapter_request_only(self):
        old, fresh, adapter = make()
        binding = prepare(adapter)
        with self.assertRaisesRegex(RuntimeError, "ALREADY_USED"):
            prepare(adapter)
        binding.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
