"""B-P1: real Runtime observation proofs, INSPECT read scope, stale boundaries.

Offline and network-free; no wheel install or user-project writes.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.pr_bcd_observation_evidence import observation_receipt
from agent_core.tool_result_contract import encode_result, decode_result
from agent_core.action_dependencies import unsatisfied_dependencies
from agent_core.tools import execute_tool
from agent_core.security_context import SecurityContext
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop


class LiveBP1EvidenceIT(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="sa-live-b-p1-")
        self.addCleanup(td.cleanup)
        base = Path(td.name).resolve()
        self.workspace = base / "default"
        self.workspace.mkdir()
        self.project = base / "SmartAgent_PR_BCD_LIVE"
        self.wheelhouse = self.project / "wheelhouse"
        self.deps = self.project / "deps"
        self.wheelhouse.mkdir(parents=True)
        self.deps.mkdir()
        self.wheel = self.wheelhouse / "smartagent_pr_bcd_live-1.0-py3-none-any.whl"
        self.wheel.write_bytes(b"offline-wheel-fixture")
        self.request_id = "RR-BP1-EVIDENCE"
        self.epoch = "EPOCH-BP1"
        self.agent = WebAgentToolContext(self.workspace)
        self.agent.current_task_epoch = self.epoch
        self.agent.begin_run(self.request_id, "inspect scoped project", [str(self.project)])
        self.agent.current_task_epoch = self.epoch
        self.policy = patch(
            "agent_core.security_context._configured_access_roots",
            return_value=((self.workspace,), ()),
        )
        self.policy.start()
        self.addCleanup(self.policy.stop)

    def _entry(self, **kwargs):
        return {
            "execution_status": "SUCCEEDED", "result_status": "SUCCEEDED",
            "verification_status": "UNKNOWN", "expectation_status": "",
            "evidence_status": "ACTIVE", "executed": True,
            **kwargs,
        }

    def test_native_list_and_inspection_are_satisfied_observations_not_task_pass(self):
        action = {
            "tool": "list_directory", "path": str(self.wheelhouse),
            "action_id": "p1-list-wheel",
        }
        response = execute_tool(action, agent=self.agent)
        outcome = decode_result(response)
        self.assertEqual(outcome["status"], "SUCCEEDED")
        self.assertEqual(outcome["reason"], "DIRECTORY_LISTED")
        receipt = observation_receipt(action, response, self._entry())
        self.assertEqual(receipt, {"admitted": True, "reason": "NATIVE_DIRECTORY_LIST_CURRENT"})
        self.assertNotEqual(self._entry()["verification_status"], "PASS")

        inspect = {
            "tool": "inspect_directory",
            "paths": [str(self.project), str(self.wheelhouse), str(self.deps)],
            "recursive": False, "action_id": "p1-inspect-three",
        }
        result = execute_tool(inspect, agent=self.agent)
        outcome = decode_result(result)
        self.assertEqual(outcome["status"], "SUCCEEDED", result)
        receipt = observation_receipt(inspect, result, self._entry())
        self.assertEqual(receipt["admitted"], True, receipt)

    def test_incomplete_inspection_cannot_prove_success(self):
        inspect = {
            "tool": "inspect_directory",
            "paths": [str(self.project), str(self.deps)],
            "recursive": False, "action_id": "p1-partial",
        }
        response = execute_tool(inspect, agent=self.agent)
        good = observation_receipt(inspect, response, self._entry())
        self.assertTrue(good["admitted"])
        modified = json.loads(response)
        modified["paths"][0]["inaccessible_count"] = 1
        modified["paths"][0]["errors"] = ["PermissionError"]
        rejected = observation_receipt(inspect, json.dumps(modified), self._entry())
        self.assertFalse(rejected["admitted"], rejected)

        unreadable = dict(self._entry(result_status="PARTIAL", execution_status="PARTIAL"))
        self.assertFalse(observation_receipt(inspect, response, unreadable)["admitted"])

    def test_spoofed_result_or_identity_never_qualifies(self):
        action = {"tool": "list_directory", "action_id": "p1-fake",
                  "path": str(self.wheelhouse)}
        fake = encode_result("SUCCEEDED", "DIRECTORY_LISTED", tool="list_directory",
                             executed=True, resolved_path=str(self.wheelhouse),
                             data={"text": "(空目錄)", "entries": []})
        self.assertTrue(observation_receipt(action, fake, self._entry())["admitted"])
        self.assertFalse(observation_receipt(
            {**action, "path": str(self.deps)}, fake, self._entry())["admitted"])
        self.assertFalse(observation_receipt(
            action, fake, self._entry(executed=False))["admitted"])
        self.assertFalse(observation_receipt(
            action, fake, self._entry(expectation_status="FAIL"))["admitted"])
        self.assertFalse(observation_receipt(
            action, fake, self._entry(evidence_status="STALE"))["admitted"])
        self.assertFalse(observation_receipt(
            action, encode_result("REJECTED", "SCOPE_REJECTED",
                                  tool="list_directory", executed=False),
            self._entry())["admitted"])
        self.assertFalse(observation_receipt(
            action, fake, self._entry(result_status="UNKNOWN"))["admitted"])
        self.assertFalse(observation_receipt(
            action, fake, self._entry(verification_status="FAIL"))["admitted"])
        self.assertFalse(observation_receipt(
            action, "SUCCEEDED", self._entry())["admitted"])

    def test_strict_inspect_read_cwd_and_literal_path_scope(self):
        safe = {
            "tool": "run_command", "operation": "INSPECT",
            "working_directory": str(self.project),
            "command": f"Test-Path -LiteralPath '{self.wheel}'",
            "action_id": "p1-safe-inspect",
        }
        preview = self.agent.preview_tool_scope(safe)
        self.assertTrue(preview["allowed"], preview)
        self.assertEqual(preview["resolved_paths"], [str(self.project)])

        other = self.workspace / "host-secret.txt"
        other.write_text("fake", encoding="utf-8")
        forbidden = {**safe,
            "command": f"Get-Content -LiteralPath '{other}'",
            "action_id": "p1-foreign-inspect"}
        scope = self.agent.preview_tool_scope(forbidden)
        self.assertFalse(scope["allowed"], scope)

        dangerous = {**safe, "command": "Set-Content -Path 'unsafe.txt' -Value x",
                     "action_id": "p1-not-readonly"}
        self.assertFalse(self.agent.preview_tool_scope(dangerous)["allowed"])
        self.assertFalse((self.project / "unsafe.txt").exists())
        self.assertFalse(self.agent.preview_tool_scope(
            {**safe, "operation": "MUTATE", "action_id": "p1-mutate"}
        )["allowed"])
        # A VERIFY action can carry additional checks and therefore must not
        # inherit the INSPECT-only read-CWD exception.
        self.assertFalse(self.agent.preview_tool_scope(
            {**safe, "operation": "VERIFY", "action_id": "p1-verify"}
        )["allowed"])

    def test_proven_inspect_receipt_still_requires_real_process_exit(self):
        command = f"Test-Path -LiteralPath '{self.wheel}'"
        action = {"tool": "run_command", "operation": "INSPECT",
                  "command": command, "action_id": "p1-inspect-exit"}
        evidence = {
            "observation_confirmed": True,
            "working_directory": str(self.project),
            "process_state": "EXITED", "execution_status": "SUCCEEDED",
            "exit_code": 0, "timed_out": False,
        }
        success = encode_result("SUCCEEDED", "COMMAND_EXITED",
                                tool="run_command", executed=True, data=evidence)
        receipt = observation_receipt(action, success,
                                      self._entry(verification_status="UNVERIFIED"))
        self.assertTrue(receipt["admitted"], receipt)
        for changed in (
            {"exit_code": 1}, {"timed_out": True},
            {"observation_confirmed": False}, {"process_state": "RUNNING"},
            {"working_directory": ""},
        ):
            with self.subTest(changed=changed):
                tampered = encode_result(
                    "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
                    data={**evidence, **changed},
                )
                self.assertFalse(observation_receipt(
                    action, tampered, self._entry())["admitted"])
        self.assertFalse(observation_receipt(
            {**action, "command": "python -c \"print('not-proven')\""},
            success, self._entry())["admitted"])

    def test_proven_inspect_dependency_does_not_need_fake_task_pass(self):
        action_id = "p1-read-evidence"
        row = {
            **self._entry(verification_status="UNVERIFIED"),
            "request_id": self.request_id, "task_epoch": self.epoch,
            "action_id": action_id, "tool": "run_command",
            "observation_proof_admitted": True,
        }
        check = {"tool": "list_directory", "depends_on": [action_id]}
        self.assertEqual(
            unsatisfied_dependencies(check, {action_id: row},
                                     self.request_id, self.epoch), [])
        self.assertEqual(
            unsatisfied_dependencies(check, {action_id: {**row,
                "observation_proof_admitted": False}}, self.request_id, self.epoch),
            [action_id])
        self.assertEqual(
            unsatisfied_dependencies(check, {action_id: {**row,
                "evidence_status": "STALE"}}, self.request_id, self.epoch),
            [action_id])

    def test_v9_runtime_persists_real_native_pr_a_satisfied_without_task_pass(self):
        from WebAgent.tests.validate_flexible_progress_admission import initial
        from WebAgent.tests.validate_action_scoped_admission import finished
        from WebAgent.tests.validate_progress_terminal_lifecycle import response
        from agent_core.prerequisite_graph_v3 import RequirementLedger

        seen = []
        action_id = "P1-V9-LIST-PROOF"
        events = []
        def planner(_prompt, _expected, _attachments):
            seen.append(1)
            if len(seen) == 1:
                return response(initial(), {
                    "tool": "list_directory", "action_id": action_id,
                    "path": str(self.wheelhouse),
                })
            return response(
                finished([action_id]),
                {"tool": "final_response", "action_id": "P1-V9-FINAL",
                 "content": "P1_READ_ONLY_PASS"},
            )

        loop = WebAgentProtocolLoop(
            self.workspace, planner, progress_root=self.workspace,
            max_turns=3, event_sink=lambda name, **kwargs: events.append((name, kwargs)),
        )
        with patch.dict("os.environ", {"SMARTAGENT_PREREQ_V3_PR_A": "1"}):
            answer = loop.run(
                "請唯讀列出目錄\n" + str(self.project),
                trusted_authorized_paths=[str(self.project)],
                request_id="RR-BP1-INTEGRATION",
                task_id="TASK-BP1-INTEGRATION",
                task_epoch="EPOCH-BP1-INTEGRATION",
            )
        self.assertEqual(answer, "P1_READ_ONLY_PASS")
        proof = loop.action_result_ledger[action_id]
        self.assertTrue(proof.get("observation_proof_admitted"), proof)
        self.assertEqual(proof["verification_status"], "UNKNOWN")
        self.assertNotEqual(proof.get("effective_verification_status"), "PASS")
        saved = RequirementLedger(self.workspace).read("TASK-BP1-INTEGRATION")
        self.assertEqual(saved["nodes"]["action:" + action_id.lower()]["state"], "SATISFIED")
        self.assertTrue(any(name == "pr_b1_observation_admitted" for name, _ in events))
        self.assertTrue(any(name == "pr_a_evidence_recorded" and
                            payload.get("status") == "SATISFIED"
                            for name, payload in events))

    def _loop(self):
        loop = WebAgentProtocolLoop.__new__(WebAgentProtocolLoop)
        loop.run_id = self.request_id
        loop.task_id = "TASK-BP1"
        loop.task_epoch = self.epoch
        loop.turn_id = 1
        loop.workspace = self.project
        loop.action_ledger = {}
        loop.action_result_ledger = {}
        loop.condition_result_ledger = {}
        loop.pending_verification_requirement = {}
        loop.current_action_repairs = []
        loop._emit = lambda *_args, **_kwargs: None
        return loop

    def test_rejected_or_unstarted_inspect_does_not_stale_prior_reads(self):
        loop = self._loop()
        loop.action_result_ledger["p1-prior"] = {
            "resource_paths": [str(self.project)],
            "evidence_status": "ACTIVE",
            "execution_status": "SUCCEEDED",
            "executed": True,
        }
        action = {"tool": "run_command", "operation": "INSPECT",
                  "command": "python -c \"print('not-proven')\"",
                  "working_directory": str(self.project),
                  "action_id": "p1-rejected"}
        loop.action_ledger["p1-rejected"] = {"result": encode_result(
            "REJECTED", "COMMAND_SCOPE_REJECTED", tool="run_command",
            executed=False,
        )}
        row = self._entry(
            execution_status="FAILED", result_status="REJECTED", executed=False)
        loop._record_action_result_evidence(action, row)
        self.assertEqual(
            loop.action_result_ledger["p1-prior"]["evidence_status"], "ACTIVE")

        # An actually executed unknown-effect command may have mutated files;
        # retaining the old proof would be unsafe even if the process failed.
        action["action_id"] = "p1-unknown-effect"
        loop.action_ledger["p1-unknown-effect"] = {"result": encode_result(
            "FAILED", "COMMAND_EXITED", tool="run_command", executed=True)}
        loop._record_action_result_evidence(
            action, self._entry(execution_status="FAILED",
                                result_status="FAILED", executed=True))
        self.assertEqual(
            loop.action_result_ledger["p1-prior"]["evidence_status"], "STALE")

        # UNKNOWN/indeterminate execution is not a safe no-op assertion.
        loop.action_result_ledger["p1-prior"]["evidence_status"] = "ACTIVE"
        action["action_id"] = "p1-uncertain"
        loop.action_ledger["p1-uncertain"] = {"result": encode_result(
            "UNKNOWN", "PROCESS_EXECUTION_UNCONFIRMED", tool="run_command",
            executed=False)}
        loop._record_action_result_evidence(
            action, self._entry(execution_status="UNKNOWN",
                                result_status="UNKNOWN", executed=False))
        self.assertEqual(
            loop.action_result_ledger["p1-prior"]["evidence_status"], "STALE")

    def test_proven_inspect_never_stales_prior_read_even_on_failed_exit(self):
        loop = self._loop()
        loop.action_result_ledger["p1-prior"] = {
            "resource_paths": [str(self.project)],
            "evidence_status": "ACTIVE",
            "execution_status": "SUCCEEDED",
            "executed": True,
        }
        action = {"tool": "run_command", "operation": "INSPECT",
                  "command": f"Test-Path -LiteralPath '{self.wheel}'",
                  "working_directory": str(self.project),
                  "action_id": "p1-proven-inspect"}
        loop.action_ledger["p1-proven-inspect"] = {"result": encode_result(
            "FAILED", "COMMAND_EXITED", tool="run_command", executed=True)}
        loop._record_action_result_evidence(
            action, self._entry(execution_status="FAILED",
                                result_status="FAILED", executed=True))
        self.assertEqual(
            loop.action_result_ledger["p1-prior"]["evidence_status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
