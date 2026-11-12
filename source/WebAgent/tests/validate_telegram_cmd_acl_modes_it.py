"""Windows regression: explicitly authenticated Telegram CMD is a host channel.

Human-approved CMD is independent of Agent ACL/Turbo state and may contain
delete operations. It is NEVER callable as an Agent action. All executions here
are mocked unless a specific disposable Windows test invokes a harmless echo.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from RemoteAgent.telegram_direct_cmd import (
    CmdControlPolicy, CmdControlError, execute_cmd_request,
)
from agent_core.windows_security import restricted_executor_required


@unittest.skipUnless(os.name == "nt", "Windows desktop CMD")
class TelegramCmdAclModesIT(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tgcmd-manual-host-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.policy = CmdControlPolicy(True, frozenset({11}), frozenset({22}))
        self.request = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\n"echo hello"\n"echo %TIME%"'
        )

    def _batch_result(self, count=2):
        return {
            "console_started": True, "console_exit_code": 0,
            "completed": True, "reason": "",
            "results": [
                {"line": i, "exit_code": 0, "stdout": "MANUAL_HOST",
                 "stderr": "", "status": "SUCCESS", "timed_out": False}
                for i in range(1, count + 1)
            ],
        }

    def test_fresh_clone_does_not_change_acl_on_its_own(self):
        profile = (self.root / "localdata" / "secure" /
                   "windows_security" / "security_profile.json")
        self.assertFalse(profile.exists())
        with patch.dict(os.environ, {
            "SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED": "0",
        }):
            self.assertFalse(restricted_executor_required(profile))
        self.assertFalse(profile.with_name("acl_mode.json").exists())

    def test_all_acl_and_turbo_modes_use_same_visible_host_console(self):
        # Patch the legacy safety gates to RAISE; these are Agent-only gates
        # and MUST NOT even be queried by the paired human CMD path.
        for acl in ("off", "on"):
            for turbo in ("off", "on"):
                with self.subTest(acl=acl, turbo=turbo), \
                     patch("agent_core.windows_security.restricted_executor_required",
                           side_effect=AssertionError("TG_MUST_NOT_USE_RESTRICTED_EXECUTOR")), \
                     patch("agent_core.workspace_access.access_snapshot",
                           side_effect=AssertionError("TG_MUST_NOT_REQUIRE_WORKSPACE_POLICY")), \
                     patch("agent_core.command_security.inspect_command",
                           side_effect=AssertionError("TG_MUST_NOT_USE_AGENT_COMMAND_GUARD")), \
                     patch("agent_core.turbo_sandbox.resolve_turbo_state",
                           side_effect=AssertionError("TG_MUST_NOT_REQUIRE_TURBO")), \
                     patch("RemoteAgent.telegram_console_cmd.run_visible_cmd_batch",
                           return_value=self._batch_result()) as visible:
                    result = execute_cmd_request(
                        self.request, user_id=11, chat_id=22, policy=self.policy,
                    )
                    self.assertEqual(result["status"], "SUCCESS", result)
                    self.assertIn("Telegram 手動 Host CMD", result["message"])
                    self.assertIn("Windows Token", result["message"])
                    visible.assert_called_once()
                    self.assertEqual(visible.call_args.args[0],
                                     ("echo hello", "echo %TIME%"))

    def test_del_rmdir_and_outside_workspace_are_explicit_human_commands(self):
        # This test does NOT execute destructive commands: it proves routing.
        parent = self.root.parent
        target = parent / "safe-fake-target"
        msg = (f'run cmd by TG\ncmd path: "{parent}"\n'
               'command:\n'
               f'"del /q {target}.txt"\n'
               f'"rmdir /s /q {target}"')
        with patch("agent_core.workspace_access.access_snapshot",
                   side_effect=AssertionError("NO_WORKSPACE_CEILING_FOR_HUMAN")), \
             patch("agent_core.command_security.inspect_command",
                   side_effect=AssertionError("NO_AGENT_COMMAND_FILTER")), \
             patch("RemoteAgent.telegram_console_cmd.run_visible_cmd_batch",
                   return_value=self._batch_result()) as visible:
            result = execute_cmd_request(
                msg, user_id=11, chat_id=22, policy=self.policy,
                trusted_install_root=self.root,
            )
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(visible.call_args.kwargs["cwd"], parent)
        self.assertTrue(visible.call_args.args[0][0].startswith("del /q "))
        self.assertTrue(visible.call_args.args[0][1].startswith("rmdir /s /q "))

    def test_missing_private_chat_authorization_is_never_a_shell_grant(self):
        with patch("RemoteAgent.telegram_console_cmd.run_visible_cmd_batch") as visible:
            with self.assertRaisesRegex(CmdControlError, "TG_CMD_RECEIVER_AUTH_REQUIRED"):
                execute_cmd_request(self.request, user_id=11, chat_id=22)
            with self.assertRaisesRegex(CmdControlError, "TG_CMD_DISABLED"):
                execute_cmd_request(
                    self.request, user_id=11, chat_id=22,
                    policy=CmdControlPolicy(False, frozenset({11}), frozenset({22})),
                )
            with self.assertRaisesRegex(CmdControlError, "TG_CMD_ADMIN_NOT_AUTHORIZED"):
                execute_cmd_request(
                    self.request, user_id=999, chat_id=22, policy=self.policy,
                )
            with self.assertRaisesRegex(CmdControlError, "TG_CMD_ADMIN_NOT_AUTHORIZED"):
                execute_cmd_request(
                    self.request, user_id=11, chat_id=999, policy=self.policy,
                )
            visible.assert_not_called()

    def test_legacy_injected_test_runner_does_not_become_agent_executor(self):
        seen = []

        def fake_runner(argv, **kwargs):
            seen.append((argv, kwargs))
            return {"process_started": True, "exit_code": 0,
                    "stdout": "ok", "stderr": "", "timed_out": False}

        with patch("agent_core.windows_security.restricted_executor_required",
                   side_effect=AssertionError("NO_RESTRICTED_SERVICE")), \
             patch("agent_core.workspace_access.access_snapshot",
                   side_effect=AssertionError("NO_WORKSPACE_ACCESS")):
            reply = execute_cmd_request(
                self.request, user_id=11, chat_id=22,
                policy=self.policy, runner=fake_runner,
            )
        self.assertEqual(reply["status"], "SUCCESS")
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(x[0][-2] == "/c" for x in seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
