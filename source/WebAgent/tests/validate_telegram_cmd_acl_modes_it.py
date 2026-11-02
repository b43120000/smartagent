"""Windows regression: Telegram CMD follows ACL mode; fresh install defaults OFF.

Run with PYTHONPATH=source. No real network access, Telegram Bot or ACL changes.
Only harmless echo commands are launched in a temporary workspace.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from RemoteAgent.telegram_direct_cmd import (
    CmdControlPolicy, CmdControlError, execute_cmd_request,
)
from agent_core.windows_security import restricted_executor_required


class TelegramCmdAclModesIT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tgcmd-acl-modes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.policy = CmdControlPolicy(True, frozenset({11}), frozenset({22}))
        self.request = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\n"echo hello"\n"echo %TIME%"'
        )

    def test_fresh_clone_without_acl_profile_is_software_guard_default(self):
        missing_profile = (
            self.root / "localdata" / "secure" / "windows_security"
            / "security_profile.json"
        )
        self.assertFalse(missing_profile.exists())
        # Existing bootstrap provisions explicit mode OFF. Without any local
        # state after git clone, no ACL profile or activation is inherited.
        with patch.dict(os.environ, {
            "SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED": "0",
        }):
            self.assertFalse(restricted_executor_required(missing_profile))
        self.assertFalse(missing_profile.with_name("acl_mode.json").exists())

    def test_acl_off_real_cmd_runs_through_existing_bounded_process(self):
        from agent_core.bounded_process import run_bounded_process
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}), \
             patch("RemoteAgent.telegram_direct_cmd.os.name", "nt"):
            response = execute_cmd_request(
                self.request, user_id=11, chat_id=22,
                policy=self.policy, runner=run_bounded_process,
            )
        self.assertEqual(response["status"], "SUCCESS", response)
        self.assertEqual([row["exit_code"] for row in response["results"]], [0, 0])
        self.assertIn("ACL OFF / Software Guard", response["message"])
        self.assertIn("hello", response["message"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_acl_on_keeps_restricted_executor_routing(self):
        from agent_core.bounded_process import run_bounded_process
        invocations = []
        def restricted(command, **kwargs):
            invocations.append((command, kwargs))
            return {"process_started": True, "exit_code": 0,
                    "stdout": "restricted-ok", "stderr": "",
                    "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}), \
             patch("agent_core.restricted_executor_client.execute_restricted",
                   side_effect=restricted), \
             patch("RemoteAgent.telegram_direct_cmd.os.name", "nt"):
            response = execute_cmd_request(
                self.request, user_id=11, chat_id=22,
                policy=self.policy, runner=run_bounded_process,
            )
        self.assertEqual(response["status"], "SUCCESS", response)
        self.assertEqual(len(invocations), 2)
        self.assertIn("ACL ON / Restricted Executor", response["message"])

    def test_acl_off_still_rejects_untrusted_shell_command(self):
        bad_request = self.request.replace('"echo hello"', '"diskpart"')
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}), \
             patch("RemoteAgent.telegram_direct_cmd.os.name", "nt"):
            with self.assertRaisesRegex(CmdControlError, "COMMAND_REJECTED"):
                execute_cmd_request(
                    bad_request, user_id=11, chat_id=22,
                    policy=self.policy,
                    runner=lambda *args, **kwargs: self.fail("No process should start"),
                )

    def test_acl_off_does_not_bypass_telegram_admin_allowlist(self):
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("RemoteAgent.telegram_direct_cmd.os.name", "nt"):
            with self.assertRaisesRegex(CmdControlError, "TG_CMD_ADMIN_NOT_AUTHORIZED"):
                execute_cmd_request(
                    self.request, user_id=999, chat_id=22,
                    policy=self.policy,
                    runner=lambda *args, **kwargs: self.fail("No process should start"),
                )

    def test_acl_off_requires_preexisting_authorized_workspace(self):
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": ()}), \
             patch("RemoteAgent.telegram_direct_cmd.os.name", "nt"):
            with self.assertRaisesRegex(CmdControlError, "WORKSPACE_NOT_AUTHORIZED"):
                execute_cmd_request(
                    self.request, user_id=11, chat_id=22,
                    policy=self.policy,
                    runner=lambda *args, **kwargs: self.fail("No process should start"),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
