"""Real Windows console integration: Telegram direct CMD keeps one shell session.

CI can run this without interactive desktop by disabling only the visibility
precondition. Production still requires a nonzero interactive user session.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from RemoteAgent.telegram_console_cmd import (
    TelegramConsoleError, _batch_path, _console_environment,
    run_visible_cmd_batch,
)
from RemoteAgent.telegram_direct_cmd import (
    CmdControlError, CmdControlPolicy, execute_cmd_request,
)


@unittest.skipUnless(os.name == "nt", "Windows console required")
class TelegramVisibleConsoleIT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tg-real-console-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.policy = CmdControlPolicy(True, frozenset({11}), frozenset({22}))

    def msg(self, *commands):
        return (f'run cmd by TG\ncmd path: "{self.root}"\ncommand:\n'
                + "\n".join(commands))

    def test_real_console_executes_sequentially_and_preserves_set_state(self):
        rows = run_visible_cmd_batch(
            ["echo FIRST", "set TG_PERSIST=VALUE_SENTINEL",
             "echo %TG_PERSIST%"],
            cwd=self.root, command_timeout_seconds=12,
            batch_timeout_seconds=40, require_interactive=False,
        )
        self.assertTrue(rows["console_started"], rows)
        self.assertTrue(rows["completed"], rows)
        self.assertEqual([r["exit_code"] for r in rows["results"]], [0, 0, 0])
        self.assertIn("FIRST", rows["results"][0]["stdout"])
        self.assertIn("VALUE_SENTINEL", rows["results"][2]["stdout"])

    def test_nonzero_exit_is_terminal_and_does_not_run_following_command(self):
        rows = run_visible_cmd_batch(
            ["echo BEFORE", "exit /b 7", "echo NEVER_RUN"],
            cwd=self.root, command_timeout_seconds=12,
            batch_timeout_seconds=40, require_interactive=False,
        )
        self.assertFalse(rows["completed"], rows)
        self.assertEqual(len(rows["results"]), 2, rows)
        self.assertEqual(rows["results"][1]["exit_code"], 7)

    def test_independent_pause_skipped_without_user_input(self):
        rows = run_visible_cmd_batch(
            ["pause", "echo AFTER_PAUSE"],
            cwd=self.root, command_timeout_seconds=12,
            batch_timeout_seconds=40, require_interactive=False,
        )
        self.assertTrue(rows["completed"], rows)
        self.assertTrue(rows["results"][0]["skipped_pause"])
        self.assertIn("AFTER_PAUSE", rows["results"][1]["stdout"])

    def test_cooperative_bat_pause_skips_without_keyboard_input(self):
        script = self.root / "inside_pause.bat"
        script.write_text(
            "@echo off\necho BEFORE\n"
            "if not defined SMARTAGENT_NONINTERACTIVE pause\n"
            "echo AFTER\n",
            encoding="ascii",
        )
        rows = run_visible_cmd_batch(
            ["call inside_pause.bat", "echo FINISHED"],
            cwd=self.root, command_timeout_seconds=8,
            batch_timeout_seconds=22, require_interactive=False,
        )
        self.assertTrue(rows["completed"], rows)
        self.assertIn("BEFORE", rows["results"][0]["stdout"])
        self.assertIn("AFTER", rows["results"][0]["stdout"])
        self.assertIn("FINISHED", rows["results"][1]["stdout"])

    def test_requested_cmd_path_is_working_directory_not_userprofile(self):
        rows = run_visible_cmd_batch(
            ["echo %CD%", "echo %USERPROFILE%"],
            cwd=self.root, command_timeout_seconds=10,
            batch_timeout_seconds=30, require_interactive=False,
        )
        self.assertTrue(rows["completed"], rows)
        self.assertEqual(
            os.path.normcase(rows["results"][0]["stdout"].strip()),
            os.path.normcase(str(self.root)),
        )
        self.assertNotEqual(
            os.path.normcase(rows["results"][1]["stdout"].strip()),
            os.path.normcase(str(self.root)),
        )

    def test_timeout_builtin_waits_without_redirected_stdin(self):
        # Regression for the real-user failure:
        # "ERROR: Input redirection is not supported, exiting".
        import time
        start = time.monotonic()
        rows = run_visible_cmd_batch(
            ["timeout /t 2 /nobreak", "echo AFTER_WAIT"],
            cwd=self.root, command_timeout_seconds=10,
            batch_timeout_seconds=30, require_interactive=False,
        )
        elapsed = time.monotonic() - start
        self.assertTrue(rows["completed"], rows)
        self.assertEqual([x["exit_code"] for x in rows["results"]], [0, 0])
        self.assertGreaterEqual(elapsed, 1.3, rows)
        self.assertIn("AFTER_WAIT", rows["results"][1]["stdout"])

    def test_legacy_bat_pause_does_not_get_fake_skip(self):
        # A non-cooperative BAT pause must not be silently rewritten or
        # receive automatic keystrokes that could confirm unsafe actions.
        legacy = self.root / "legacy_pause.bat"
        legacy.write_text(
            "@echo off\necho BEFORE\npause\necho NEVER\n",
            encoding="ascii",
        )
        rows = run_visible_cmd_batch(
            ["call legacy_pause.bat", "echo NEXT_NOT_RUN"],
            cwd=self.root, command_timeout_seconds=1,
            batch_timeout_seconds=9, require_interactive=False,
        )
        self.assertFalse(rows["completed"], rows)
        self.assertEqual(rows["reason"], "TG_CMD_LINE_TIMEOUT", rows)
        self.assertEqual(len(rows["results"]), 1, rows)
        self.assertTrue(rows["results"][0]["timed_out"])

    def test_command_timeout_does_not_start_next_command(self):
        rows = run_visible_cmd_batch(
            ["ping -n 15 127.0.0.1 >nul", "echo NEVER_RUN"],
            cwd=self.root, command_timeout_seconds=1,
            batch_timeout_seconds=8, require_interactive=False,
        )
        self.assertFalse(rows["completed"], rows)
        self.assertEqual(rows["reason"], "TG_CMD_LINE_TIMEOUT", rows)
        self.assertEqual(len(rows["results"]), 1, rows)
        self.assertTrue(rows["results"][0]["timed_out"])

    def test_desktop_precondition_and_acl_on_fail_closed(self):
        with patch("RemoteAgent.telegram_console_cmd._interactive_desktop_session",
                   return_value=False):
            with self.assertRaisesRegex(TelegramConsoleError,
                                        "TG_CMD_DESKTOP_SESSION_REQUIRED"):
                run_visible_cmd_batch(
                    ["echo hello"], cwd=self.root,
                    command_timeout_seconds=10,
                    batch_timeout_seconds=15,
                )
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}), \
             patch("RemoteAgent.telegram_console_cmd.run_visible_cmd_batch") as launched:
            with self.assertRaisesRegex(CmdControlError,
                                        "TG_CMD_VISIBLE_CONSOLE_ACL_ON_UNAVAILABLE"):
                execute_cmd_request(
                    self.msg("echo hello"),
                    user_id=11, chat_id=22, policy=self.policy,
                )
            launched.assert_not_called()

    def test_production_direct_cmd_uses_one_visible_batch_not_runner(self):
        fake = {
            "console_started": True, "console_exit_code": 0,
            "completed": True, "reason": "",
            "results": [
                {"line": 1, "exit_code": 0, "status": "SUCCESS",
                 "stdout": "one", "stderr": "", "timed_out": False},
                {"line": 2, "exit_code": 0, "status": "SUCCESS",
                 "stdout": "two", "stderr": "", "timed_out": False},
            ],
        }
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}), \
             patch("RemoteAgent.telegram_console_cmd.run_visible_cmd_batch",
                   return_value=fake) as launched:
            result = execute_cmd_request(
                self.msg("echo one", "echo two"),
                user_id=11, chat_id=22, policy=self.policy,
            )
            self.assertEqual(result["status"], "SUCCESS", result)
            self.assertIn("CREATE_NEW_CONSOLE", result["message"])
            self.assertIn("stdout:\none", result["message"])
            self.assertEqual(launched.call_count, 1)
            self.assertEqual(launched.call_args.args[0],
                             ("echo one", "echo two"))

    def test_environment_prefers_system_git_without_modifying_machine(self):
        with tempfile.TemporaryDirectory(prefix="git-os-") as git_temp:
            native = Path(git_temp)
            (native / "git.exe").write_bytes(b"dummy")
            root = Path(__file__).resolve().parents[3]
            bundled = str(root / ".venv" / "git" / "cmd")
            with patch.dict(os.environ, {
                "PATH": bundled + os.pathsep + str(native),
            }):
                before = os.environ["PATH"]
                env = _console_environment(self.root)
                self.assertEqual(os.environ["PATH"], before)
                self.assertNotIn(bundled.lower(), env["PATH"].lower())
                self.assertIn(str(native), env["PATH"])

    def test_staging_path_rejects_metacharacters(self):
        for value in ('C:\\x&y', 'C:\\x%ok', 'C:\\x"y'):
            if "%" in value:
                self.assertIn("%%", _batch_path(Path(value)))
            else:
                with self.assertRaises(TelegramConsoleError):
                    _batch_path(Path(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
