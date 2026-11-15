"""Standalone tests for git64 local Runtime test gateway (no live OS mutation)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core.runtime_test_gateway import GatewayError, SCHEMA, run_case
from agent_core.tool_result_contract import encode_result


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = str(Path(self.tmp.name).resolve())
        self.directory = str(Path(self.workspace) / "turbo")
        Path(self.directory).mkdir()
        self.command = {
            "tool": "run_command",
            "operation": "TEST",
            "working_directory": self.directory,
            "timeout": 30,
            "command": "Start-Sleep -Seconds 12",
        }
        self.want = {
            "status": "FAILED",
            "data.exit_code": 1,
            "data.sandbox_evidence.native_failure_substage": "CWD_SET_LOCATION",
        }
        self.spec = {
            "schema": SCHEMA,
            "workspace": self.workspace,
            "actions": [{"action": self.command, "expect": self.want}],
        }
        self.policy = lambda: {"writable_workspaces": [self.workspace]}

    @staticmethod
    def native_gate(workspace, action):
        return {"decision": "BROKER_REQUIRED", "reason_code": "BROKER_READY"}

    @staticmethod
    def native_receipt(code=1, native=True):
        return encode_result(
            "FAILED" if code else "SUCCEEDED", "COMMAND_EXITED", tool="run_command",
            executed=True,
            data={
                "exit_code": code,
                "process_state": "EXITED",
                "sandbox_evidence": {
                    "sandbox_backend": (
                        "WIN32_APPCONTAINER" if native else "HOST_EXECUTOR"
                    ),
                    "sandbox_identity_verified": native,
                    "turbo_effective_for_action": "ON" if native else "OFF",
                    "native_failure_substage": (
                        "CWD_SET_LOCATION" if code else ""
                    ),
                    "native_cwd_probes": {
                        "native_cwd_status": "MATCH",
                        "native_cwd_hresult": "00000000",
                        "psdrive_status": "FILESYSTEM",
                        "psdrive_hresult": "00000000",
                        "provider_path_status": "ERROR",
                        "provider_path_hresult": "80070005",
                    },
                },
            },
        )

    def test_live_opt_in_required_without_dispatch(self):
        dispatcher = Mock()
        with self.assertRaisesRegex(GatewayError, "EXPLICIT_LIVE"):
            run_case(self.spec, dispatch=dispatcher)
        dispatcher.assert_not_called()

    def test_workspace_cannot_be_invented_by_fixture(self):
        dispatcher = Mock()
        with self.assertRaisesRegex(GatewayError, "WORKSPACE_NOT_CONFIGURED"):
            run_case(
                self.spec, live_authorized=True,
                policy_loader=lambda: {"writable_workspaces": []},
                admission=self.native_gate, dispatch=dispatcher,
            )
        dispatcher.assert_not_called()

    def test_broker_required_no_fallback_to_normal_executor(self):
        dispatcher = Mock()
        result = run_case(
            self.spec, live_authorized=True, policy_loader=self.policy,
            admission=lambda workspace, action: {
                "decision": "NORMAL", "reason_code": "ACL_OFF"},
            dispatch=dispatcher,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertFalse(result["cases"][0]["executed"])
        dispatcher.assert_not_called()

    def test_exact_action_reaches_runtime_shared_dispatch(self):
        observed = []

        def dispatch(action, *, agent):
            observed.append((dict(action), agent))
            return self.native_receipt()
        result = run_case(
            self.spec, live_authorized=True,
            policy_loader=self.policy, admission=self.native_gate,
            dispatch=dispatch,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["test_actions_observed"], 1)
        self.assertFalse(result["planner_invoked"])
        action, agent = observed[0]
        self.assertEqual(action["command"], "Start-Sleep -Seconds 12")
        self.assertEqual(action["working_directory"], self.directory)
        self.assertEqual(action["operation"], "TEST")
        self.assertEqual(str(agent.workspace_root), self.workspace)
        self.assertTrue(action["action_id"].startswith("ACT-RTG-"))
        self.assertTrue(agent.current_task_id.startswith("TASK-RTG-"))

    def test_unattested_execution_never_passes(self):
        result = run_case(
            self.spec, live_authorized=True,
            policy_loader=self.policy, admission=self.native_gate,
            dispatch=lambda action, *, agent: self.native_receipt(native=False),
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            result["cases"][0]["reason"],
            "GATEWAY_EXECUTED_WITHOUT_NATIVE_ATTESTATION",
        )

    def test_real_receipt_expectations_not_rewritten(self):
        result = run_case(
            self.spec, live_authorized=True,
            policy_loader=self.policy, admission=self.native_gate,
            dispatch=lambda action, *, agent: self.native_receipt(code=0),
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["cases"][0]["assertions"][0]["pass"])

    def test_internal_broker_marker_is_never_fixture_authority(self):
        spec = json.loads(json.dumps(self.spec))
        spec["actions"][0]["action"]["_runtime_native_broker_required"] = True
        with self.assertRaisesRegex(GatewayError, "PRIVATE_MARKER_FORBIDDEN"):
            run_case(spec, live_authorized=True)

    def test_multimodule_integration_same_task_identity(self):
        spec = json.loads(json.dumps(self.spec))
        spec["actions"].append({
            "action": {"tool": "read_file", "path": str(Path(self.directory) / "x.txt")},
            "expect": {"status": "SUCCEEDED"},
        })
        identities = []

        def dispatch(action, *, agent):
            identities.append((agent.current_task_id, action["action_id"]))
            if action["tool"] == "run_command":
                return self.native_receipt()
            return encode_result(
                "SUCCEEDED", "FILE_READ", tool="read_file",
                executed=True, data={"text": "fixture"},
            )

        result = run_case(
            spec, live_authorized=True,
            policy_loader=self.policy,
            admission=lambda ws, action: {
                "decision": (
                    "BROKER_REQUIRED" if action["tool"] == "run_command" else "NORMAL"),
                "reason_code": "TEST_ADMISSION",
            },
            dispatch=dispatch,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(identities), 2)
        self.assertEqual(identities[0][0], identities[1][0])
        self.assertNotEqual(identities[0][1], identities[1][1])

    def test_raw_command_text_and_output_not_in_summary(self):
        result = run_case(
            self.spec, live_authorized=True,
            policy_loader=self.policy, admission=self.native_gate,
            dispatch=lambda action, *, agent: self.native_receipt(),
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Start-Sleep", serialized)
        self.assertIn("80070005", serialized)
        self.assertIn("runtime_receipt_sha256", serialized)

    def test_gateway_exposes_fixed_psdrive_fallback_trace(self):
        from agent_core.tool_result_contract import decode_result

        def dispatch(action, *, agent):
            receipt = decode_result(self.native_receipt())
            receipt["data"]["sandbox_evidence"]["native_command_trace"] = [
                "CWD_GET_LOCATION_INITIAL.PASS",
                "CWD_SET_LOCATION.START",
                "CWD_SET_LOCATION.FAIL",
                "CWD_PSDRIVE_FALLBACK.START",
                "CWD_PSDRIVE_FALLBACK.PASS",
            ]
            return "[TOOL_RESULT] " + json.dumps(receipt)

        result = run_case(
            self.spec, live_authorized=True,
            policy_loader=self.policy, admission=self.native_gate,
            dispatch=dispatch,
        )
        self.assertEqual(result["status"], "PASS")
        native = result["cases"][0]["runtime"]
        self.assertEqual(native["cwd_binding_method"], "PSDRIVE_ROOT_FALLBACK")
        self.assertIn("CWD_PSDRIVE_FALLBACK.PASS", native["native_setup_trace"])

    def test_typed_write_still_requires_native_broker(self):
        spec = {
            "schema": SCHEMA, "workspace": self.workspace, "actions": [
                {"action": {
                    "tool": "write_file",
                    "path": str(Path(self.directory) / "file.txt"),
                    "content": "fixture",
                }, "expect": {"status": "SUCCEEDED"}}
            ],
        }
        dispatch = Mock()
        result = run_case(
            spec, live_authorized=True, policy_loader=self.policy,
            admission=lambda ws, action: {"decision": "NORMAL"},
            dispatch=dispatch,
        )
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        dispatch.assert_not_called()


    def test_policy_flips_to_normal_at_real_execute_tool_no_host_fallback(self):
        # Gate looked native at fixture admission, but the actual shared
        # Runtime observes a changed policy and must NOT launch a host process.
        with patch("agent_core.turbo_broker_admission.assess",
                   return_value={
                       "decision": "NORMAL",
                       "reason_code": "TURBO_SUPPRESSED_ACL_OFF",
                       "evidence": {},
                   }), patch(
                       "agent_core.tools.resolve_command_working_directory",
                       return_value=Path(self.directory),
                   ), patch(
                       "agent_core.security_context._configured_access_roots",
                       return_value=((Path(self.workspace),), (), ()),
                   ), patch(
                       "agent_core.tools._run_powershell_capture"
                   ) as host_capture:
            report = run_case(
                self.spec, live_authorized=True,
                policy_loader=self.policy,
                admission=self.native_gate,
            )
        host_capture.assert_not_called()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(
            report["cases"][0]["runtime"]["tool_reason"],
            "RUNTIME_TEST_NATIVE_ONLY_NO_HOST_FALLBACK",
        )

    def test_bounded_process_rechecks_native_under_context(self):
        from agent_core.bounded_process import (
            native_only_runtime_test, native_only_runtime_test_active,
            run_bounded_process,
        )
        self.assertFalse(native_only_runtime_test_active())
        with patch("agent_core.turbo_broker_admission.assess",
                   return_value={
                       "decision": "NORMAL", "reason_code": "TURBO_DISABLED",
                       "evidence": {},
                   }), patch(
                       "agent_core.workspace_access.resolve_turbo_authorization_workspace",
                       return_value=None,
                   ), patch("agent_core.bounded_process.subprocess.Popen") as launch:
            with native_only_runtime_test():
                self.assertTrue(native_only_runtime_test_active())
                result = run_bounded_process(
                    "Write-Output simulated",
                    cwd=Path(self.directory), timeout=5,
                    security_command="Write-Output simulated",
                )
            self.assertFalse(native_only_runtime_test_active())
        launch.assert_not_called()
        self.assertTrue(result["security_rejected"])
        self.assertFalse(result["process_started"])
        self.assertIn("RUNTIME_TEST_NATIVE_ONLY_NO_HOST_FALLBACK", result["error"])


if __name__ == "__main__":
    unittest.main()
