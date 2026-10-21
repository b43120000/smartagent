"""Publish builders must never package per-user DOM or browser state."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

BUILDER = Path(__file__).resolve().parents[1] / 'build_update_manifest.py'
spec = importlib.util.spec_from_file_location('publish_builder', BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class PublishPrivacyTests(unittest.TestCase):
    def test_manual_dom_guide_is_deployable_but_account_settings_are_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            allowed=['adapterUI/dom.md','source/agent_core/web_ui/dom_config.py',
                     'source/agent_core/web_ui/providers/m365/dom_config.default.json',
                     '.venv/portable-git/manifest.json','.venv/portable-git/git.zip',
                     '.venv/portable-runtime/manifest.json','.venv/portable-runtime/runtime.zip.part000']
            private=['doc/portable_git_handoff.md','source/run_t16_c_diag.py',
                     'source/agent_core/web_ui/providers/gemini/dom_config - copy.json',
                     'config/debug_config.json','config/browser_session.json',
                     'source/agent_core/web_ui/providers/m365/dom_config.json',
                     'source/agent_core/web_ui/providers/other/other_fixture/dom_config.json',
                     'source/agent_core/web_ui/providers/m365/dom_config.history/old.json',
                     'localdata/logs/private.json','.agents/session.json',
                     '.venv/git/cmd/git.exe','.venv/portable-git/private.txt',
                     '.venv/Lib/site-packages/private.py','.venv/Scripts/python.exe']
            for relative in allowed+private:
                path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            self.assertEqual(set(builder.deployable_files(root)),set(allowed))

if __name__=='__main__': unittest.main()
