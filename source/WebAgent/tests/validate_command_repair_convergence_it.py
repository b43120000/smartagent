#!/usr/bin/env python3
"""IT: Command/Progress repair convergence at the Action Validation boundary.

No command is ever executed. Test only pure readiness and _accept_ack
admission orchestration using a no-execution fake admission gate.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent_core.command_repair_readiness import (
    ACTION_RECONSTRUCTION, FIELD_REPAIR, READY_FOR_ADMISSION,
    analyze_command_repair, repair_progress,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.protocol_loop import WebAgentProtocolLoop


VALID_VERIFY = {
    "action": "file_exists", "path": "E:/authorized/staged/output.json",
}
INLINE_SCRIPT = (
    'python -c "import sys; values=[1,2,3]; '
    'total=sum(values); print(total)"'
)


def action(**updates):
    item = {
        "tool": "run_command", "action_id": "D9-SCRIPT-EXEC",
        "command": "Write-Output READY",
        "operation": "TRANSFER",
        "verify": dict(VALID_VERIFY),
        "success_criteria": "The staged fixture is verified",
    }
    item.update(updates)
    return item


class FakeAdmission:
    def __init__(self):
        self.run_id = "RR-D9"
        self.task_epoch = "EPOCH-D9"
        self.turn_id = 1
        self.action_ledger = {}
        self.action_result_ledger = {}
        self.deferred_action_repairs = {}
        self.current_action_repairs = []
        self.pending_command_field_repairs = {}
        self.events = []
        self.batch_calls = 0

    def _emit(self, name, **kwargs):
        self.events.append((name, kwargs))

    def _accept_action_batch(self, calls, _expected):
        self.batch_calls += 1
        proposed = [call for call in calls if call.get("tool") == "run_command"]
        if not proposed:
            return [], [{"reason": "invalid_progress_payload"}]
        selected = proposed[0]
        valid, error = validate_tool_envelope(selected)
        if not valid:
            return [], [error]
        if selected.get("operation") == "PYTHON_EXEC":
            return [], [{
                "reason": "run_command_verification_contract_required",
                "tool": "run_command",
            }]
        issues = analyze_command_repair(selected)
        if issues["mode"] != READY_FOR_ADMISSION:
            return [], [{
                "reason": "run_command_verification_contract_required",
                "tool": "run_command",
            }]
        return [selected], []


class CommandRepairReadinessIT(unittest.TestCase):
    def test_invalid_operation_and_conditional_verifier_reported_together(self):
        candidate = action(operation="PYTHON_EXEC")
        candidate.pop("verify")
        candidate.pop("success_criteria")
        diagnostic = analyze_command_repair(candidate)
        self.assertEqual(diagnostic["mode"], FIELD_REPAIR)
        fields = {row["field"] for row in diagnostic["field_issues"]}
        self.assertEqual(fields, {"operation", "verify", "success_criteria"})
        self.assertGreaterEqual(diagnostic["issue_count"], 3)
        self.assertFalse(diagnostic["authorization_granted"])
        self.assertNotIn(candidate["command"], repr(diagnostic))

    def test_inline_script_is_reconstruction_not_verify_patch(self):
        candidate = action(command=INLINE_SCRIPT)
        diagnostic = analyze_command_repair(candidate)
        self.assertEqual(diagnostic["mode"], ACTION_RECONSTRUCTION)
        self.assertTrue(any(item["field"] == "command"
                            and item["code"] == "inline_script_too_complex"
                            for item in diagnostic["field_issues"]))

    def test_verify_inline_script_is_field_repair(self):
        candidate = action(verify={
            "action": "run_command", "command": INLINE_SCRIPT,
            "expect_exit_code": 0,
        })
        diagnostic = analyze_command_repair(candidate)
        self.assertEqual(diagnostic["mode"], FIELD_REPAIR)
        self.assertIn("verify", diagnostic["repairable_fields"])
        self.assertNotIn("command", [row["field"] for row in diagnostic["field_issues"]])

    def test_invalid_command_and_scope_never_grant_authority(self):
        result = analyze_command_repair(action(command="Remove-Item -Recurse E:/authorized/staged"))
        self.assertEqual(result["mode"], ACTION_RECONSTRUCTION)
        self.assertFalse(result["authorization_granted"])
        self.assertFalse(result["execution_inferred"])
        self.assertTrue(result["full_admission_required"])
        result2 = analyze_command_repair(action(extra_unrecognized="UNTRUSTED"))
        self.assertEqual(result2["mode"], ACTION_RECONSTRUCTION)
        self.assertNotIn("UNTRUSTED", repr(result2))

    def test_all_independent_fields_resolved_before_acceptance(self):
        broken = action(operation="PYTHON_EXEC")
        broken.pop("verify")
        broken.pop("success_criteria")
        before = analyze_command_repair(broken)
        valid = analyze_command_repair(action())
        self.assertEqual(valid["mode"], READY_FOR_ADMISSION, valid)
        convergence = repair_progress(before, valid)
        self.assertTrue(convergence["converging"])
        self.assertEqual(convergence["resolved_issue_count"], before["issue_count"])
        self.assertEqual(convergence["current_issue_count"], 0)
        self.assertFalse(convergence["authorization_granted"])
        self.assertFalse(convergence["budget_reset"])

    def test_pending_patch_preserves_original_command_and_is_revalidated(self):
        host = FakeAdmission()
        initial = action(operation="PYTHON_EXEC")
        initial.pop("verify")
        initial.pop("success_criteria")
        admitted, rejected = WebAgentProtocolLoop._accept_ack(host, [initial], {})
        self.assertEqual(admitted, [])
        self.assertTrue(rejected)
        self.assertIn("D9-SCRIPT-EXEC", host.pending_command_field_repairs)
        hint = rejected[0]["command_repair"]
        self.assertEqual(hint["mode"], FIELD_REPAIR)
        self.assertEqual(set(hint["required_fields"]),
                         {"operation", "verify", "success_criteria"})
        self.assertEqual(host.batch_calls, 1)
        self.assertFalse(rejected[0]["command_repair"]["authorization_granted"])
        host.turn_id += 1
        repaired = {
            "tool": "run_command", "action_id": "D9-SCRIPT-EXEC",
            "operation": "TRANSFER", "verify": dict(VALID_VERIFY),
            "success_criteria": "The staged fixture is verified",
        }
        admitted, rejected = WebAgentProtocolLoop._accept_ack(host, [repaired], {})
        self.assertFalse(rejected, rejected)
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]["command"], initial["command"])
        self.assertEqual(admitted[0]["operation"], "TRANSFER")
        self.assertEqual(host.batch_calls, 2)
        self.assertEqual(host.pending_command_field_repairs, {})
        emitted = [row for row in host.events if row[0] == "command_repair_converged"]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][1]["current_issue_count"], 0)
        self.assertFalse(emitted[0][1]["executed"])

    def test_identity_change_rejected_without_execution(self):
        host = FakeAdmission()
        initial = action(operation="PYTHON_EXEC")
        initial.pop("verify")
        initial.pop("success_criteria")
        WebAgentProtocolLoop._accept_ack(host, [initial], {})
        changed = {
            "tool": "run_command", "action_id": "D9-SCRIPT-EXEC",
            "command": "Write-Output DIFFERENT", "operation": "TRANSFER",
        }
        admitted, diagnostic = WebAgentProtocolLoop._accept_ack(host, [changed], {})
        self.assertFalse(admitted)
        self.assertEqual(diagnostic[0]["reason"], "command_repair_identity_changed")
        self.assertEqual(host.batch_calls, 1)

    def test_stagnant_repair_escalates_to_reconstruction(self):
        host = FakeAdmission()
        original = action(operation="PYTHON_EXEC")
        original.pop("verify")
        original.pop("success_criteria")
        WebAgentProtocolLoop._accept_ack(host, [original], {})
        host.turn_id += 1
        WebAgentProtocolLoop._accept_ack(host, [
            {"tool": "run_command", "action_id": "D9-SCRIPT-EXEC"},
        ], {})
        host.turn_id += 1
        _, rejected = WebAgentProtocolLoop._accept_ack(host, [
            {"tool": "run_command", "action_id": "D9-SCRIPT-EXEC"},
        ], {})
        self.assertEqual(rejected[0]["command_repair"]["mode"], ACTION_RECONSTRUCTION)
        self.assertEqual(host.pending_command_field_repairs, {})
        self.assertEqual(host.deferred_action_repairs["D9-SCRIPT-EXEC"]["status"],
                         "ACTION_RECONSTRUCTION_REQUIRED")

    def test_progress_reject_includes_action_readiness_without_executing(self):
        host = FakeAdmission()
        def reject_progress(calls, expected):
            return [], [{"reason": "invalid_progress_payload",
                         "field_issues": [{"field": "evidence_refs"}]}]
        host._accept_action_batch = reject_progress
        broken = action(operation="PYTHON_EXEC")
        broken.pop("verify")
        broken.pop("success_criteria")
        _, diagnostic = WebAgentProtocolLoop._accept_ack(host, [broken], {})
        self.assertEqual(diagnostic[0]["reason"], "invalid_progress_payload")
        self.assertEqual(diagnostic[0]["command_repair"]["mode"], FIELD_REPAIR)
        self.assertTrue(host.pending_command_field_repairs)
        self.assertFalse(any(name == "tool_started" for name, _ in host.events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
