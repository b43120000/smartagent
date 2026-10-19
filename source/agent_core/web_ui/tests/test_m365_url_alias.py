"""The enterprise Copilot URL shares the M365 adapter and saved configuration."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ..factory import create_web_ui_for_page,normalize_web_conversation_url
from ..provider_registry import provider_from_url
from ..base_adapter import BaseWebUIAdapter
from ...web_provider_routing import provider_for_url,web_model_key_for_url,endpoint_for_url

class M365URLAliasTests(unittest.TestCase):
    def test_alias_routes_through_existing_m365_entrypoints(self):
        for host in ('m365.cloud.microsoft','copilot.cloud.microsoft'):
            for path in ('/chat/conversation','/chat/conversation/fixture?from=test'):
                url='https://'+host+path
                with self.subTest(url=url):
                    self.assertEqual(normalize_web_conversation_url(url),url)
                    self.assertEqual(provider_from_url(url),'m365')
                    self.assertEqual(provider_for_url(url),'m365')
                    self.assertEqual(web_model_key_for_url(url),'web_m365')
                    self.assertEqual(endpoint_for_url(url),'m365.cloud.microsoft')
                    adapter=create_web_ui_for_page(SimpleNamespace(url=url))
                    self.assertEqual(adapter.provider_name,'m365')
                    with patch.object(BaseWebUIAdapter,'page_reachable',return_value=True):
                        self.assertTrue(adapter.page_reachable())
    def test_unrelated_and_lookalike_hosts_do_not_match(self):
        for host in ('copilot.microsoft.com','copilot.cloud.microsoft.example.org','fakecopilot.cloud.microsoft'):
            with self.subTest(host=host):
                self.assertNotEqual(provider_from_url('https://'+host+'/chat/conversation'),'m365')

if __name__=='__main__':unittest.main()
