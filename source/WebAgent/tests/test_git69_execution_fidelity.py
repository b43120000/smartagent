"""Git69 shell contract, process evidence and sidecar process integration."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_core.shell_execution import execution_contract, effective_action_for_policy, ShellModeError
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.protocol_v8 import validate_model_action
from agent_core.command_risk import classify_command
from agent_core.action_dependencies import command_is_proven_observation
from agent_core.tool_result_contract import decode_result
from agent_core import tools

GIT = r"C:\smartagent\.venv\git\cmd\git.exe"
GIT_ARGS = ["ls-remote", "https://github.com/b43120000/test.git", "HEAD"]

class ShellContractUT(unittest.TestCase):
    def action(self, **updates):
        a={"tool":"run_command", "action_id":"ACT-G69", "command":GIT,
           "argv":GIT_ARGS, "shell_mode":"DIRECT", "operation":"GIT_INSPECT"}
        a.update(updates)
        return a
    def test_legacy_ps_default(self):
        s=execution_contract({"command":"Write-Output 'ok'"})
        self.assertEqual((s["shell_mode"],s["policy_command"]),
                         ("POWERSHELL","Write-Output 'ok'"))
    def test_direct_argv_is_literal_and_readonly_git(self):
        a=self.action()
        s=execution_contract(a)
        self.assertEqual(s["launch_argv"],[GIT,*GIT_ARGS])
        self.assertTrue(command_is_proven_observation(effective_action_for_policy(a)))
        self.assertEqual(classify_command(effective_action_for_policy(a)),"PROVEN_READ")
        ok,diag=validate_tool_envelope(a)
        self.assertTrue(ok,diag)
        self.assertEqual(validate_model_action(a)["operation"],"GIT_INSPECT")
    def test_git_clone_cannot_be_labelled_inspect(self):
        ok,diag=validate_tool_envelope(self.action(argv=["clone","https://github.com/a/b.git"]))
        self.assertFalse(ok)
        self.assertEqual(diag["reason"],"run_command_operation_mismatch")
    def test_bad_modes_argv_and_exe_rejected(self):
        invalid=[
            {"command":"echo hello","shell_mode":"BASH"},
            {"command":"echo hello","shell_mode":"CMD","argv":["x"]},
            {"command":"relative.exe","shell_mode":"DIRECT","argv":[]},
            {"command":GIT,"shell_mode":"DIRECT","argv":["ok\nbad"]},
            {"command":GIT,"shell_mode":"DIRECT"},
        ]
        for a in invalid:
            with self.subTest(a=a),self.assertRaises(ShellModeError):
                execution_contract(a)
    def test_native_broker_non_ps_fails_closed(self):
        ok,diag=validate_tool_envelope(self.action(),native_broker=True)
        self.assertFalse(ok)
        self.assertEqual(diag["reason"],"NATIVE_BROKER_SHELL_MODE_UNSUPPORTED")
    def test_cmd_launch_form_is_independent(self):
        self.assertEqual(execution_contract({"command":"echo test","shell_mode":"CMD"})["launch_argv"],
                         ["cmd.exe","/d","/s","/c","echo test"])

class SidecarIntegration(unittest.TestCase):
    @unittest.skipUnless(os.name=="nt","requires real Windows process")
    def test_direct_actual_child_exit_stderr_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            s=execution_contract({"command":sys.executable,"shell_mode":"DIRECT",
                "argv":["-c","import sys; print('F69_OUT'); print('F69_ERR',file=sys.stderr); sys.exit(7)"]})
            with patch("agent_core.tools.require_command_allowed"),patch("agent_core.command_security.require_command_allowed"),patch("agent_core.turbo_broker_admission.assess",return_value={"decision":"NORMAL"}):
                r=tools._run_explicit_shell_capture(s,20,td,cwd=td,telemetry=None,approval_manifest=None)
            self.assertEqual(r["exit_code"],7,r)
            self.assertIn("F69_OUT",r["stdout"])
            self.assertIn("F69_ERR",r["stderr"])
            self.assertTrue(r["process_started"])

    @unittest.skipUnless(os.name=="nt","requires real CMD")
    def test_cmd_actual_child_exit_stderr_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            s=execution_contract({"command":"echo F69_OUT & echo F69_ERR 1>&2 & exit /b 9","shell_mode":"CMD"})
            with patch("agent_core.tools.require_command_allowed"),patch("agent_core.command_security.require_command_allowed"),patch("agent_core.turbo_broker_admission.assess",return_value={"decision":"NORMAL"}):
                r=tools._run_explicit_shell_capture(s,20,td,cwd=td,telemetry=None,approval_manifest=None)
            self.assertEqual(r["exit_code"],9,r)
            self.assertIn("F69_OUT",r["stdout"])
            self.assertIn("F69_ERR",r["stderr"])

    def test_exit_zero_verification_failed_kept_separate(self):
        with tempfile.TemporaryDirectory() as td:
            fake={"exit_code":0,"process_started":True,"process_state":"EXITED","stdout":"OK","stderr":"",
                  "timed_out":False}
            with patch("agent_core.tools.resolve_command_working_directory",return_value=Path(td)),patch(
                "agent_core.tools._run_explicit_shell_capture",return_value=fake),patch(
                "agent_core.tools._evaluate_verification_step",return_value={"spec_valid":True,"passed":False}):
                receipt=decode_result(tools.tool_run_command("C:\\fixtures\\fake.exe","TEST",
                    shell_mode="DIRECT",argv=[],working_directory=td,
                    success_criteria="file exists",verify=[{"action":"file_exists","path":"missing"}]))
            self.assertEqual(receipt["status"],"SUCCEEDED",receipt)
            self.assertEqual(receipt["data"]["verification_status"],"FAIL")
            self.assertEqual(receipt["data"]["failure_origin"],"VERIFICATION")

    def test_timeout_does_not_claim_child_failed(self):
        with tempfile.TemporaryDirectory() as td:
            fake={"exit_code":-9,"process_started":True,"process_state":"EXITED",
                  "stdout":"PARTIAL","stderr":"","timed_out":True}
            with patch("agent_core.tools.resolve_command_working_directory",return_value=Path(td)),patch(
                "agent_core.tools._run_explicit_shell_capture",return_value=fake):
                receipt=decode_result(tools.tool_run_command("C:\\fixtures\\fake.exe","MUTATE",
                    shell_mode="DIRECT",argv=[],working_directory=td))
            self.assertEqual(receipt["status"],"UNKNOWN",receipt)
            self.assertIsNone(receipt["data"]["exit_code"])
            self.assertEqual(receipt["data"]["observed_post_timeout_exit_code"],-9)
            self.assertTrue(receipt["data"]["possible_partial_execution"])
