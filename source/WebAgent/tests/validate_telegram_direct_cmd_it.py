"""Telegram direct-CMD fixed software route: Windows offline regression."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from RemoteAgent.remote_feature_catalog import (
    command_for_remote_callback, remote_feature_inline_keyboard,
    render_remote_feature_list,
)
from RemoteAgent.telegram_direct_cmd import (
    CmdControlPolicy, CmdControlError, execute_cmd_request,
    is_direct_cmd_request, parse_cmd_request, render_cmd_template,
)
from RemoteAgent.telegram_transport import (
    TelegramOffsetStore, TelegramReceiver, TelegramReceiverConfig,
)
from agent_core.remote_control_dispatcher import RemoteControlDispatcher


class FakeLog:
    def __init__(self):
        self.rows = []

    def write(self, *args, **kwargs):
        self.rows.append((args, kwargs))


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.acks = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), str(text), kwargs))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_id, *, text=""):
        self.acks.append((callback_id, text))


class NoTaskIngress:
    def accept(self, *_args, **_kwargs):
        raise AssertionError("Direct CMD must never enter Agent Task Queue")


def packet(uid, chat, *, text=None, callback=None, update_id=7):
    if callback:
        return {"update_id": update_id,
                "callback_query": {
                    "id": "CBA-" + str(update_id), "data": callback,
                    "from": {"id": uid, "is_bot": False},
                    "message": {"message_id": 13,
                                "chat": {"id": chat, "type": "private"}},
                }}
    return {"update_id": update_id,
            "message": {
                "message_id": 12, "from": {"id": uid, "is_bot": False},
                "chat": {"id": chat, "type": "private"}, "text": text,
            }}


class TelegramDirectCmdIT(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="tgcmd-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.logs = FakeLog()
        self.client = FakeTelegram()
        self.dispatcher = RemoteControlDispatcher(
            root=self.root, python="python", runtime_log=self.logs,
            control_plane=SimpleNamespace(), session_state=lambda: {},
            request_restart=lambda _: {}, request_close=lambda _: {},
        )
        self.receiver = TelegramReceiver(
            config=TelegramReceiverConfig(
                enabled=True, bot_token="UNIT_TEST",
                workspace=str(self.root), allowed_user_ids=(11,),
                allowed_chat_ids=(22,),
            ),
            client=self.client,
            offset_store=TelegramOffsetStore(self.root / "offset.json"),
            ingress=NoTaskIngress(),
            control_handler=self.dispatcher.handle,
            task_admission_guard=lambda _: (True, ""),
        )
        self.template = render_cmd_template(self.root).split(
            "run cmd by TG\n", 1
        )[1].split("\n\n每行", 1)[0]
        self.message = "run cmd by TG\n" + self.template
        self.policy = CmdControlPolicy(True, frozenset({11}), frozenset({22}))

    def test_keyboard_and_callback_and_form_bypass_agent_queue(self):
        listing = render_remote_feature_list()
        self.assertIn("10. 執行cmd", listing)
        buttons = remote_feature_inline_keyboard()["inline_keyboard"]
        callback = next(
            button["callback_data"]
            for row in buttons for button in row if button["text"] == "執行cmd"
        )
        self.assertEqual(command_for_remote_callback(callback), "執行cmd")
        self.receiver._process_updates([packet(11, 22, callback=callback)])
        self.assertTrue(self.receiver.wait_for_controls(5))
        self.assertIn('run cmd by TG', self.client.sent[-1][1])
        self.assertIn(str(self.root), self.client.sent[-1][1])
        self.assertEqual(self.client.acks[-1], ("CBA-7", "已收到"))

        with patch(
            "RemoteAgent.telegram_transport.TelegramReceiverConfig.from_env",
            return_value=self.receiver.config,
        ), patch(
            "RemoteAgent.telegram_direct_cmd.execute_cmd_request",
            return_value={"message": "CMD 執行結果 TEST\n狀態：SUCCESS",
                          "control_id": "TEST", "status": "SUCCESS",
                          "results": [{"exit_code": 0}]},
        ) as execute:
            self.receiver._process_updates([
                packet(11, 22, text=self.message, update_id=8),
            ])
            self.assertTrue(self.receiver.wait_for_controls(5))
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.kwargs["trusted_install_root"], self.root)
            validated = execute.call_args.kwargs["policy"]
            validated.assert_authorized(11, 22)
            with self.assertRaises(CmdControlError):
                validated.assert_authorized(99, 22)
        self.assertIn("狀態：SUCCESS", self.client.sent[-1][1])

    def test_invalid_sender_and_nonprivate_rejected(self):
        self.receiver._process_updates([
            packet(12, 22, text=self.message, update_id=9),
        ])
        self.assertFalse(self.client.sent)
        self.assertFalse(self.receiver._control_threads)

    def test_format_bounds_and_explicit_enable_required(self):
        self.assertTrue(is_direct_cmd_request(self.message))
        self.assertFalse(is_direct_cmd_request("請解釋 run cmd by TG"))
        cwd, commands = parse_cmd_request(self.message)
        self.assertEqual(cwd, self.root)
        self.assertEqual(commands, ("echo hello", "echo time"))
        with self.assertRaises(CmdControlError):
            CmdControlPolicy(False, frozenset({11}), frozenset({22})).assert_authorized(11, 22)
        with self.assertRaisesRegex(CmdControlError, "TG_CMD_RECEIVER_AUTH_REQUIRED"):
            execute_cmd_request(self.message, user_id=11, chat_id=22)
        with self.assertRaises(CmdControlError):
            self.policy.assert_authorized(12, 22)
        with self.assertRaises(CmdControlError):
            parse_cmd_request(self.message + "\n" + "\n".join(
                '"echo x"' for _ in range(15)
            ))
        with self.assertRaises(CmdControlError):
            parse_cmd_request(self.message.replace(str(self.root), "relative-path"))

    def test_sequential_output_and_nonzero_stops_following_line(self):
        seen = []
        def success(cmd, **kwargs):
            seen.append((cmd, kwargs))
            return {"process_started": True, "exit_code": 0,
                    "stdout": "hello", "stderr": "", "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            good = execute_cmd_request(
                self.message, user_id=11, chat_id=22,
                policy=self.policy, runner=success,
            )
            self.assertEqual(good["status"], "SUCCESS")
            self.assertEqual(len(seen), 2)
            self.assertTrue(all(x[0][-2] == "/c" for x in seen))
            self.assertTrue(all(x[1]["cwd"] == self.root for x in seen))
            self.assertIn("Exit Code: 0", good["message"])

            seen.clear()
            def failure(cmd, **kwargs):
                seen.append(cmd)
                return {"process_started": True, "exit_code": 7,
                        "stdout": "", "stderr": "oops"}
            bad = execute_cmd_request(
                self.message, user_id=11, chat_id=22,
                policy=self.policy, runner=failure,
            )
            self.assertEqual(len(seen), 1)
            self.assertEqual(bad["status"], "FAILED")
            self.assertIn("Exit Code: 7", bad["message"])

    def test_glued_or_repeated_template_rejected_before_execution(self):
        for suffix in (
            '"git status --short"run cmd by TG',
            'run cmd by TG',
            'cmd path: "C:\\Other"',
            'command:',
        ):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(
                CmdControlError, "TG_CMD_DUPLICATE_TEMPLATE_OR_GLUED_HEADER"
            ):
                parse_cmd_request(self.message + "\n" + suffix)

    def test_long_git_fetch_completes_before_next_command(self):
        command_text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\ngit fetch origin\ngit status --short'
        )
        seen = []
        def complete(cmd, **kwargs):
            seen.append((cmd[-1], kwargs["timeout"], kwargs["noninteractive"]))
            return {"process_started": True, "exit_code": 0,
                    "stdout": "", "stderr": "", "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                command_text, user_id=11, chat_id=22,
                policy=self.policy, runner=complete,
            )
        self.assertEqual(reply["status"], "SUCCESS", reply)
        self.assertEqual(seen, [("git fetch origin", 300, True),
                                ("git status --short", 300, True)])
        self.assertIn("單筆上限 300s", reply["message"])

    def test_fetch_dns_error_reports_environment_diagnostic_not_timeout(self):
        text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\ngit fetch origin\necho NEVER_RUN'
        )
        seen = []
        def dns_failed(cmd, **kwargs):
            seen.append(cmd[-1])
            return {"process_started": True, "exit_code": 128,
                    "stdout": "",
                    "stderr": "fatal: unable to access URL: Could not resolve host: github.com",
                    "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                text, user_id=11, chat_id=22,
                policy=self.policy, runner=dns_failed,
            )
        self.assertEqual(seen, ["git fetch origin"])
        self.assertEqual(reply["status"], "FAILED")
        self.assertIn("GIT_DNS_UNRESOLVED", reply["message"])
        self.assertNotIn("TIMED_OUT", reply["message"])
        self.assertEqual(len(reply["results"]), 1)

    def test_fetch_missing_ref_is_not_retried_or_rewritten(self):
        text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\ngit fetch origin feat\necho NEVER_RUN'
        )
        seen = []
        def ref_failed(cmd, **kwargs):
            seen.append(cmd[-1])
            return {"process_started": True, "exit_code": 128, "stdout": "",
                    "stderr": "fatal: couldn't find remote ref feat",
                    "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                text, user_id=11, chat_id=22,
                policy=self.policy, runner=ref_failed,
            )
        self.assertEqual(seen, ["git fetch origin feat"])
        self.assertIn("GIT_REMOTE_REF_NOT_FOUND", reply["message"])
        self.assertEqual(len(reply["results"]), 1)

    def test_pause_line_skip_does_not_execute_itself(self):
        text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\npause\necho finished'
        )
        seen = []
        def completed(cmd, **kwargs):
            seen.append(cmd[-1])
            return {"process_started": True, "exit_code": 0,
                    "stdout": "finished", "stderr": "", "timed_out": False}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                text, user_id=11, chat_id=22,
                policy=self.policy, runner=completed,
            )
        self.assertEqual(reply["status"], "SUCCESS", reply)
        self.assertEqual(seen, ["echo finished"])
        self.assertTrue(reply["results"][0]["skipped_pause"])
        self.assertIn("SKIPPED_PAUSE", reply["message"])

    def test_timeout_stops_batch_even_with_remaining_commands(self):
        text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\necho slow\necho NEVER_RUN'
        )
        seen = []
        def timed_out(cmd, **kwargs):
            seen.append(cmd[-1])
            return {"process_started": True, "exit_code": 1,
                    "stdout": "", "stderr": "", "timed_out": True}
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                text, user_id=11, chat_id=22,
                policy=self.policy, runner=timed_out,
            )
        self.assertEqual(seen, ["echo slow"])
        self.assertEqual(reply["status"], "FAILED")
        self.assertIn("TIMED_OUT", reply["message"])
        self.assertEqual(len(reply["results"]), 1)

    def test_bat_pause_with_nul_stdin_completes_without_input(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows cmd.exe required")
        from agent_core.bounded_process import run_bounded_process
        script = self.root / "tg_pause_test.bat"
        script.write_text(
            "@echo off\necho BEFORE_PAUSE\npause\necho AFTER_PAUSE\n",
            encoding="ascii",
        )
        exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "cmd.exe")
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=False):
            info = run_bounded_process(
                [exe, "/d", "/s", "/c", "tg_pause_test.bat"],
                cwd=self.root, timeout=8, shell=False,
                security_command="tg_pause_test.bat",
                noninteractive=True,
            )
        self.assertEqual(info.get("exit_code"), 0, info)
        self.assertFalse(info.get("timed_out"), info)
        self.assertIn("BEFORE_PAUSE", info.get("stdout", ""))
        self.assertIn("AFTER_PAUSE", info.get("stdout", ""))

    def test_real_windows_cmd_echo_smoke_no_side_effects(self):
        import subprocess
        actual_text = (
            f'run cmd by TG\ncmd path: "{self.root}"\n'
            'command:\n"echo hello"\n"echo time"'
        )
        def benign_cmd_runner(argv, **kwargs):
            process = subprocess.run(
                argv, cwd=kwargs["cwd"], capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=kwargs["timeout"], check=False,
            )
            return {
                "process_started": True, "exit_code": process.returncode,
                "stdout": process.stdout.strip(), "stderr": process.stderr.strip(),
                "timed_out": False,
            }
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            reply = execute_cmd_request(
                actual_text, user_id=11, chat_id=22,
                policy=self.policy, runner=benign_cmd_runner,
            )
        self.assertEqual(reply["status"], "SUCCESS")
        self.assertIn("hello", reply["message"])
        self.assertIn("time", reply["message"])
        self.assertEqual(len(reply["results"]), 2)
        self.assertEqual(sorted(self.root.iterdir()), [self.root / "offset.json"]
                         if (self.root / "offset.json").exists() else [])

    def test_unknown_workspace_and_blocked_command(self):
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": ()}):
            with self.assertRaisesRegex(CmdControlError, "WORKSPACE_NOT_AUTHORIZED"):
                execute_cmd_request(
                    self.message, user_id=11, chat_id=22, policy=self.policy,
                    runner=lambda *args, **kwargs: self.fail("must not execute"),
                )
        unsafe = self.message.replace('"echo hello"', '"diskpart"')
        with patch("agent_core.windows_security.restricted_executor_required",
                   return_value=True), \
             patch("agent_core.workspace_access.access_snapshot",
                   return_value={"writable_workspaces": (str(self.root),)}):
            with self.assertRaisesRegex(CmdControlError, "COMMAND_REJECTED"):
                execute_cmd_request(
                    unsafe, user_id=11, chat_id=22, policy=self.policy,
                    runner=lambda *args, **kwargs: self.fail("must not execute"),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
