"""Offline GAR WebGPT1/WebGPT2 saved startup and workspace-manager regression."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.startup_preferences import (
    load_startup_preferences, save_startup_preferences, validate_gar_webgpt2_url,
)
from agent_core import workspace_manager
from WebAgent.controller import apply_saved_gar_webgpt2_url

WEBGPT1 = "https://chatgpt.com/c/primary-1"
WEBGPT1_PROJECT = "https://chatgpt.com/g/g-p-team/c/primary-1"
WEBGPT2 = "https://chatgpt.com/g/g-p-team/c/fresh-2"


class SavedGARBindingTests(unittest.TestCase):
    def test_legacy_v1_migration_keep_replace_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup_preferences.json"
            path.write_text(json.dumps({
                "version": 1, "selected": {
                    "workspace": directory, "gpt_url": WEBGPT1,
                    "planner_key": "web_chatgpt", "executor_key": "cloud_gptoss",
                }
            }), encoding="utf-8")
            old = load_startup_preferences(path)
            self.assertNotIn("gar_webgpt2_url", old)
            options = dict(workspace=directory, gpt_url=WEBGPT1,
                           planner_key="web_chatgpt", executor_key="cloud_gptoss",
                           path=path)
            saved = save_startup_preferences(**options, gar_webgpt2_url=WEBGPT2)
            self.assertEqual(saved["gar_webgpt2_url"], WEBGPT2)
            self.assertEqual(load_startup_preferences(path)["gar_webgpt2_url"], WEBGPT2)
            self.assertEqual(save_startup_preferences(**options)["gar_webgpt2_url"], WEBGPT2)
            # Rebind WebGPT1 to another distinct conversation without losing WebGPT2.
            other = save_startup_preferences(**{**options,
                "gpt_url": "https://chatgpt.com/c/primary-3"})
            self.assertEqual(other["gar_webgpt2_url"], WEBGPT2)
            empty = save_startup_preferences(**options, gar_webgpt2_url="")
            self.assertEqual(empty["gar_webgpt2_url"], "")
            self.assertEqual(load_startup_preferences(path)["gar_webgpt2_url"], "")

    def test_reject_same_conversation_wrong_provider_or_missing_id(self):
        for candidate in (
            WEBGPT1, WEBGPT1_PROJECT, "https://chatgpt.com/",
            "https://example.com/c/other", "https://claude.ai/chat/other",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    validate_gar_webgpt2_url(WEBGPT1, candidate)
        self.assertEqual(validate_gar_webgpt2_url(WEBGPT1, ""), "")
        self.assertEqual(validate_gar_webgpt2_url(WEBGPT1, WEBGPT2), WEBGPT2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pref.json"
            with self.assertRaises(ValueError):
                save_startup_preferences(
                    workspace=directory, gpt_url=WEBGPT1,
                    planner_key="web_chatgpt", executor_key="cloud_gptoss",
                    gar_webgpt2_url=WEBGPT1_PROJECT, path=path)
            self.assertFalse(path.exists())

    def test_load_process_override_and_saved_url_validation(self):
        saved = {"gpt_url": WEBGPT1, "gar_webgpt2_url": WEBGPT2}
        env = {}
        self.assertEqual(apply_saved_gar_webgpt2_url(saved, WEBGPT1, environ=env), WEBGPT2)
        self.assertEqual(env["SMARTAGENT_GAR_FRESH_CONTEXT_URL"], WEBGPT2)
        self.assertNotIn("SMARTAGENT_GAR_V1", env)
        explicit = {"SMARTAGENT_GAR_FRESH_CONTEXT_URL": "https://chatgpt.com/c/manual"}
        self.assertEqual(apply_saved_gar_webgpt2_url(saved, WEBGPT1, environ=explicit),
                         explicit["SMARTAGENT_GAR_FRESH_CONTEXT_URL"])
        off = {"SMARTAGENT_GAR_FRESH_CONTEXT_URL": ""}
        self.assertEqual(apply_saved_gar_webgpt2_url(saved, WEBGPT1, environ=off), "")
        self.assertEqual(off["SMARTAGENT_GAR_FRESH_CONTEXT_URL"], "")
        with self.assertRaisesRegex(ValueError, "gar_webgpt2_must_differ"):
            apply_saved_gar_webgpt2_url(saved, WEBGPT2, environ={})
        legacy = {}
        self.assertEqual(apply_saved_gar_webgpt2_url({"gpt_url": WEBGPT1}, WEBGPT1, environ=legacy), "")
        self.assertNotIn("SMARTAGENT_GAR_FRESH_CONTEXT_URL", legacy)

    def test_local_menu_keep_clear_and_remote_unchanged(self):
        state = {
            "security_status": "CONFIGURED", "writable_workspaces": ["C:/workspace"],
            "local": {"gpt_url": WEBGPT1, "gar_webgpt2_url": WEBGPT2},
            "remote": {"gpt_url": WEBGPT1},
        }
        for gar_input, expected in (("", None), ("-", ""), (WEBGPT2, WEBGPT2)):
            with self.subTest(raw=gar_input), patch.object(
                workspace_manager, "_binding_snapshot", return_value=state
            ), patch.object(workspace_manager, "_choose_workspace",
                            return_value="C:/workspace"), patch(
                "builtins.input", side_effect=["1", WEBGPT1, gar_input]
            ), patch.object(workspace_manager, "_save_binding",
                            return_value={"status": "APPLIED"}) as write:
                self.assertEqual(workspace_manager._binding_menu(), 0)
                self.assertEqual(write.call_args.kwargs["gar_webgpt2_url"], expected)
        with patch.object(workspace_manager, "_binding_snapshot", return_value=state), \
             patch.object(workspace_manager, "_choose_workspace", return_value="C:/workspace"), \
             patch("builtins.input", side_effect=["2", WEBGPT1]), \
             patch.object(workspace_manager, "_save_binding",
                          return_value={"status": "APPLIED"}) as write:
            self.assertEqual(workspace_manager._binding_menu(), 0)
            self.assertNotIn("gar_webgpt2_url", write.call_args.kwargs)

    def test_local_cli_supports_explicit_clear(self):
        with patch.object(workspace_manager, "_install_apply_crash_logger"), \
             patch.object(workspace_manager, "_write_apply_log"), \
             patch.object(workspace_manager, "_save_binding",
                          return_value={"status": "APPLIED"}) as write:
            self.assertEqual(workspace_manager.main([
                "--set-binding", "local", "--workspace", "C:/workspace",
                "--url", WEBGPT1, "--gar-webgpt2-url", "",
            ]), 0)
            self.assertEqual(write.call_args.kwargs["gar_webgpt2_url"], "")
            self.assertEqual(workspace_manager.main([
                "--set-binding", "local", "--workspace", "C:/workspace",
                "--url", WEBGPT1,
            ]), 0)
            self.assertIsNone(write.call_args.kwargs["gar_webgpt2_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
