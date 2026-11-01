"""Offline, no-network regression for central feature configuration.

PYTHONPATH=source python -B source/WebAgent/tests/validate_runtime_feature_registry.py
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import runtime_config as cfg


class RegistryIT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="feature-registry-")
        self.addCleanup(self.tmp.cleanup)
        self.override = Path(self.tmp.name) / "feature_flags.json"

    def resolve(self, agent, env=None):
        return cfg.resolve_features(agent, user_file=self.override, environ=env or {})

    def test_registry_has_explicit_agent_capabilities_and_derived_gar(self):
        registry = cfg.load_registry()
        self.assertEqual(registry["schema"], cfg.SCHEMA)
        self.assertEqual(registry["derived"]["gar_v1"]["env"], "SMARTAGENT_GAR_V1")
        self.assertNotIn("gar_v1", registry["features"])
        for name in ("tri_one_task_result", "tri_one_text_only",
                     "tri_one_shared_worker", "composer_hybrid",
                     "gar_l4_supervisor"):
            self.assertIn(name, registry["features"])

    def test_defaults_match_across_remote_and_webdirect(self):
        remote = self.resolve("remote_agent")
        direct = self.resolve("web_direct")
        for name in ("tri_one_task_result", "tri_one_text_only",
                     "composer_hybrid", "gar_l4_supervisor"):
            self.assertTrue(remote["flags"][name], name)
            self.assertTrue(direct["flags"][name], name)
        self.assertTrue(remote["flags"]["tri_one_shared_worker"])
        self.assertFalse(direct["flags"]["tri_one_shared_worker"])
        self.assertTrue(direct["flags"]["tri_one_text_only_web_direct"])
        for name in ("tri_one_task_result", "tri_one_text_only",
                     "tri_one_shared_worker", "gar_l4_supervisor"):
            self.assertFalse(self.resolve("local_agent")["flags"][name])

    def test_persisted_global_and_agent_override(self):
        self.override.write_text(json.dumps({
            "schema": cfg.SCHEMA,
            "features": {"tri_one_text_only": False},
            "agent_overrides": {"web_direct": {"tri_one_text_only": True}},
        }), encoding="utf-8")
        remote = self.resolve("remote_agent")
        direct = self.resolve("web_direct")
        self.assertFalse(remote["flags"]["tri_one_text_only"])
        self.assertEqual(remote["sources"]["tri_one_text_only"], "user_config")
        self.assertFalse(remote["flags"]["tri_one_text_only_telegram"])
        self.assertTrue(direct["flags"]["tri_one_text_only"])
        self.assertEqual(direct["sources"]["tri_one_text_only"], "agent_override:web_direct")

    def test_explicit_environment_zero_one_takes_precedence(self):
        self.override.write_text(json.dumps({
            "schema": cfg.SCHEMA,
            "features": {"composer_hybrid": True, "tri_one_task_result": False},
        }), encoding="utf-8")
        case = self.resolve("remote_agent", {
            "SMARTAGENT_COMPOSER_HYBRID_V1": "0",
            "TRI_ONE_TASK_RESULT_V1": "1",
        })
        self.assertFalse(case["flags"]["composer_hybrid"])
        self.assertTrue(case["flags"]["tri_one_task_result"])
        self.assertEqual(case["sources"]["composer_hybrid"], "process_environment")

    def test_task_result_is_required_for_5a(self):
        case = self.resolve("remote_agent", {
            "TRI_ONE_TASK_RESULT_V1": "0",
            "TRI_ONE_TEXT_ONLY_V1": "1",
            "TRI_ONE_TEXT_ONLY_TELEGRAM": "1",
        })
        self.assertFalse(case["flags"]["tri_one_task_result"])
        self.assertFalse(case["flags"]["tri_one_text_only"])
        self.assertFalse(case["flags"]["tri_one_text_only_telegram"])
        self.assertTrue(case["sources"]["tri_one_text_only"].startswith("dependency_disabled:"))

    def test_unsupported_local_agent_cannot_force_5a(self):
        forced = self.resolve("local_agent", {
            "TRI_ONE_TASK_RESULT_V1": "1",
            "TRI_ONE_TEXT_ONLY_V1": "1",
            "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "1",
        })
        for name in ("tri_one_task_result", "tri_one_text_only", "gar_l4_supervisor"):
            self.assertFalse(forced["flags"][name])
            self.assertEqual(forced["sources"][name], "unsupported_agent:local_agent")

    def test_worker_inherits_same_effective_values_and_sources(self):
        parent_env = {}
        parent = cfg.apply_process_config(
            "remote_agent", environ=parent_env,
            user_file=self.override, write_log=False)
        child_env = dict(parent_env)
        child = cfg.apply_process_config(
            "remote_agent", environ=child_env,
            user_file=self.override, write_log=False)
        self.assertEqual(parent["effective_sha256"], child["effective_sha256"])
        self.assertEqual(parent["flags"], child["flags"])
        self.assertEqual(parent["sources"], child["sources"])
        self.assertEqual(child_env["TRI_ONE_TEXT_ONLY_V1"], "1")
        self.assertEqual(child_env["TRI_ONE_TASK_RESULT_V1"], "1")
        self.assertEqual(child_env["SMARTAGENT_COMPOSER_HYBRID_V1"], "1")
        self.assertNotIn("SMARTAGENT_GAR_V1", child_env)

    def test_local_supervisor_to_remote_worker_recomputes_capability_gates(self):
        env = {}
        local = cfg.apply_process_config("local_agent", environ=env,
                                         user_file=self.override, write_log=False)
        self.assertFalse(local["flags"]["tri_one_text_only"])
        remote = cfg.apply_process_config("remote_agent", environ=env,
                                          user_file=self.override, write_log=False)
        self.assertTrue(remote["flags"]["tri_one_text_only"])
        self.assertTrue(remote["flags"]["tri_one_task_result"])
        self.assertEqual(remote["sources"]["tri_one_text_only"], "tracked_default")
        self.assertEqual(env["TRI_ONE_TEXT_ONLY_V1"], "1")

    def test_genuine_explicit_override_survives_agent_role_switch(self):
        env = {"TRI_ONE_TASK_RESULT_V1": "0"}
        cfg.apply_process_config("local_agent", environ=env,
                                 user_file=self.override, write_log=False)
        remote = cfg.apply_process_config("remote_agent", environ=env,
                                          user_file=self.override, write_log=False)
        self.assertFalse(remote["flags"]["tri_one_task_result"])
        self.assertFalse(remote["flags"]["tri_one_text_only"])
        self.assertEqual(remote["sources"]["tri_one_task_result"],
                         "process_environment")
        env = {"TRI_ONE_TEXT_ONLY_V1": "0"}
        cfg.apply_process_config("local_agent", environ=env,
                                 user_file=self.override, write_log=False)
        remote = cfg.apply_process_config("remote_agent", environ=env,
                                          user_file=self.override, write_log=False)
        self.assertFalse(remote["flags"]["tri_one_text_only"])
        self.assertEqual(remote["sources"]["tri_one_text_only"],
                         "process_environment")

    def test_inherited_configuration_version_change_requires_restart(self):
        env = {}
        cfg.apply_process_config("remote_agent", environ=env,
                                 user_file=self.override, write_log=False)
        self.override.write_text(json.dumps({
            "schema": cfg.SCHEMA,
            "features": {"composer_hybrid": False},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config_changed_since_parent"):
            cfg.apply_process_config("remote_agent", environ=env,
                                     user_file=self.override, write_log=False)

    def test_env_change_after_parent_snapshot_is_explicit(self):
        parent_env = {}
        cfg.apply_process_config("remote_agent", environ=parent_env,
                                 user_file=self.override, write_log=False)
        parent_env["TRI_ONE_TEXT_ONLY_V1"] = "0"
        child = cfg.apply_process_config("remote_agent", environ=parent_env,
                                         user_file=self.override, write_log=False)
        self.assertFalse(child["flags"]["tri_one_text_only"])
        self.assertEqual(child["sources"]["tri_one_text_only"], "process_environment")

    def test_invalid_config_schema_and_values_fail_closed(self):
        for payload in (
            {"schema": "BAD"},
            {"schema": cfg.SCHEMA, "features": {"unknown": True}},
            {"schema": cfg.SCHEMA, "agent_overrides": {"invalid_agent": {"composer_hybrid": True}}},
            {"schema": cfg.SCHEMA, "features": {"composer_hybrid": "yes"}},
        ):
            with self.subTest(payload=payload):
                self.override.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.resolve("remote_agent")
        self.override.unlink()
        with self.assertRaisesRegex(ValueError, "invalid_feature_boolean"):
            self.resolve("remote_agent", {"TRI_ONE_TEXT_ONLY_V1": "perhaps"})

    def test_user_setting_uses_local_config_file_only(self):
        cfg.save_feature("composer_hybrid", False, path=self.override)
        saved = json.loads(self.override.read_text(encoding="utf-8"))
        self.assertFalse(saved["features"]["composer_hybrid"])
        cfg.save_feature("composer_hybrid", True, agent="web_direct",
                         path=self.override)
        saved = json.loads(self.override.read_text(encoding="utf-8"))
        self.assertTrue(saved["agent_overrides"]["web_direct"]["composer_hybrid"])

    def test_source_wiring_and_webdirect_no_hardcoded_flags(self):
        root = Path(__file__).resolve().parents[3]
        wiring = {
            "source/WebAgent/controller.py": 'apply_process_config("web_direct")',
            "source/RemoteAgent/telegram_webagent_worker.py":
                'apply_process_config("remote_agent")',
            "source/RemoteAgent/remote_worker_launcher.py":
                'apply_process_config(',
            "source/smart_agent.py": 'apply_process_config("remote_agent" if remote_worker_mode else "local_agent")',
            "source/agent_core/host_supervisor.py": 'apply_process_config("remote_agent" if args.remote_only or args.dispatcher_only',
        }
        for filename, needle in wiring.items():
            with self.subTest(filename=filename):
                self.assertIn(needle, (root / filename).read_text(encoding="utf-8"))
        bat = (root / "launch_webcopilot.bat").read_text(encoding="utf-8")
        self.assertNotIn('set "TRI_ONE_TASK_RESULT_V1=1"', bat)
        self.assertNotIn('set "TRI_ONE_TEXT_ONLY_V1=1"', bat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
