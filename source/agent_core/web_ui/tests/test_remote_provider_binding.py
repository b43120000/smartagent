"""Remote manager persistence and Telegram execution routing across providers."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core import remote_binding, remote_binding_manager, task_transport
from agent_core.conversation_registry import ConversationRegistry
from agent_core.conversation_identity import conversation_target_matches, same_conversation
from RemoteAgent.telegram_webagent_worker import (
    bind_resolved_execution_page,
    preserve_completed_page,
)
from WebAgent.browser_client import WebAgentBrowserClient

URLS = [
    'https://chatgpt.com/c/remote-test',
    'https://gemini.google.com/app/remote-test',
    'https://claude.ai/chat/remote-test',
    'https://m365.cloud.microsoft/chat/conversation/remote-test',
    'https://custom.example/thread/one?mode=chat',
]

ENTRY_TO_CONVERSATION = {
    'https://chatgpt.com/': 'https://chatgpt.com/c/resolved-chat',
    'https://claude.ai/': 'https://claude.ai/chat/resolved-chat',
    'https://gemini.google.com/': 'https://gemini.google.com/app/resolved-chat',
    'https://copilot.cloud.microsoft/':
        'https://copilot.cloud.microsoft/chat/conversation/resolved-chat',
}


class RemoteProviderBindingTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[4] / 'localdata' / 'test_tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = ConversationRegistry(self.root / 'registry.json')
        self.registry.load()
        self.task = SimpleNamespace(workspace=str(self.root), conversation_url='telegram://chat/123',
            metadata={'transport': 'TELEGRAM', 'authorized': True},
            reply_route={'transport': 'TELEGRAM', 'conversation_key': 'telegram://chat/123', 'chat_id': 123})

    def test_save_reload_and_telegram_resolution_for_every_provider(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(remote_binding, 'active', return_value=None):
            os.environ.pop('SMARTAGENT_REMOTE_EXECUTION_URL', None)
            for url in URLS:
                with self.subTest(url=url):
                    saved = remote_binding.save(dict(workspace=str(self.root), gpt_url=url,
                        skill_path=str(self.root)), self.root)
                    self.assertEqual(remote_binding.load(self.root), saved)
                    self.registry.upsert_binding(str(self.root), url)
                    os.environ['SMARTAGENT_REMOTE_EXECUTION_URL'] = url
                    self.assertEqual(task_transport.resolve_task_execution_chatgpt_url(
                        self.task, registry=self.registry), url)

    def test_telegram_manager_transaction_preserves_selected_provider(self):
        manager = remote_binding_manager.RemoteBindingManager(self.root)
        remote_binding.save(dict(workspace=str(self.root), gpt_url=URLS[0], skill_path=str(self.root)), self.root)
        for url in [*URLS, *ENTRY_TO_CONVERSATION]:
            with patch.object(remote_binding_manager, 'access_snapshot',
                    return_value={'writable_workspaces': [str(self.root)], 'read_only_roots': []}), \
                 patch.object(remote_binding_manager, 'require_writable_workspace', return_value=str(self.root)):
                text = manager.begin(chat_id=123)
                start = text.index(remote_binding_manager.UPDATE_OPEN)
                end = text.index(remote_binding_manager.UPDATE_CLOSE) + len(remote_binding_manager.UPDATE_CLOSE)
                block = text[start:end].replace('webgpt_url=' + URLS[0], 'webgpt_url=' + url)
                applied = []
                manager.apply(block, chat_id=123, runtime_apply=applied.append)
                self.assertEqual(applied[0]['gpt_url'], url)
                with self.assertRaisesRegex(ValueError, 'transaction_unknown'):
                    manager.apply(block, chat_id=123, runtime_apply=applied.append)

    def test_invalid_urls_and_cross_provider_identity(self):
        for url in ['file:///test', 'javascript:alert(1)', 'https://a:b@custom.example/chat', 'https://']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                remote_binding.validate(dict(workspace=str(self.root), gpt_url=url))
        self.assertFalse(same_conversation(URLS[0], URLS[2]))
        self.assertFalse(same_conversation(URLS[4], 'https://different.example/thread/one?mode=chat'))

    def test_remote_binding_accepts_provider_entry_urls(self):
        for entry in ENTRY_TO_CONVERSATION:
            with self.subTest(entry=entry):
                saved = remote_binding.save(dict(
                    workspace=str(self.root), gpt_url=entry, skill_path=str(self.root)
                ), self.root)
                self.assertEqual(saved['gpt_url'], entry)
                self.assertEqual(remote_binding.load(self.root)['gpt_url'], entry)

    def test_entry_target_resolves_once_and_pins_exact_conversation(self):
        for entry, resolved in ENTRY_TO_CONVERSATION.items():
            with self.subTest(entry=entry):
                scraper = SimpleNamespace()
                self.assertTrue(conversation_target_matches(resolved, entry))
                self.assertEqual(
                    bind_resolved_execution_page(scraper, entry, resolved), resolved
                )
                self.assertEqual(scraper._expected_execution_url, resolved)

    def test_entry_resolution_rejects_unidentified_and_wrong_provider_pages(self):
        entry = 'https://chatgpt.com/'
        for actual in (
            'https://chatgpt.com/',
            'https://claude.ai/chat/resolved-chat',
            'https://gemini.google.com/app/resolved-chat',
        ):
            with self.subTest(actual=actual):
                self.assertFalse(conversation_target_matches(actual, entry))
                with self.assertRaisesRegex(RuntimeError, 'remote_execution_page_changed'):
                    bind_resolved_execution_page(SimpleNamespace(), entry, actual)

    def test_execution_guards_reject_same_id_on_different_provider(self):
        expected = 'https://chatgpt.com/c/shared-id'
        actual = 'https://claude.ai/chat/shared-id'
        scraper = SimpleNamespace(
            _expected_execution_url=expected,
            _page=SimpleNamespace(url=actual),
            release_conversation_owner=lambda: 0,
        )
        client = WebAgentBrowserClient(scraper, governor=object())
        with self.assertRaisesRegex(RuntimeError, 'remote_execution_page_changed'):
            client._assert_expected_page('test')
        page = SimpleNamespace(url=actual, closed=False, is_closed=lambda: False)
        with self.assertRaisesRegex(RuntimeError, 'remote_completion_page_not_preserved'):
            preserve_completed_page(page, scraper, None, expected)

    def test_execution_guard_rejects_empty_or_credentialed_page_url(self):
        expected = 'https://chatgpt.com/c/shared-id'
        for actual in ('', 'https://user:password@chatgpt.com/c/shared-id'):
            with self.subTest(actual=actual):
                scraper = SimpleNamespace(
                    _expected_execution_url=expected,
                    _page=SimpleNamespace(url=actual),
                )
                client = WebAgentBrowserClient(scraper, governor=object())
                with self.assertRaisesRegex(RuntimeError, 'remote_execution_page_changed'):
                    client._assert_expected_page('test')

    def test_completion_guard_accepts_pinned_resolved_conversation_alias(self):
        expected = 'https://chatgpt.com/c/resolved-chat'
        page = SimpleNamespace(
            url=expected + '?model=auto', closed=False, is_closed=lambda: False
        )
        scraper = SimpleNamespace(release_conversation_owner=lambda: 1)
        attached = SimpleNamespace(stopped=False)
        attached.stop = lambda: setattr(attached, 'stopped', True)
        handoff = preserve_completed_page(page, scraper, attached, expected)
        self.assertEqual(handoff['conversation_url'], page.url)
        self.assertEqual(handoff['remaining_owners'], 1)
        self.assertTrue(attached.stopped)


if __name__ == '__main__':
    unittest.main()
