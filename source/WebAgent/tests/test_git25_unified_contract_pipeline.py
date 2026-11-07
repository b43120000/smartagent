"""Git25: one contract language across Progress, Replan, Prerequisite and Action.

Offline test fixtures only. No network, real deployment or actual deletion.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.canonical_contract_resolver import resolve_core_progress_fields
from agent_core.progress_reply_contract import normalize_progress_fields
from agent_core.unified_contract_transaction import (
    SCHEMA, build_transaction, diagnostic_receipts, render_transaction,
    retain_hold_for_progress_repair,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response


class UnifiedLanguageTests(unittest.TestCase):
    def test_request_goal_anchor_and_no_authorization(self):
        row = build_transaction(
            request_id="RR-G25", task_epoch="EPOCH-25",
            original_goal="Only inspect exact authorized path",
            round_id=4, phase="REJECTED",
            diagnostics=[{"reason": "invalid_progress_payload",
                          "field_issues": [{"field": "evidence_refs"}]}],
            submitted=[{"tool": "report_progress", "action_id": "P1"}],
            pending=[{"tool": "inspect_directory", "action_id": "A1"}],
            recovery_events=[{"status": "HELD_UNADMITTED", "action_ids": ["A1"]}],
            ccrc={"mode": "FIELD_REPAIR"}, evidence_count=0,
        )
        self.assertEqual(row["schema"], SCHEMA)
        self.assertEqual(row["request_id"], "RR-G25")
        self.assertEqual(row["task_epoch"], "EPOCH-25")
        self.assertEqual(len(row["original_goal_sha256"]), 64)
        self.assertNotIn("Only inspect exact authorized path", str(row))
        self.assertEqual(row["issues"][0]["contract"], "PROGRESS")
        self.assertEqual(row["issues"][0]["field_issues"], ["evidence_refs"])
        self.assertEqual(row["issues"][0]["decision"], "NEEDS_INFORMATION")
        self.assertEqual(row["pending_action_ids"], ["A1"])
        self.assertEqual(row["admitted_action_ids"], [])
        self.assertFalse(row["authorization_granted"])
        self.assertFalse(row["result_evidence_created"])
        self.assertIn("SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1",
                      render_transaction(row))

    def test_unexpected_field_differentiates_progress_from_action(self):
        progress = {"tool": "report_progress", "action_id": "P",
                    "current_step": 0, "total_steps": 1,
                    "current_focus": "Inspect", "evidence_ref": "FAKE"}
        action = {"tool": "inspect_directory", "action_id": "A",
                  "paths": ["X"], "permission_override": True}
        cases = [
            (progress, "report_progress", "PROGRESS", ["evidence_ref"]),
            (action, "inspect_directory", "ACTION", ["permission_override"]),
        ]
        for proposal, tool, contract, extras in cases:
            with self.subTest(tool=tool):
                rows = diagnostic_receipts(
                    [{"reason": "unexpected_field"}], submitted=[proposal])
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["tool"], tool)
                self.assertEqual(row["contract"], contract)
                self.assertEqual(row["unexpected_keys"], extras)
                self.assertFalse(row["authorization_granted"])
                self.assertFalse(row["executed"])

    def test_authorization_reject_replan_and_evidence_have_separate_routes(self):
        cases = [
            ("TOOL_SCOPE_REJECTED", "AUTHORIZATION", "DENIED"),
            ("USER_APPROVAL_REQUIRED", "AUTHORIZATION", "REQUIRES_APPROVAL"),
            ("semantic_path_planner_selection_mismatch", "REPLAN", "REJECT_REPLAN"),
            ("runtime_evidence_ref_unknown", "EVIDENCE", "NEEDS_INFORMATION"),
            ("missing_context_requires_runtime_read", "PREREQUISITE",
             "NEEDS_INFORMATION"),
        ]
        for reason, contract, route in cases:
            with self.subTest(reason=reason):
                row = diagnostic_receipts([{"reason": reason}], submitted=[])[0]
                self.assertEqual((row["contract"], row["decision"]),
                                 (contract, route))

    def test_case_d_round6_progress_error_keeps_hold_but_action_error_clears(self):
        progress = {"tool": "report_progress", "action_id": "P",
                    "current_step": 0, "total_steps": 5,
                    "current_focus": "Discover", "evidence_ref": "UNTRUSTED"}
        held = {"tool": "inspect_directory", "action_id": "ACT-D4-TARGET",
                "paths": ["X"]}
        progress_diagnostic = [{"reason": "unexpected_field",
                                "tool": "report_progress"}]
        self.assertTrue(retain_hold_for_progress_repair(
            progress_diagnostic, submitted=[progress, held]))
        self.assertTrue(retain_hold_for_progress_repair(
            [{"reason": "invalid_progress_payload"}],
            submitted=[progress, held]))
        unsafe = {**held, "allow_outside_scope": True}
        self.assertFalse(retain_hold_for_progress_repair(
            [{"reason": "unexpected_field"}], submitted=[progress, unsafe]))
        self.assertFalse(retain_hold_for_progress_repair(
            [{"reason": "TOOL_SCOPE_REJECTED", "tool": "inspect_directory"}],
            submitted=[progress, held]))
        self.assertFalse(retain_hold_for_progress_repair(
            [{"reason": "unexpected_field"}],
            submitted=[progress, held]))

    def test_core_progress_exact_alias_is_safe_but_no_forged_evidence(self):
        planner = {
            "tool": "report_progress",
            "matchedCondition": "PENDING",
            "evidenceRefs": ["REQUEST_ACCEPTED"],
            "nextAction": "inspect_directory",
        }
        canonical, receipt = resolve_core_progress_fields(planner)
        self.assertEqual(receipt["decision"], "REWRITE_SAFE_EQUIVALENT")
        self.assertEqual(canonical["matched_condition"], "PENDING")
        self.assertEqual(canonical["evidence_refs"], ["REQUEST_ACCEPTED"])
        self.assertEqual(canonical["next_action"], "inspect_directory")
        self.assertNotIn("evidenceRefs", canonical)
        self.assertEqual(normalize_progress_fields(planner)["evidence_refs"],
                         ["REQUEST_ACCEPTED"])
        self.assertNotIn("decision", canonical)
        self.assertNotIn("outcome", canonical)
        self.assertNotIn("completion_contract", canonical)
        with self.assertRaisesRegex(ValueError, "conflicting_progress_alias"):
            resolve_core_progress_fields({
                "evidenceRefs": ["REQUEST_ACCEPTED"],
                "evidence_refs": ["FORGED_ACTION"],
            })

    def test_pending_is_not_execution_and_admission_not_result_evidence(self):
        row = build_transaction(
            request_id="R", task_epoch="E", original_goal="goal",
            round_id=2, phase="ADMITTED",
            submitted=[{"tool": "report_progress", "action_id": "P"}],
            accepted=[{"tool": "report_progress", "action_id": "P"},
                      {"tool": "inspect_directory", "action_id": "A"}],
            recovery_events=[{"status": "RESTORED_FOR_FULL_ADMISSION",
                              "action_ids": ["A"]}],
        )
        self.assertEqual(row["admitted_action_ids"], ["A"])
        self.assertEqual(row["actual_execution_status"],
                         "NOT_EXECUTED_BY_THIS_CONTRACT")
        self.assertFalse(row["result_evidence_created"])
        self.assertFalse(row["authorization_granted"])


class IntegratedUnifiedPipelineTests(unittest.TestCase):
    def test_real_loop_canonical_alias_executes_read_once(self):
        with tempfile.TemporaryDirectory(prefix="git25-unified-") as temp:
            root = Path(temp).resolve()
            (root / "proof.txt").write_text("evidence", encoding="utf-8")
            rounds, executed = [], []
            def planner(prompt, expected, attachments):
                rounds.append(prompt)
                if len(rounds) == 1:
                    p = dict(initial())
                    p["matchedCondition"] = p.pop("matched_condition")
                    p["evidenceRefs"] = p.pop("evidence_refs")
                    p["currentFocus"] = p.pop("current_focus")
                    return response(p, {"tool": "list_directory",
                                        "action_id": "ACT-READ",
                                        "path": str(root)})
                self.assertIn("unified_contract_transaction", prompt)
                self.assertIn("proof.txt", prompt)
                p = dict(initial())
                p.update(decision="COMPLETE", outcome="SUCCESS",
                         current_step=1,
                         steps=[{"step": 1, "desc": "讀取目錄",
                                 "status": "COMPLETED"}],
                         matched_condition="目錄資料已讀取",
                         evidence_refs=["ACT-READ"])
                p.pop("next_action", None)
                return response(p, {"tool": "final_response",
                                    "action_id": "G25-TERMINAL",
                                    "content": "GIT25_UNIFIED_PASS"})
            loop = WebAgentProtocolLoop(root, planner, progress_root=root)
            original_execute = loop._execute_action
            def track(action):
                executed.append(action["action_id"])
                return original_execute(action)
            loop._execute_action = track
            result = loop.run("只讀取目錄資訊\n" + str(root),
                              trusted_authorized_paths=[str(root)])
            self.assertEqual(result, "GIT25_UNIFIED_PASS")
            self.assertEqual(executed, ["ACT-READ"])
            self.assertEqual(len(rounds), 2)
            self.assertEqual(loop._last_unified_contract_receipt["phase"], "ADMITTED")

    def test_held_action_is_rejected_across_task_epoch(self):
        with tempfile.TemporaryDirectory(prefix="git25-epoch-") as temp:
            root = Path(temp).resolve()
            loop = WebAgentProtocolLoop(root, lambda *x: "", progress_root=root)
            loop.run_id = loop.task_id = "G25-EPOCH"
            loop.task_epoch = "EPOCH-A"
            loop.pending_progress_repair_actions = [{
                "tool": "list_directory", "action_id": "OLD-ACT",
                "path": str(root)}]
            loop._pending_progress_repair_binding = {
                "request_id": loop.run_id, "task_epoch": "EPOCH-A"}
            loop.task_epoch = "EPOCH-B"
            calls = [dict(initial()), {"tool": "turn_commit",
                                        "action_count": 1}]
            restored = loop._restore_progress_repair_actions(calls)
            self.assertEqual(restored, calls)
            self.assertFalse(loop.pending_progress_repair_actions)
            self.assertEqual(
                loop._unified_action_recovery_events[-1]["status"],
                "REQUEST_EPOCH_MISMATCH_REJECTED")

    def test_bad_progress_holds_and_restores_exact_action_without_dispatch(self):
        from agent_core.task_progress import initialize_progress
        with tempfile.TemporaryDirectory(prefix="git25-held-") as temp:
            root = Path(temp).resolve()
            loop = WebAgentProtocolLoop(root, lambda *x: "", progress_root=root)
            loop.run_id = loop.task_id = loop.task_epoch = "G25-HOLD"
            loop.intent_digest = "f" * 64
            loop.progress_ledger = initialize_progress(
                loop.task_id, request_id=loop.run_id,
                goal="Inspect scoped directory", root=root,
            )
            loop.authorized_paths = [str(root)]
            loop.tools._authorized_local_paths = [str(root)]
            action = {"tool": "list_directory", "action_id": "HELD-G25",
                      "path": str(root)}
            progress = dict(initial())
            progress["evidence_refs"] = None  # Explicit invalid is not absent.
            raw = [progress, action, {"tool": "turn_commit",
                                      "action_count": 2}]
            admitted, errors = loop._accept_v8_ack(raw, {})
            self.assertFalse(admitted)
            self.assertTrue(errors)
            self.assertEqual([x["action_id"] for x in
                              loop.pending_progress_repair_actions], ["HELD-G25"])
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop._unified_action_recovery_events[-1]["status"],
                             "HELD_UNADMITTED")
            repaired = [dict(initial()), {"tool": "turn_commit",
                                         "action_count": 1}]
            restored = loop._restore_progress_repair_actions(repaired)
            self.assertEqual([x["tool"] for x in restored],
                             ["report_progress", "list_directory", "turn_commit"])
            self.assertEqual(restored[1], action)
            self.assertEqual(restored[-1]["action_count"], 2)
            self.assertEqual(loop._unified_action_recovery_events[-1]["status"],
                             "RESTORED_FOR_FULL_ADMISSION")
            admitted, errors = loop._accept_v8_ack(restored, {})
            self.assertFalse(errors, errors)
            self.assertEqual([x["action_id"] for x in admitted
                              if x["tool"] == "list_directory"], ["HELD-G25"])
            # Admission does not dispatch: no real Action Result is created.
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.action_ledger, {})


if __name__ == "__main__":
    unittest.main()
