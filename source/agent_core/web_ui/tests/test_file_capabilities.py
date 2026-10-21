"""Planner file capabilities follow live validated calibration, never native fallback."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from .. import dom_config
from ..file_config import web_file_capabilities, save_files
from ..provider_registry import provider_from_url
from ..dialogue_state import DEFAULT_BLOCKING_RULES
from ...web_runtime import WebLLMScraper, WebScraperStageError
from ...tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext


class FileCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_path = dom_config.config_path
        self.template = {
            'schema': dom_config.SCHEMA,
            'elements': {name: {'selector': '#fixture-' + name,
                'validation': {dom_config.VALIDATION_KEYS[name]: True}}
                for name in dom_config.ELEMENT_FIELDS},
            'capabilities': {
                'generation_state': {'strategy': 'stop_busy_and_stable_text', 'quiet_seconds': 5,
                                     'validation': {'lifecycle_verified': True}},
                'blocking_state': {'rules': DEFAULT_BLOCKING_RULES, 'validation': {'rules_verified': True}},
                'conversation_loop': {'validation': {'loop_verified': True}},
            },
            'files': {'entry': '#entry', 'input': '#file', 'chip': '#chip', 'remove': '#remove',
                      'download': '#download', 'attachment_probe': {'completion': 'busy_idle'},
                      'entry_verified': True, 'upload_verified': True, 'attachment_verified': True,
                      'attachment_inventory_verified': True, 'download_verified': True},
        }
        patched = patch.object(dom_config, 'config_path', side_effect=lambda provider, root=None:
            self.original_path(provider, root=root or self.root))
        patched.start()
        self.addCleanup(patched.stop)

    def write(self, url='https://gemini.google.com', *, files=True, roundtrip=True):
        provider = provider_from_url(url)
        document = copy.deepcopy(self.template)
        document['provider'] = provider
        parsed = urlsplit(url)
        document['source_origin'] = parsed.scheme + '://' + parsed.netloc
        if not files:
            document['files'] = {}
        elif roundtrip:
            document['files']['roundtrip'] = True
        else:
            document['files'].pop('roundtrip', None)
        path = dom_config.config_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding='utf-8')
        return provider, path

    def scraper(self, provider):
        scraper = object.__new__(WebLLMScraper)
        scraper.service = provider
        scraper._web_ui_adapter = Mock(return_value=SimpleNamespace(provider_name=provider))
        return scraper

    def test_only_complete_calibration_enables_files_for_all_providers(self):
        for url in ('https://chatgpt.com', 'https://claude.ai', 'https://gemini.google.com',
                    'https://m365.cloud.microsoft', 'https://custom.example'):
            for files, roundtrip, enabled in ((False, False, False), (True, False, False), (True, True, True)):
                with self.subTest(url=url, files=files, roundtrip=roundtrip):
                    provider, _ = self.write(url, files=files, roundtrip=roundtrip)
                    caps = web_file_capabilities(provider)
                    self.assertIs(caps['browser_upload'], enabled)
                    self.assertIs(caps['browser_download'], enabled)

    def test_missing_corrupt_or_invalid_proofs_fail_closed(self):
        self.assertFalse(web_file_capabilities('gemini')['browser_upload'])
        provider, path = self.write()
        for value in ('{invalid', json.dumps({'schema': 'invalid'})):
            path.write_text(value, encoding='utf-8')
            self.assertFalse(web_file_capabilities(provider)['browser_download'])
        provider, path = self.write()
        document = json.loads(path.read_text(encoding='utf-8'))
        document['files'].pop('attachment_probe')
        path.write_text(json.dumps(document), encoding='utf-8')
        self.assertFalse(web_file_capabilities(provider)['browser_upload'])

    def test_prompt_updates_each_round_and_recalibration_revokes_files(self):
        provider, _ = self.write(files=False)
        scraper = self.scraper(provider)
        first = scraper._web_file_planner_prompt('request one')
        self.assertIn('"browser_upload":false', first)
        self.assertIn('project_sync INDEX_ONLY', first)
        self.assertTrue(first.startswith('request one'))
        self.write()
        second = scraper._web_file_planner_prompt('request two')
        self.assertIn('"browser_upload":true', second)
        self.assertIn('"browser_download":true', second)
        save_files(provider, {'download': 'a.changed-download', 'download_verified': True})
        third = scraper._web_file_planner_prompt('request three')
        self.assertIn('"browser_download":false', third)

    def test_runtime_blocks_upload_before_composer_and_download_before_fallback(self):
        provider, _ = self.write(files=False)
        scraper = self.scraper(provider)
        scraper._page = SimpleNamespace(url='https://gemini.google.com/app/fixture')
        scraper._check_provider_transport_error = Mock()
        scraper._recover_composer_before_request = Mock()
        scraper._upload_one_attachment = Mock()
        scraper._emit_status = Mock()
        with self.assertRaisesRegex(WebScraperStageError, 'WEB_FILE_TRANSFER_UNAVAILABLE'):
            scraper.ask('request', attachment_paths=['fixture.txt'])
        scraper._recover_composer_before_request.assert_not_called()
        with self.assertRaisesRegex(WebScraperStageError, 'WEB_FILE_TRANSFER_UNAVAILABLE'):
            scraper._upload_attachments(['fixture.txt'])
        scraper._upload_one_attachment.assert_not_called()
        result = scraper.download_latest_artifact(str(self.root / 'output.txt'))
        self.assertEqual(result['status'], 'ARTIFACT_DOWNLOAD_UNAVAILABLE')
        self.assertFalse(result['executed'])
        with self.assertRaisesRegex(WebScraperStageError, 'WEB_FILE_TRANSFER_UNAVAILABLE'):
            scraper._download_latest_artifact(str(self.root / 'output.txt'))
        self.assertFalse((self.root / 'output.txt').exists())

    def test_complete_calibration_keeps_existing_upload_flow(self):
        provider, _ = self.write()
        scraper = self.scraper(provider)
        scraper._upload_one_attachment = Mock()
        scraper._wait_for_attachment_ui = Mock(return_value=True)
        scraper._emit_status = Mock()
        scraper._upload_attachments(['fixture.txt'])
        scraper._upload_one_attachment.assert_called_once_with('fixture.txt')
        self.assertEqual(scraper._wait_for_attachment_ui.call_count, 2)

    def test_text_only_request_reaches_normal_composer_preparation(self):
        provider, _ = self.write(files=False)
        scraper = self.scraper(provider)
        scraper._web_ui_adapter.return_value.prepare_transport_prompt = lambda prompt: prompt
        scraper._page = SimpleNamespace(url='https://gemini.google.com/app/fixture')
        scraper._check_provider_transport_error = Mock()
        scraper._rate_limited_until = 0
        scraper._cancel_requested = Mock()
        scraper._recover_composer_before_request = Mock(side_effect=LookupError('reached normal preparation'))
        with self.assertRaisesRegex(LookupError, 'reached normal preparation'):
            scraper.ask('text request')
        prepared = scraper._recover_composer_before_request.call_args.args[0]
        self.assertTrue(prepared.startswith('text request'))
        self.assertIn('"browser_upload":false', prepared)

    def test_missing_or_broken_capability_provider_rejects_transfer(self):
        ctx = WebAgentToolContext(self.root)
        for getter in (None, Mock(side_effect=ValueError('invalid provider')), lambda: {}):
            ctx.configure_web_file_capabilities(getter)
            with patch('agent_core.action_dispatcher.dispatch_action') as dispatch:
                self.assertEqual(decode_result(ctx.execute({'tool': 'upload_file'}))['status'], 'REJECTED')
                self.assertEqual(decode_result(ctx.web_download_artifact('out.txt'))['status'], 'REJECTED')
                self.assertEqual(decode_result(ctx.queue_attachments(['fixture.txt']))['status'], 'REJECTED')
                self.assertEqual(ctx.run_project_sync_transaction({})['status'], 'REJECTED')
                dispatch.assert_not_called()

    def test_tool_admission_and_direct_entrypoints_block_only_browser_transfer(self):
        provider, _ = self.write(files=False)
        ctx = WebAgentToolContext(self.root)
        ctx.configure_web_file_capabilities(lambda: web_file_capabilities(provider))
        actions = [{'tool': tool} for tool in ('upload_file', 'upload_files', 'download_artifact')]
        actions += [{'tool': 'project_sync', 'strategy': strategy} for strategy in ('FULL_BUNDLE', 'DELTA')]
        with patch('agent_core.action_dispatcher.dispatch_action', return_value='DISPATCHED') as dispatch:
            for action in actions:
                result = decode_result(ctx.execute(action))
                self.assertEqual(result['status'], 'REJECTED')
                self.assertEqual(result['reason'], 'WEB_FILE_TRANSFER_UNAVAILABLE')
                self.assertFalse(result['executed'])
            dispatch.assert_not_called()
            for action in ({'tool': 'project_sync', 'strategy': 'INDEX_ONLY'},
                           {'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'list_directory'},
                           {'tool': 'run_command'}, {'tool': 'return_artifact'}, {'tool': 'google_drive_upload'}):
                self.assertEqual(ctx.execute(action), 'DISPATCHED')
            self.write()
            for action in actions:
                self.assertEqual(ctx.execute(action), 'DISPATCHED')
        self.write(files=False)
        self.assertEqual(decode_result(ctx.queue_attachments(['fixture.txt']))['status'], 'REJECTED')
        self.assertEqual(ctx.take_pending_attachments(), [])
        self.assertEqual(decode_result(ctx.web_download_artifact('out.txt'))['status'], 'REJECTED')
        self.assertEqual(ctx.run_project_sync_transaction({})['status'], 'REJECTED')

    def test_recovery_prompt_retains_live_capabilities(self):
        provider, _ = self.write(files=False)
        scraper = self.scraper(provider)
        adapter = SimpleNamespace(provider_name=provider,
            prepare_transport_prompt=lambda prompt: 'WUS-fixture\n' + prompt)
        scraper._web_ui_adapter.return_value = adapter
        scraper._log_stage = Mock()
        scraper._protocol_recovery_admission_state = lambda: (True, {})
        scraper._write_prompt_to_composer = Mock()
        scraper._wait_for_send_ready = Mock(return_value='send')
        scraper._capture_request_turn_state = Mock(return_value={})
        scraper._submit_verified_prompt = Mock()
        scraper._wait_for_user_sent = Mock()
        scraper._send_protocol_recovery_probe({}, prompt_override='repair request')
        sent = scraper._write_prompt_to_composer.call_args.args[0]
        self.assertIn('"browser_upload":false', sent)
        scraper._capture_request_turn_state.assert_called_once_with(sent)

    def test_large_results_stay_local_even_when_attachment_is_requested(self):
        from ...result_exchange import prepare_tool_result
        ctx = WebAgentToolContext(self.root)
        action = {'tool': 'read_file', 'action_id': 'READ-RESULT', 'result_transport': 'ATTACHMENT',
                  'full_result_required': True, 'result_purpose': 'Inspect exact diagnostic trace'}
        result = prepare_tool_result(action, 'diagnostic trace\n' * 5000, ctx, force_attachment=True)
        self.assertIn('[SMARTAGENT_RESULT_LOCAL_REF]', result)
        self.assertIn('reason=WEB_FILE_TRANSFER_UNAVAILABLE', result)
        self.assertNotIn('[SMARTAGENT_RESULT_ATTACHMENT]', result)
        self.assertEqual(ctx.take_pending_attachments(), [])

    def test_initial_attachments_cannot_be_silently_dropped(self):
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        planner = Mock()
        loop = WebAgentProtocolLoop(self.root, planner, progress_root=self.root)
        with self.assertRaisesRegex(RuntimeError, 'WEB_FILE_TRANSFER_UNAVAILABLE'):
            loop.run('Inspect attachment', initial_attachments=['fixture.txt'])
        planner.assert_not_called()

    def test_revoked_queue_never_claims_result_attachment_was_sent(self):
        from ...result_exchange import prepare_tool_result
        from ...tool_result_contract import encode_result
        ctx = WebAgentToolContext(self.root)
        ctx.configure_web_file_capabilities(lambda: {'browser_upload': True, 'browser_download': True})
        ctx.queue_attachments = Mock(return_value=encode_result(
            'REJECTED', 'WEB_FILE_TRANSFER_UNAVAILABLE', tool='upload_files'))
        action = {'tool': 'read_file', 'action_id': 'REVOKED-RESULT'}
        result = prepare_tool_result(action, 'diagnostic trace\n' * 5000, ctx, force_attachment=True)
        self.assertIn('[SMARTAGENT_RESULT_LOCAL_REF]', result)
        self.assertIn('reason=WEB_FILE_TRANSFER_UNAVAILABLE', result)
        self.assertNotIn('[SMARTAGENT_RESULT_ATTACHMENT]', result)
        ctx.queue_attachments.assert_called_once()


if __name__ == '__main__':
    unittest.main()
