#!/usr/bin/env python3
"""GAST V1: Goal anchor, trusted prerequisite evidence and bounded replay IT."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.goal_strategy_transition import (
    SCHEMA, GoalTransitionError, build_transition_readiness,
    classify_runtime_evidence, goal_digest, repeat_denied_read_diagnostic,
    validate_planner_transition,
)
from agent_core.task_progress import (
    _atomic_write, condition_id_for, get_ledger_path, initialize_progress,
    read_progress, record_planner_goal_transition, record_runtime_strategy_revision,
)
from agent_core.tool_result_contract import attach_runtime_result


class GoalTransitionIT(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="goal-transition-it-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.goal = "Acquire trusted package and deploy only to approved test scope"
        self.task = "TASK-GOAL-TX-IT"
        self.request = "RR-GOAL-TX-IT"
        self.epoch = "EPOCH-GOAL-TX-IT"
        self.contract = {
            "success": ["Package downloaded and verified under authorized scope"],
            "failure": ["Verified implementation failure"],
            "in_progress": ["Still acquiring prerequisites"],
            "interrupted": ["No safe authorized route"],
        }
        self.cid = condition_id_for("success", self.contract["success"][0])
        self.original = {
            "tool": "list_directory", "action_id": "ACT-READ-DENIED",
        }
        self.action_ledger = {
            "ACT-READ-DENIED": {
                "action": self.original,
                "result": "[TOOL_SCOPE_REJECTED] explicit_path_scope_requires_path",
            },
        }
        self.evidence = {
            "ACT-READ-DENIED": {
                "tool": "list_directory",
                "executed": False,
                "result_status": "REJECTED",
                "verification_status": "UNVERIFIED",
                "effective_verification_status": "UNVERIFIED",
                "evidence_status": "ACTIVE",
                "request_id": self.request,
                "task_epoch": self.epoch,
            },
        }
        self.fixed = {"tool": "list_directory", "action_id": "ACT-FIXED",
                      "path": str(self.root)}
        self.proposal = {
            "schema": SCHEMA, "kind": "ACTION_REPAIR",
            "goal_anchor_sha256": goal_digest(self.goal),
            "goal_condition_id": self.cid,
            "evidence_ref": "ACT-READ-DENIED",
            "blocked_action_id": "ACT-READ-DENIED",
            "next_tool": "list_directory",
            "rationale": "Repair the missing path field; preserve the original objective.",
        }

    def check(self, proposal):
        return validate_planner_transition(
            proposal, goal=self.goal, contract=self.contract,
            evidence=self.evidence, action_ledger=self.action_ledger,
            operational=[self.fixed], request_id=self.request,
            task_epoch=self.epoch,
        )

    def must_reject(self, proposal, code):
        with self.assertRaisesRegex(GoalTransitionError, code):
            self.check(proposal)

    def test_preexecution_denial_requires_field_repair_not_absence_claim(self):
        fact = classify_runtime_evidence(
            self.evidence["ACT-READ-DENIED"], self.action_ledger["ACT-READ-DENIED"])
        self.assertEqual(fact["eligible_transition"], "ACTION_REPAIR")
        self.assertEqual(fact["required_field"], "path")
        self.assertFalse(fact["proves_missing_prerequisite"])
        admitted = self.check(self.proposal)
        self.assertFalse(admitted["authorization_granted"])
        self.assertFalse(admitted["completion_verified"])
        self.must_reject({**self.proposal, "kind": "PREREQUISITE_RESOLUTION"},
                         "goal_transition_prerequisite_not_verified")
        self.must_reject({**self.proposal, "kind": "STRATEGY_REPLAN"},
                         "goal_transition_strategy_not_evidence_supported")

    def test_goal_condition_evidence_and_tool_identity_are_required(self):
        self.must_reject({**self.proposal, "goal_anchor_sha256": "0" * 64},
                         "goal_transition_goal_anchor_mismatch")
        self.must_reject({**self.proposal, "goal_condition_id": "COND-UNDECLARED"},
                         "goal_transition_condition_not_declared")
        self.must_reject({**self.proposal, "evidence_ref": "FAKE"},
                         "goal_transition_evidence_binding_invalid")
        self.must_reject({**self.proposal, "next_tool": "run_command"},
                         "goal_transition_next_action_not_submitted")
        self.must_reject({**self.proposal, "unknown": "permission"},
                         "goal_transition_schema_invalid")
        self.evidence["ACT-READ-DENIED"]["task_epoch"] = "WRONG"
        self.must_reject(self.proposal, "goal_transition_evidence_identity_mismatch")

    def test_wire_contract_accepts_optional_goal_transition(self):
        from agent_core.smartagent_protocol import validate_tool_envelope
        valid, diagnostic = validate_tool_envelope({
            "tool": "report_progress", "action_id": "P-WIRE",
            "current_step": 1, "total_steps": 2,
            "current_focus": "Repair the path field",
            "goal_transition": self.proposal,
        })
        self.assertTrue(valid, diagnostic)

    def test_negative_tool_error_does_not_infer_absence(self):
        case = {
            **self.evidence["ACT-READ-DENIED"],
            "tool": "inspect_directory", "executed": True,
            "result_status": "FAILED", "result_reason": "DIRECTORY_NOT_FOUND",
            "effective_verification_status": "UNVERIFIED",
        }
        item = classify_runtime_evidence(case)
        self.assertEqual(item["eligible_transition"], "EVIDENCE_ACQUISITION")
        self.assertFalse(item["proves_missing_prerequisite"])
        case["effective_verification_status"] = "FAIL"
        item = classify_runtime_evidence(case)
        self.assertEqual(item["eligible_transition"], "STRATEGY_REPLAN")
        self.assertFalse(item["proves_missing_prerequisite"])

    def test_only_trusted_confirmed_stat_negative_supports_prerequisite(self):
        target = {"tool": "observe_fact", "action_id": "FACT-ABSENT",
                  "path": str(self.root / "target")}
        raw = attach_runtime_result(
            '{"schema":"SMARTAGENT_FACT_OBSERVATION_RESULT_V1"}',
            "SUCCEEDED", "FACT_OBSERVED", tool="observe_fact", executed=True,
            action_id="FACT-ABSENT", data={
                "observations": [{
                    "schema": "SMARTAGENT_EVIDENCE_V1", "field": "file.exists",
                    "source": "filesystem.stat", "freshness": "CURRENT",
                    "raw_evidence_ref": "FACT-ABSENT",
                    "observation_status": "CONFIRMED", "value": False,
                }],
            },
        )
        self.action_ledger["FACT-ABSENT"] = {"action": target, "result": raw}
        self.evidence["FACT-ABSENT"] = {
            "tool": "observe_fact", "executed": True,
            "result_status": "SUCCEEDED", "result_reason": "FACT_OBSERVED",
            "effective_verification_status": "UNVERIFIED",
            "request_id": self.request, "task_epoch": self.epoch,
            "evidence_status": "ACTIVE",
        }
        fact = classify_runtime_evidence(
            self.evidence["FACT-ABSENT"], self.action_ledger["FACT-ABSENT"])
        self.assertEqual(fact["classification"], "VERIFIED_NEGATIVE_FACT")
        self.assertTrue(fact["proves_missing_prerequisite"])
        self.assertFalse(fact["authorizes_execution"])
        proposal = {
            **self.proposal,
            "kind": "PREREQUISITE_RESOLUTION",
            "evidence_ref": "FACT-ABSENT",
            "blocked_action_id": "FACT-ABSENT",
        }
        accepted = self.check(proposal)
        self.assertEqual(accepted["evidence_classification"], "VERIFIED_NEGATIVE_FACT")

    def test_identical_denied_read_blocked_but_field_repair_allowed(self):
        same = {"tool": "list_directory", "action_id": "ACT-NEW-ID"}
        blocked = repeat_denied_read_diagnostic(
            [same], evidence=self.evidence, action_ledger=self.action_ledger)
        self.assertEqual(blocked["reason"],
                         "goal_alignment_identical_denied_read_without_new_evidence")
        self.assertIsNone(repeat_denied_read_diagnostic(
            [self.fixed], evidence=self.evidence, action_ledger=self.action_ledger))
        # Any subsequent committed independent evidence lifts the replay
        # duplicate flag, but normal Tool Admission still applies.
        self.evidence["ACT-INDEPENDENT"] = {
            "tool": "observe_fact", "executed": True,
            "result_status": "SUCCEEDED", "evidence_status": "ACTIVE",
        }
        self.assertIsNone(repeat_denied_read_diagnostic(
            [same], evidence=self.evidence, action_ledger=self.action_ledger))

    def test_ledger_keeps_original_goal_and_runtime_revision(self):
        original = initialize_progress(
            self.task, request_id=self.request, goal=self.goal, root=self.root)
        original.completion_contract = self.contract
        original.strategy_state = {"revision": 3, "history": [{"revision": 3}]}
        _atomic_write(get_ledger_path(self.task, self.root), original.to_dict())
        decision = self.check(self.proposal)
        updated = record_planner_goal_transition(
            self.task, request_id=self.request, decision=decision,
            round_id=4, root=self.root)
        self.assertEqual(updated.goal, original.goal)
        self.assertEqual(updated.completion_contract, original.completion_contract)
        self.assertEqual(updated.strategy_state["revision"], 3)
        self.assertEqual(updated.strategy_state["planner_transition_count"], 1)
        persisted = read_progress(self.task, self.root)
        self.assertEqual(
            persisted.strategy_state["planner_transition_history"][0]["kind"],
            "ACTION_REPAIR",
        )
        # A separate Runtime-attested Replan must not erase Planner's
        # previously accepted Goal/evidence dependency.
        evidence_receipt = {
            "schema": "SMARTAGENT_PREEXECUTION_REPLAN_V1",
            "request_id": self.request, "scope_verified": True,
            "authorization_granted": False, "executed": False,
            "evidence_digest": "a" * 64,
            "rejected_action_id": "ACT-BLOCKED",
            "target": str(self.root / "missing"),
            "existing_writable_parent": str(self.root),
        }
        revised, changed = record_runtime_strategy_revision(
            self.task, request_id=self.request, evidence=evidence_receipt,
            round_id=5, root=self.root)
        self.assertTrue(changed)
        self.assertEqual(revised.strategy_state["revision"], 4)
        self.assertEqual(len(revised.strategy_state["planner_transition_history"]), 1)
        self.assertEqual(revised.goal, self.goal)

    def test_readiness_preserves_goal_anchor_and_no_tool_grants(self):
        class Runtime:
            progress_ledger = None
            task_request = ""
            action_result_ledger = {}
            action_ledger = {}
            condition_result_ledger = {}
        r = Runtime()
        r.progress_ledger = type("Progress", (), {
            "goal": self.goal, "completion_contract": self.contract,
        })()
        r.action_result_ledger = self.evidence
        r.action_ledger = self.action_ledger
        state = build_transition_readiness(r)
        self.assertEqual(state["goal_anchor_sha256"], goal_digest(self.goal))
        self.assertEqual(state["latest_blocker"]["eligible_transition"], "ACTION_REPAIR")
        self.assertEqual(len(state["unresolved_success_conditions"]), 1)
        self.assertTrue(state["runtime_owns_evidence_and_admission"])
        r.action_result_ledger = {
            **self.evidence,
            "ACT-VERIFIED-LATER": {
                "tool": "list_directory", "result_status": "SUCCEEDED",
                "effective_verification_status": "PASS", "executed": True,
                "evidence_status": "ACTIVE",
            },
        }
        later = build_transition_readiness(r)
        self.assertEqual(later["latest_blocker"], {})
        self.assertEqual(len(later["recent_blockers"]), 1)


class GoalTransitionLiveIT(unittest.TestCase):
    def test_denied_read_then_goal_linked_field_repair_executes_once(self):
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        from WebAgent.tests.validate_progress_terminal_lifecycle import response
        from agent_core.semantic_capability_resolution import SCHEMA as SCR_SCHEMA
        with tempfile.TemporaryDirectory(prefix="goal-transition-e2e-") as location:
            root = Path(location).resolve()
            goal = '唯讀列出 "' + str(root) + '" 的檔案'
            success = "The requested folder has been successfully listed"
            in_progress = "Directory listing not yet performed"
            condition = condition_id_for("success", success)
            rounds, events = [], []
            def progress(action_id, refs):
                return {
                    "tool": "report_progress", "action_id": action_id,
                    "base_evaluation": "The authorized directory already exists",
                    "total_steps": 2, "current_step": 1,
                    "steps": [
                        {"step": 1, "desc": "List the directory", "status": "IN_PROGRESS"},
                        {"step": 2, "desc": "Check the listing", "status": "PENDING"},
                    ],
                    "current_focus": "List target path",
                    "next_action": "list_directory",
                    "completion_contract": {
                        "success": [success], "failure": ["Read action failed"],
                        "in_progress": [in_progress],
                        "interrupted": ["No authorized observation route"],
                    },
                    "decision": "CONTINUE", "outcome": "PENDING",
                    "matched_condition": in_progress,
                    "evidence_refs": refs,
                    "decision_reason": "Need an authorized directory listing.",
                }
            def planner(prompt, expected, attachments):
                rounds.append(prompt)
                if len(rounds) == 1:
                    p = progress("P-MISSING", ["REQUEST_ACCEPTED"])
                    p["capability_resolution"] = {
                        "schema": SCR_SCHEMA, "capabilities": ["directory.list"]}
                    return response(p, {"tool": "list_directory",
                                        "action_id": "ACT-MISSING"})
                if len(rounds) == 2:
                    self.assertIn("goal_transition_readiness", prompt)
                    self.assertIn("ACTION_REPAIR", prompt)
                    p = progress("P-FIXED", ["ACT-MISSING"])
                    p["goal_transition"] = {
                        "schema": SCHEMA, "kind": "ACTION_REPAIR",
                        "goal_anchor_sha256": goal_digest(goal),
                        "goal_condition_id": condition,
                        "evidence_ref": "ACT-MISSING",
                        "blocked_action_id": "ACT-MISSING",
                        "next_tool": "list_directory",
                        "rationale": "The prior action was denied before execution; add its required path.",
                    }
                    return response(p, {"tool": "list_directory",
                                        "action_id": "ACT-FIXED", "path": str(root)})
                raise RuntimeError("STOP_AFTER_GOAL_ALIGNED_REPAIR")
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=6,
                event_sink=lambda name, **kw: events.append((name, kw)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "0", "SMARTAGENT_PREREQ_V3_BCD": "0",
                "SMARTAGENT_PREREQ_V3_PR_A": "0",
            }):
                with self.assertRaisesRegex(RuntimeError, "STOP_AFTER_GOAL_ALIGNED_REPAIR"):
                    loop.run(goal, request_id="RR-GOAL-E2E",
                             task_id="TASK-GOAL-E2E", task_epoch="EPOCH-GOAL-E2E",
                             trusted_authorized_paths=[str(root)])
            self.assertEqual(len(rounds), 3)
            goals = [e for e in events if e[0] == "goal_transition_accepted"]
            self.assertEqual(len(goals), 1)
            self.assertFalse(goals[0][1]["authorization_granted"])
            started = [e for e in events if e[0] == "tool_started"]
            self.assertTrue(any(e[1].get("action_id") == "ACT-FIXED" for e in started))
            ledger = read_progress("TASK-GOAL-E2E", root)
            self.assertEqual(ledger.goal, goal)
            self.assertEqual(
                ledger.strategy_state["planner_transition_history"][-1]["goal_condition_id"],
                condition,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
