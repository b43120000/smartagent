import subprocess
import unittest
from unittest.mock import patch
from agent_core import composer_operation as co
from agent_core.web_runtime import WebLLMScraper
class ObserverTests(unittest.TestCase):
    def test_connection_retry_is_read_only(self):
        with patch.object(co,'run_helper',side_effect=[{'status':'CDP_CONNECT_TIMEOUT'},{'status':'VERIFIED_SAFE_TO_SUBMIT'}]) as h:
            self.assertEqual(co.observe_composer({'mode':'write'},8)['status'],'VERIFIED_SAFE_TO_SUBMIT')
            self.assertEqual(h.call_count,2)
            self.assertTrue(all(c.args[0]['mode']=='observe' for c in h.call_args_list))
    def test_retry_is_bounded(self):
        with patch.object(co,'run_helper',return_value={'status':'CDP_CONNECT_TIMEOUT'}) as h:
            self.assertEqual(co.observe_composer({},8)['status'],'CDP_CONNECT_TIMEOUT')
            self.assertEqual(h.call_count,2)
    def test_partial_draft_is_not_retried(self):
        with patch.object(co,'run_helper',return_value={'status':'PARTIAL_OR_EMPTY'}) as h:
            self.assertEqual(co.observe_composer({},8)['status'],'PARTIAL_OR_EMPTY')
            self.assertEqual(h.call_count,1)
    def test_deadline_is_reported(self):
        with patch.object(co,'run_helper',side_effect=subprocess.TimeoutExpired('observer',15)):
            self.assertEqual(co.observe_composer({},8)['error_code'],'OBSERVER_TIMEOUT')
    def test_status(self):
        self.assertEqual(WebLLMScraper._status_from_web_stage('composer_bounded_failed')[0],'COMPOSER_CONFIRMATION_FAILED')
