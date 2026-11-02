"""Offline IT: persistent Telegram CMD settings are local, safe and restartable."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from RemoteAgent.telegram_cmd_settings import (
    SCHEMA, TelegramCmdSettingsError, settings_path,
    apply_saved_telegram_cmd_environment, read_saved_telegram_cmd_settings,
)
from RemoteAgent.telegram_direct_cmd import CmdControlPolicy, CmdControlError


class TelegramCmdSettingsIT(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="tgcmd-local-config-")
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.file = settings_path(self.root)
        self.assertEqual(
            self.file.relative_to(self.root),
            Path("localdata") / "secure" / "telegram_cmd_settings.json",
        )

    def write(self, *, enabled=True, users=None, chats=None, extra=None):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "enabled": enabled,
            "allowed_user_ids": [11] if users is None else users,
            "allowed_chat_ids": [22] if chats is None else chats,
        }
        if extra:
            payload.update(extra)
        self.file.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_saved_configuration_is_loaded_when_launching_a_fresh_supervisor(self):
        self.write(users=[11, 33], chats=[22, 44])
        # inherited CMD-only env settings are overridden by explicit local
        # policy. Other Telegram/Agent settings must not be modified.
        before = {
            "SMARTAGENT_TG_CMD_ENABLED": "0",
            "SMARTAGENT_TG_CMD_ALLOWED_USER_IDS": "999",
            "SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS": "999",
            "SMARTAGENT_TELEGRAM_BOT_TOKEN": "unchanged",
        }
        result = apply_saved_telegram_cmd_environment(
            root=self.root, environ=before,
        )
        self.assertEqual(result, {
            "loaded": True, "enabled": True, "reason": "configured",
        })
        self.assertEqual(before["SMARTAGENT_TG_CMD_ENABLED"], "1")
        self.assertEqual(before["SMARTAGENT_TG_CMD_ALLOWED_USER_IDS"], "11,33")
        self.assertEqual(before["SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS"], "22,44")
        self.assertEqual(before["SMARTAGENT_TELEGRAM_BOT_TOKEN"], "unchanged")
        policy = CmdControlPolicy.from_env(before)
        policy.assert_authorized(11, 22)
        policy.assert_authorized(33, 44)
        with self.assertRaises(CmdControlError):
            policy.assert_authorized(99, 22)

        # A second launcher generation starts with a clean environment but
        # reads the same saved file without requiring a PowerShell assignment.
        after_restart = {}
        again = apply_saved_telegram_cmd_environment(
            root=self.root, environ=after_restart,
        )
        self.assertTrue(again["enabled"])
        CmdControlPolicy.from_env(after_restart).assert_authorized(11, 22)

    def test_disabled_file_overrides_inherited_enabled_variables(self):
        self.write(enabled=False, users=[11], chats=[22])
        env = {
            "SMARTAGENT_TG_CMD_ENABLED": "1",
            "SMARTAGENT_TG_CMD_ALLOWED_USER_IDS": "11",
            "SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS": "22",
        }
        result = apply_saved_telegram_cmd_environment(root=self.root, environ=env)
        self.assertTrue(result["loaded"])
        self.assertFalse(result["enabled"])
        self.assertEqual(env["SMARTAGENT_TG_CMD_ENABLED"], "0")
        with self.assertRaisesRegex(CmdControlError, "TG_CMD_DISABLED"):
            CmdControlPolicy.from_env(env).assert_authorized(11, 22)

    def test_missing_file_keeps_explicit_legacy_env(self):
        env = {
            "SMARTAGENT_TG_CMD_ENABLED": "1",
            "SMARTAGENT_TG_CMD_ALLOWED_USER_IDS": "11",
            "SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS": "22",
        }
        result = apply_saved_telegram_cmd_environment(root=self.root, environ=env)
        self.assertEqual(result["reason"], "not_configured")
        self.assertEqual(env["SMARTAGENT_TG_CMD_ENABLED"], "1")
        self.assertFalse(self.file.exists())

    def test_corrupt_file_rejects_instead_of_trusting_old_env(self):
        env = {
            "SMARTAGENT_TG_CMD_ENABLED": "1",
            "SMARTAGENT_TG_CMD_ALLOWED_USER_IDS": "11",
            "SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS": "22",
        }
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text('{"enabled": true}', encoding="utf-8")
        result = apply_saved_telegram_cmd_environment(root=self.root, environ=env)
        self.assertFalse(result["enabled"])
        self.assertTrue(result["reason"].startswith("invalid_local_config:"))
        self.assertEqual(env["SMARTAGENT_TG_CMD_ENABLED"], "0")
        self.assertNotIn("SMARTAGENT_TG_CMD_ALLOWED_USER_IDS", env)
        self.assertNotIn("SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS", env)

    def test_fail_closed_validation_of_unsafe_ids_and_unknown_fields(self):
        invalid = [
            {"users": ["11"]},
            {"users": [True]},
            {"users": [0]},
            {"users": [-1]},
            {"users": [11, 11]},
            {"users": []},
            {"chats": []},
            {"chats": [22, 22]},
            {"chats": ["22"]},
            {"extra": {"arbitrary_command": "echo unsafe"}},
            {"enabled": "true"},
        ]
        for variant in invalid:
            with self.subTest(variant=variant):
                self.write(**variant)
                with self.assertRaises(TelegramCmdSettingsError):
                    read_saved_telegram_cmd_settings(self.root)

    def test_disabled_example_without_ids_is_allowed_and_never_enables(self):
        self.write(enabled=False, users=[], chats=[])
        data = read_saved_telegram_cmd_settings(self.root)
        self.assertEqual(data, {
            "enabled": False, "user_ids": (), "chat_ids": (),
        })
        env = {}
        apply_saved_telegram_cmd_environment(root=self.root, environ=env)
        self.assertEqual(env["SMARTAGENT_TG_CMD_ENABLED"], "0")

    def test_file_is_only_read_from_installation_localdata(self):
        other = self.root / "elsewhere"
        other.mkdir()
        sample = other / "telegram_cmd_settings.json"
        sample.write_text(json.dumps({
            "schema": SCHEMA, "enabled": True,
            "allowed_user_ids": [11], "allowed_chat_ids": [22],
        }), encoding="utf-8")
        self.assertIsNone(read_saved_telegram_cmd_settings(self.root))
        self.assertTrue(sample.exists())

    def test_existing_symlink_is_never_treated_as_admin_config(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "somewhere.json"
        target.write_text(json.dumps({
            "schema": SCHEMA, "enabled": True,
            "allowed_user_ids": [11], "allowed_chat_ids": [22],
        }), encoding="utf-8")
        try:
            self.file.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported by this Windows CI user")
        with self.assertRaises(TelegramCmdSettingsError):
            read_saved_telegram_cmd_settings(self.root)

    def test_large_settings_file_is_rejected(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(" " * 4100, encoding="utf-8")
        with self.assertRaises(TelegramCmdSettingsError):
            read_saved_telegram_cmd_settings(self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
