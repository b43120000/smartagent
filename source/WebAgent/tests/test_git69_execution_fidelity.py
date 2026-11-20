"""Git69 shell contract, process evidence and sidecar process integration."""
import os
import hashlib
import json
import re
import subprocess
import uuid
import zipfile
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

class RawStreamAndCollectorIT(unittest.TestCase):
    def test_full_capture_retains_whitespace_and_raw_sha256(self):
        from agent_core.bounded_process import _read_capture, _stream_sha256
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"raw.bin"
            raw=b"  leading  \r\ntrailing \n"
            p.write_bytes(raw)
            digest=_stream_sha256(p)
            self.assertEqual(digest,hashlib.sha256(raw).hexdigest())
            text,size,truncated,local_ref=_read_capture(p,4096)
            self.assertEqual(text,raw.decode("utf-8"))
            self.assertEqual(size,len(raw))
            self.assertFalse(truncated)
            self.assertFalse(local_ref)

    def test_large_capture_retains_ref_and_raw_digest(self):
        from agent_core.bounded_process import _read_capture, _stream_sha256
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"large.bin"
            raw=(b"F69_BLOCK_"*20000)
            p.write_bytes(raw)
            digest=_stream_sha256(p)
            text,size,truncated,local_ref=_read_capture(p,512)
            self.assertTrue(truncated)
            self.assertEqual(size,len(raw))
            self.assertEqual(digest,hashlib.sha256(raw).hexdigest())
            self.assertTrue(Path(local_ref).is_file())
            self.assertEqual(Path(local_ref).read_bytes(),raw)
            self.assertIn("middle omitted",text)

    @unittest.skipUnless(os.name=="nt","collect.bat ZIP integration needs Windows")
    def test_collect_bat_includes_unencrypted_fidelity_file(self):
        root=Path(__file__).resolve().parents[3]
        task="TASK-G69"+uuid.uuid4().hex[:14].upper()
        request="RR-G69"+uuid.uuid4().hex[:14].upper()
        result=subprocess.run(["cmd.exe","/d","/c",str(root/"collect.bat"),task,request],
            cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,errors="replace",timeout=120)
        self.assertEqual(result.returncode,0,
            result.stdout[-3500:]+result.stderr[-3500:])
        m=re.search(r"(?m)^\[COLLECT\] ZIP:\s*(.+)$",result.stdout)
        self.assertIsNotNone(m,result.stdout)
        archive=Path(m.group(1).strip())
        try:
            with zipfile.ZipFile(archive) as zf:
                self.assertIsNone(zf.testzip())
                self.assertIn("executor_execution_fidelity.json",zf.namelist())
                data=json.loads(zf.read("executor_execution_fidelity.json").decode("utf-8-sig"))
                self.assertIsInstance(data,list)
        finally:
            archive.unlink(missing_ok=True)

class FidelityAdditionalCases(unittest.TestCase):
    @unittest.skipUnless(os.name=="nt","Windows process launch failure")
    def test_start_failure_is_not_child_exit(self):
        with tempfile.TemporaryDirectory() as td:
            missing=str(Path(td)/"not_existing_git69_binary.exe")
            spec=execution_contract({"command":missing,"shell_mode":"DIRECT","argv":["--version"]})
            with patch("agent_core.tools.require_command_allowed"),patch(
                "agent_core.command_security.require_command_allowed"),patch(
                "agent_core.turbo_broker_admission.assess",return_value={"decision":"NORMAL"}):
                got=tools._run_explicit_shell_capture(spec,10,td,cwd=td,
                    telemetry=None,approval_manifest=None)
            self.assertIsNone(got["exit_code"])
            self.assertFalse(got["process_started"])
            self.assertTrue(got["error"])

    @unittest.skipUnless(os.name=="nt","Windows PowerShell behavior")
    def test_powershell_stderr_does_not_override_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("agent_core.tools.require_command_allowed"),patch(
                "agent_core.command_security.require_command_allowed"),patch(
                "agent_core.turbo_broker_admission.assess",return_value={"decision":"NORMAL"}):
                got=tools._run_powershell_capture(
                    "Write-Error 'F69_ERROR'; Write-Output 'F69_OK'",
                    timeout=20,capture_root=td,cwd=td)
            self.assertEqual(got["exit_code"],0,got)
            self.assertIn("F69_OK",got["stdout"])
            self.assertIn("F69_ERROR",got["stderr"])

    def test_true_raw_digest_when_output_truncated(self):
        from agent_core.bounded_process import run_bounded_process
        with tempfile.TemporaryDirectory() as td:
            script="import sys; sys.stdout.buffer.write(b'Z'*20000+b'\\n'); sys.stderr.buffer.write(b'Y'*20000+b'\\n')"
            with patch("agent_core.command_security.require_command_allowed"),patch(
                "agent_core.turbo_broker_admission.assess",return_value={"decision":"NORMAL"}):
                got=run_bounded_process([sys.executable,"-c",script],cwd=td,timeout=20,
                    capture_root=td,max_stream_bytes=512)
            self.assertEqual(got["exit_code"],0,got)
            self.assertTrue(got["stdout_truncated"])
            self.assertTrue(got["stderr_truncated"])
            expected_out=("Z"*20000+"\n").encode()
            expected_err=("Y"*20000+"\n").encode()
            self.assertEqual(got["stdout_raw_sha256"],hashlib.sha256(expected_out).hexdigest())
            self.assertEqual(got["stderr_raw_sha256"],hashlib.sha256(expected_err).hexdigest())
            self.assertEqual(got["stdout_bytes"],len(expected_out))
            self.assertEqual(got["stderr_bytes"],len(expected_err))
            self.assertEqual(Path(got["stdout_ref"]).read_bytes(),expected_out)
            self.assertEqual(Path(got["stderr_ref"]).read_bytes(),expected_err)


class RestrictedStreamFidelityUT(unittest.TestCase):
    def test_exact_crlf_whitespace_and_digest(self):
        from agent_core.restricted_executor_service import _capture_restricted_stream
        with tempfile.TemporaryDirectory() as td:
            raw = b"  leading  \r\ntrailing  \n"
            result = _capture_restricted_stream(raw, 1024, Path(td), "EXEC-" + "A"*32, "stdout")
            self.assertEqual(result["stdout"], raw.decode("utf-8"))
            self.assertEqual(result["stdout_bytes"], len(raw))
            self.assertFalse(result["stdout_truncated"])
            self.assertEqual(result["stdout_raw_sha256"], hashlib.sha256(raw).hexdigest())

    def test_truncated_stream_keeps_full_raw_file(self):
        from agent_core.restricted_executor_service import _capture_restricted_stream
        with tempfile.TemporaryDirectory() as td:
            raw = b"  X\r\n" * 1500
            result = _capture_restricted_stream(raw, 128, Path(td), "EXEC-" + "B"*32, "stderr")
            self.assertTrue(result["stderr_truncated"])
            self.assertEqual(result["stderr_bytes"], len(raw))
            self.assertEqual(Path(result["stderr_ref"]).read_bytes(), raw)
            self.assertEqual(result["stderr_raw_sha256"], hashlib.sha256(raw).hexdigest())

    def test_bad_request_id_does_not_write(self):
        from agent_core.restricted_executor_service import _capture_restricted_stream
        with tempfile.TemporaryDirectory() as td, self.assertRaises(RuntimeError):
            _capture_restricted_stream(b"x", 1, Path(td), "../unsafe", "stdout")

class FailureOriginUT(unittest.TestCase):
    def observed(self, fake):
        with tempfile.TemporaryDirectory() as td:
            with patch("agent_core.tools.resolve_command_working_directory", return_value=Path(td)), patch(
                    "agent_core.tools._run_explicit_shell_capture", return_value=fake):
                return decode_result(tools.tool_run_command(
                    r"C:\fixtures\missing.exe", "GENERAL", shell_mode="DIRECT", argv=[],
                    working_directory=td))

    def test_process_start_failed_does_not_claim_execution(self):
        receipt = self.observed({"exit_code":None, "process_started":False,
            "error_stage":"PROCESS_START", "error":"FileNotFoundError",
            "process_state":"NOT_STARTED", "timed_out":False})
        self.assertEqual(receipt["data"]["failure_origin"], "PROCESS_START")
        self.assertFalse(receipt["executed"])
        self.assertIsNone(receipt["data"]["exit_code"])
        self.assertFalse(receipt["data"]["output_complete"])

    def test_post_start_capture_error_is_unknown(self):
        receipt = self.observed({"exit_code":None, "process_started":True,
            "error_stage":"CAPTURE", "error":"OSError", "timed_out":False,
            "process_state":"UNKNOWN", "possible_partial_execution":True})
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["data"]["failure_origin"], "CAPTURE")
        self.assertIsNone(receipt["data"]["exit_code"])

    def test_admission_rejection_cannot_claim_execution(self):
        receipt = self.observed({"exit_code":None, "process_started":False,
            "security_rejected":True, "error":"SECURITY_COMMAND_REJECTED",
            "timed_out":False})
        self.assertEqual(receipt["status"], "REJECTED")
        self.assertFalse(receipt["executed"])
        self.assertEqual(receipt["data"]["failure_origin"], "ADMISSION")

class RealWindowsAdditionalIT(unittest.TestCase):
    @unittest.skipUnless(os.name=="nt", "Windows Git semantics")
    def test_real_git_exit_0_and_128(self):
        import shutil
        executable = shutil.which("git")
        if not executable:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            with patch("agent_core.tools.require_command_allowed"), patch(
                "agent_core.command_security.require_command_allowed"), patch(
                "agent_core.turbo_broker_admission.assess", return_value={"decision":"NORMAL"}):
                def invoke(args):
                    return tools._run_explicit_shell_capture(
                        execution_contract({"command":str(Path(executable).resolve()),
                            "shell_mode":"DIRECT", "argv":args}),
                        20, td, cwd=td, telemetry=None, approval_manifest=None)
                ok = invoke(["--version"])
                bad = invoke(["-C", td, "rev-parse", "HEAD"])
            self.assertEqual(ok["exit_code"], 0, ok)
            self.assertEqual(bad["exit_code"], 128, bad)
            self.assertTrue(bad["stderr"], bad)

    @unittest.skipUnless(os.name=="nt", "Windows BAT semantics")
    def test_cmd_call_bat_exit_code_is_37(self):
        with tempfile.TemporaryDirectory() as td:
            bat = Path(td)/"f69_exit.bat"
            bat.write_bytes(b"@echo off\r\necho F69_BAT_ERR 1>&2\r\nexit /b 37\r\n")
            spec = execution_contract({"command":'call "'+str(bat)+'"', "shell_mode":"CMD"})
            with patch("agent_core.tools.require_command_allowed"), patch(
                "agent_core.command_security.require_command_allowed"), patch(
                "agent_core.turbo_broker_admission.assess", return_value={"decision":"NORMAL"}):
                got = tools._run_explicit_shell_capture(spec, 20, td, cwd=td,
                    telemetry=None, approval_manifest=None)
            self.assertEqual(got["exit_code"], 37, got)
            self.assertIn("F69_BAT_ERR", got["stderr"])

    @unittest.skipUnless(os.name=="nt", "Windows PowerShell exception")
    def test_powershell_terminating_error_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("agent_core.tools.require_command_allowed"), patch(
                "agent_core.command_security.require_command_allowed"), patch(
                "agent_core.turbo_broker_admission.assess", return_value={"decision":"NORMAL"}):
                got = tools._run_powershell_capture("throw 'G69_ABORT'",
                    timeout=20, capture_root=td, cwd=td)
            self.assertNotEqual(got["exit_code"], 0, got)
            self.assertTrue(got["stderr"], got)

    @unittest.skipUnless(os.name=="nt", "Windows timeout")
    def test_real_timeout_does_not_create_success(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("agent_core.tools.require_command_allowed"), patch(
                "agent_core.command_security.require_command_allowed"), patch(
                "agent_core.turbo_broker_admission.assess", return_value={"decision":"NORMAL"}):
                got = tools._run_explicit_shell_capture(
                    execution_contract({"command":sys.executable,"shell_mode":"DIRECT",
                        "argv":["-c", "import time; time.sleep(4)"]}),
                    1, td, cwd=td, telemetry=None, approval_manifest=None)
            self.assertTrue(got["timed_out"], got)
            self.assertTrue(got["process_started"], got)
