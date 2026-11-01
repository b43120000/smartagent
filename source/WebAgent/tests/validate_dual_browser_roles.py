"""Offline browser-role selection, configuration and GAR attachment regression."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from agent_core import browser_preferences as prefs
from WebAgent.fresh_context import prepare_gar_browser_context


class BrowserRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "browser_session.json"
        patcher = patch.object(prefs, "settings_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        lock = patch.object(prefs, "exclusive_process_lock",
                            side_effect=lambda *a, **kw: nullcontext())
        lock.start()
        self.addCleanup(lock.stop)

    def test_legacy_settings_use_one_context_by_default(self):
        self.path.write_text('{"mode":"legacy","browser":"chrome","instance":1}',
                             encoding="utf-8")
        selected = prefs.load_browser_preferences()
        self.assertEqual(selected["webgpt2"], {"browser":"chrome","instance":1})
        self.assertEqual(prefs.browser_launch_selection(role="webgpt1")["port"],12801)
        self.assertEqual(prefs.browser_launch_selection(role="webgpt2")["port"],12801)

    def test_separate_browsers_ports_profiles_survive_primary_change(self):
        prefs.save_browser_preferences("chrome",1)
        prefs.save_browser_preferences("edge",3,role="webgpt2")
        saved = prefs.load_browser_preferences()
        self.assertEqual(saved["browser"],"chrome")
        self.assertEqual(saved["webgpt2"],{"browser":"edge","instance":3})
        second = prefs.browser_launch_selection(role="webgpt2")
        self.assertEqual(second["port"],12813)
        self.assertEqual(second["profile"],"edge_3")
        self.assertEqual(second["endpoint"],"http://127.0.0.1:12813")
        prefs.save_browser_preferences("chrome",5)
        self.assertEqual(prefs.load_browser_preferences()["webgpt2"],
                         {"browser":"edge","instance":3})
        self.assertEqual(prefs.browser_launch_selection(role="webgpt1")["port"],12805)

    def test_invalid_role_and_profile_fail_closed(self):
        for call in (lambda: prefs.save_browser_preferences("firefox",1),
                     lambda: prefs.save_browser_preferences("chrome",6),
                     lambda: prefs.save_browser_preferences("edge",1,role="bad"),
                     lambda: prefs.browser_launch_selection(role="bad")):
            with self.assertRaises(ValueError):
                call()
        self.path.write_text('{"browser":"chrome","instance":1,"webgpt2":"bad"}',
                             encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"webgpt2_settings_invalid"):
            prefs.load_browser_preferences()

    def test_runtime_reuses_primary_when_browser_selection_matches(self):
        prefs.save_browser_preferences("chrome",1)
        ctx=types.SimpleNamespace(pages=[])
        scraper=types.SimpleNamespace(_browser=ctx)
        self.assertIs(prepare_gar_browser_context(scraper),ctx)

    def test_dedicated_role_connects_second_cdp_and_retains_connection(self):
        prefs.save_browser_preferences("chrome",1)
        prefs.save_browser_preferences("chrome",2,role="webgpt2")
        ctx=types.SimpleNamespace(pages=[])
        second=types.SimpleNamespace(pages=[])
        browser=types.SimpleNamespace(contexts=[second])
        calls=[]
        def connect(endpoint,**kwargs):
            calls.append(endpoint)
            return browser
        playwright=types.SimpleNamespace(chromium=types.SimpleNamespace(connect_over_cdp=connect))
        scraper=types.SimpleNamespace(_browser=ctx,_pw=playwright,
                                       _active_cdp_endpoint="http://127.0.0.1:12801")
        with patch("agent_core.web_ui.manual_browser.ensure_agent_browser",
                   return_value=("http://127.0.0.1:12802",False)):
            self.assertIs(prepare_gar_browser_context(scraper),second)
            self.assertIs(prepare_gar_browser_context(scraper),second)
        self.assertEqual(calls,["http://127.0.0.1:12802"])
        self.assertEqual(scraper._gar_second_endpoint,"http://127.0.0.1:12802")
        self.assertIs(scraper._gar_second_browser_connection,browser)

    def test_dedicated_role_never_falls_back_if_second_connection_fails(self):
        prefs.save_browser_preferences("chrome",1)
        prefs.save_browser_preferences("edge",2,role="webgpt2")
        ctx=types.SimpleNamespace(pages=[])
        scraper=types.SimpleNamespace(_browser=ctx,_pw=types.SimpleNamespace(
            chromium=types.SimpleNamespace(connect_over_cdp=lambda *a,**kw:
                                             (_ for _ in ()).throw(RuntimeError("CDP_OFFLINE")))))
        with patch("agent_core.web_ui.manual_browser.ensure_agent_browser",
                   return_value=("http://127.0.0.1:12812",False)):
            with self.assertRaisesRegex(RuntimeError,"CDP_OFFLINE"):
                prepare_gar_browser_context(scraper)
        self.assertFalse(hasattr(scraper,"_gar_second_context"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
