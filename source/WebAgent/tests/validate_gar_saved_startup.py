"""Shared GAR WebGPT2 target: offline persistence, migration and cross-agent regression."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import workspace_manager
from agent_core.gar_webgpt2_settings import SCHEMA, ENV_KEY, GAR_FLAG, apply_target, load_target, save_target
from agent_core.startup_preferences import save_startup_preferences

PRIMARY = "https://chatgpt.com/c/primary"
FRESH = "https://chatgpt.com/g/g-p-team/c/fresh"


class SharedGARTests(unittest.TestCase):
    def test_global_store_and_explicit_clear(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            self.assertEqual(save_target(FRESH,path=p),FRESH)
            self.assertEqual(load_target(path=p),FRESH)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["schema"],SCHEMA)
            save_target("",path=p)
            self.assertEqual(load_target(path=p),"")

    def test_legacy_local_setting_migrates(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            legacy=Path(d)/"startup_preferences.json"
            legacy.write_text(json.dumps({"version":1,"selected":{
                "workspace":d,"gpt_url":PRIMARY,"gar_webgpt2_url":FRESH}}),encoding="utf-8")
            self.assertEqual(load_target(path=p,legacy_path=legacy),FRESH)
            saved=save_startup_preferences(workspace=d,gpt_url=FRESH,
                 planner_key="web_chatgpt",executor_key="cloud_gptoss",path=legacy)
            self.assertEqual(saved["gar_webgpt2_url"],FRESH)
            save_target("",path=p)
            self.assertEqual(load_target(path=p,legacy_path=legacy),"")

    def test_shared_target_three_independent_primary_conversations(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            save_target(FRESH,path=p)
            for primary in (PRIMARY,"https://chatgpt.com/c/remote","https://chatgpt.com/c/local"):
                env={}
                self.assertEqual(apply_target(primary,environ=env,path=p),FRESH)
                self.assertEqual(env[ENV_KEY],FRESH)
                self.assertEqual(env[GAR_FLAG],"1")
            with self.assertRaisesRegex(ValueError,"must_differ"):
                apply_target(FRESH,environ={},path=p)

    def test_explicit_env_override_and_blank_disable(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            save_target(FRESH,path=p)
            manual="https://chatgpt.com/c/manual"
            explicit={ENV_KEY:manual,GAR_FLAG:"0"}
            self.assertEqual(apply_target(PRIMARY,environ=explicit,path=p),manual)
            self.assertEqual(explicit[GAR_FLAG],"1")
            disabled={ENV_KEY:"",GAR_FLAG:"1"}
            self.assertEqual(apply_target(PRIMARY,environ=disabled,path=p),"")
            self.assertEqual(disabled[GAR_FLAG],"0")
            with self.assertRaisesRegex(ValueError,"must_differ"):
                apply_target(PRIMARY,environ={ENV_KEY:PRIMARY},path=p)

    def test_no_saved_webgpt2_disables_gar_at_startup(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            legacy=Path(d)/"legacy.json"
            for raw in (None,""):
                if raw is not None:
                    save_target(raw,path=p)
                env={GAR_FLAG:"1"}
                self.assertEqual(apply_target(PRIMARY,environ=env,path=p,legacy_path=legacy),"")
                self.assertEqual(env[GAR_FLAG],"0")
                self.assertNotIn(ENV_KEY,env)

    def test_clear_global_setting_turns_off_gar(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            env={}
            save_target(FRESH,path=p)
            self.assertEqual(apply_target(PRIMARY,environ=env,path=p),FRESH)
            self.assertEqual(env[GAR_FLAG],"1")
            save_target("",path=p)
            # Each agent startup is a fresh process; it does not inherit a
            # previously calculated URL from an earlier launched agent.
            next_start_env={GAR_FLAG:"1"}
            self.assertEqual(apply_target(PRIMARY,environ=next_start_env,path=p),"")
            self.assertEqual(next_start_env[GAR_FLAG],"0")

    def test_invalid_target_does_not_enable_gar(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            save_target(FRESH,path=p)
            env={ENV_KEY:"https://chatgpt.com/",GAR_FLAG:"0"}
            with self.assertRaises(ValueError):
                apply_target(PRIMARY,environ=env,path=p)
            self.assertEqual(env[GAR_FLAG],"0")

    def test_fail_closed_on_invalid_url_or_schema(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gar.json"
            save_target(FRESH,path=p)
            for value in ("https://chatgpt.com/","https://claude.ai/chat/x","https://example.com/c/x","bad"):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    save_target(value,path=p)
                self.assertEqual(load_target(path=p),FRESH)
            p.write_text('{"schema":"invalid","url":"x"}',encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"schema_invalid"):
                load_target(path=p)

    def test_independent_menu_and_cli(self):
        with patch.object(workspace_manager,"load_gar_webgpt2",return_value=FRESH),\
             patch.object(workspace_manager,"save_gar_webgpt2",return_value="") as save,\
             patch("builtins.input",return_value="-"):
            self.assertEqual(workspace_manager._gar_webgpt2_menu(),0)
            save.assert_called_once_with("")
        with patch.object(workspace_manager,"_install_apply_crash_logger"),\
             patch.object(workspace_manager,"_write_apply_log"),\
             patch.object(workspace_manager,"save_gar_webgpt2",return_value=FRESH) as save:
            self.assertEqual(workspace_manager.main(["--set-gar-webgpt2-url",FRESH]),0)
            save.assert_called_once_with(FRESH)

    def test_original_local_and_remote_binding_menu_unchanged(self):
        snapshot={"security_status":"CONFIGURED","writable_workspaces":["C:/ws"],
                  "local":{"gpt_url":PRIMARY},"remote":{"gpt_url":PRIMARY}}
        for raw in ("1","2"):
            with patch.object(workspace_manager,"_binding_snapshot",return_value=snapshot),\
                 patch.object(workspace_manager,"_choose_workspace",return_value="C:/ws"),\
                 patch("builtins.input",side_effect=[raw,PRIMARY]),\
                 patch.object(workspace_manager,"_save_binding",return_value={"status":"APPLIED"}) as save:
                self.assertEqual(workspace_manager._binding_menu(),0)
                self.assertEqual(save.call_args.args,(("local" if raw=="1" else "remote"),"C:/ws",PRIMARY))

    def test_source_wires_shared_url_for_three_interfaces(self):
        root=Path(__file__).resolve().parents[3]/"source"
        self.assertIn("apply_target(args.webgpt_url)",(root/"WebAgent/controller.py").read_text(encoding="utf-8"))
        remote=(root/"RemoteAgent/telegram_webagent_worker.py").read_text(encoding="utf-8")
        self.assertIn("gar_fresh_url = apply_target(execution_url)",remote)
        self.assertIn("page_lease_held=True",remote)
        self.assertIn("apply_gar_target(selected_gpt_url)",(root/"smart_agent.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
