"""Bounded long-bootstrap budget; strict composer failure stays fail closed."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from agent_core import composer_operation as co
from agent_core.web_runtime import WebLLMScraper,WebScraperStageError
class ComposerBudget(unittest.TestCase):
    def scraper(self):
        s=WebLLMScraper.__new__(WebLLMScraper);s.service='chatgpt'
        s._page=SimpleNamespace(url='https://chatgpt.com/c/isolated-budget')
        s.get_cdp_endpoint=lambda:'http://127.0.0.1:12803'
        s._log_stage=lambda *_:None;s._set_request_state=lambda *_:None
        return s
    def test_measured_long_bootstrap_fits_finite_budget(self):
        with patch.object(co,'prepare_composer',return_value={'operation_id':'isolated'}) as prepare:
            self.assertTrue(self.scraper()._write_prompt_to_composer('x'*33060))
        budget=prepare.call_args.kwargs['timeout_sec']
        self.assertGreaterEqual(budget,150)
        self.assertLess(budget,160)
        self.assertIsNotNone(prepare.call_args.kwargs['cancel_check'])
    def test_short_prompt_keeps_existing_budget(self):
        with patch.object(co,'prepare_composer',return_value={'operation_id':'isolated'}) as prepare:
            self.scraper()._write_prompt_to_composer('short')
        self.assertEqual(prepare.call_args.kwargs['timeout_sec'],45)
    def test_timeout_still_blocks_submit_and_auto_retry(self):
        s=self.scraper()
        with patch.object(co,'prepare_composer',side_effect=co.ComposerOperationError('composer_write_unconfirmed:WRITER_TIMEOUT')):
            with self.assertRaises(WebScraperStageError) as error:s._write_prompt_to_composer('x'*33060)
        self.assertFalse(error.exception.safe_to_retry)
        self.assertTrue(s._composer_io_unconfirmed)
if __name__=='__main__':unittest.main()
