#!/usr/bin/env python3
"""P0-P3 protocol repair transaction regressions on real WebAgent admission.

Only local temporary projects are accessed; no network or external process
execution. The round-4-to-8 scenario is an RCA-derived SYNTHETIC replay:
the sanitized RCA does not contain raw Planner replies.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from agent_core.bounded_repair import BoundedRepairBudget
from agent_core.cross_contract_repair import (
    CrossContractRepairCoordinator, FIELD_REPAIR, inspect_cross_contract,
    repair_instruction,
)
from agent_core.protocol_repair_transaction import (
    ProtocolRepairTransaction, normalize_query_project,
)
from agent_core.progress_reply_contract import (
    materialize_progress, reconcile_redundant_patch_fields,
)
from agent_core.task_progress import (
    initialize_progress, record_model_progress, read_progress,
    validate_model_progress,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def contract():
    return {
        "success": ["實際讀取並驗證資料"],
        "failure": ["實際來源無法讀取"],
        "in_progress": ["需要讀取來源"],
        "interrupted": ["目前無法取得合法資料"],
    }


def progress(*, current=0, current_focus="先觀測來源",
             next_action="list_directory", action_id="P-1"):
    return {
        "tool": "report_progress", "action_id": action_id,
        "progress_update": "FULL", "decision": "CONTINUE", "outcome": "PENDING",
        "base_evaluation": "依據 Runtime 真實證據逐步驗證",
        "total_steps": 2, "current_step": current,
        "steps": [
            {"step": 1, "desc": "觀測", "status": "COMPLETED" if current else "IN_PROGRESS"},
            {"step": 2, "desc": "查詢並驗證", "status": "IN_PROGRESS" if current else "PENDING"},
        ],
        "current_focus": current_focus, "next_action": next_action,
        "completion_contract": contract(),
        "matched_condition": "需要讀取來源",
        "evidence_refs": ["A-LIST"] if current else ["REQUEST_ACCEPTED"],
        "decision_reason": "只使用目前 Request 的 Runtime 觀測",
    }


def query_project(action_id="A-QUERY", queries=None, **extra):
    return {
        "tool": "query_project", "action_id": action_id,
        "queries": queries if queries is not None
        else [{"operation": "search_text", "query": "probe"}],
        **extra,
    }


class ProtocolRepairTransactionIT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="protocol-tx-it-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "source.txt").write_text(
            "probe\nfixture data\n", encoding="utf-8")
        self.ledger = initialize_progress(
            "TASK-TX", request_id="RR-TX",
            goal="Local read only", root=self.root)
        self.ledger = record_model_progress(
            "TASK-TX", progress(), request_id="RR-TX",
            goal="Local read only", round_id=1, root=self.root)
        self.ledger = record_model_progress(
            "TASK-TX", progress(current=1, next_action="query_project",
                                action_id="P-2"),
            request_id="RR-TX", goal="Local read only",
            round_id=2, root=self.root)
        self.tx = ProtocolRepairTransaction("RR-TX", "EP-TX")
        self.valid = progress(current=1, next_action="query_project",
                              action_id="P-Q")
        self.action = query_project(queries=[{"query": "probe"}])

    def capture(self):
        return self.tx.capture_query_rejection(
            self.valid, [self.action], ledger=self.ledger,
            action_results={"A-LIST": {"status": "SUCCEEDED"}},
            request_id="RR-TX", task_epoch="EP-TX")

    def restore(self, proposed, actions=None, ledger=None, evidence=None,
                request_id="RR-TX", task_epoch="EP-TX"):
        return self.tx.restore_progress(
            proposed, actions if actions is not None else [self.action],
            ledger=ledger if ledger is not None else self.ledger,
            action_results=evidence if evidence is not None else {
                "A-LIST": {"status": "SUCCEEDED"}},
            request_id=request_id, task_epoch=task_epoch)

    def test_query_mapping_to_list_requires_explicit_operation(self):
        p = query_project(queries={"operation": "search_text", "query": "probe"})
        q, reason = normalize_query_project(p)
        self.assertEqual(reason, "OBJECT_TO_LIST")
        self.assertEqual(q["queries"], [{"operation": "search_text", "query": "probe"}])
        self.assertEqual(p["queries"], {"operation": "search_text", "query": "probe"})

    def test_explicit_operation_tag_to_canonical_query(self):
        p = query_project(queries=[
            {"search_text": {"query": "probe"}},
            {"read_range": {"path": "source.txt", "start_line": 1, "end_line": 1}},
        ])
        q, reason = normalize_query_project(p)
        self.assertEqual(reason, "EXPLICIT_KEY_TO_OPERATION")
        self.assertEqual(q["queries"][0], {"operation": "search_text", "query": "probe"})
        self.assertEqual(q["queries"][1]["operation"], "read_range")

    def test_missing_or_unknown_operation_stays_unmodified(self):
        for queries in (
            [{"query": "probe"}], [{"operation": "bad", "query": "probe"}],
            "search_text", {"query": "probe"}, [{"search_text": "probe"}],
            [{"search_text": {"operation": "read_range", "query": "probe"}}],
        ):
            p = query_project(queries=queries)
            q, reason = normalize_query_project(p)
            self.assertEqual(reason, "", queries)
            self.assertEqual(q, p)

    def test_query_adapter_never_alters_cwd_or_authorization(self):
        p = query_project(
            queries={"operation": "search_text", "query": "probe"},
            project_root=str(self.root), max_bytes=1234)
        q, reason = normalize_query_project(p)
        self.assertEqual(reason, "OBJECT_TO_LIST")
        self.assertEqual(q["project_root"], p["project_root"])
        self.assertEqual(q["max_bytes"], p["max_bytes"])
        self.assertEqual(q["action_id"], p["action_id"])

    def test_reuses_valid_progress_candidate_without_ledger_commit(self):
        before = read_progress("TASK-TX", self.root).to_dict()
        self.assertTrue(self.capture())
        bare = {"tool": "report_progress", "action_id": "P-REPAIRED",
                "decision": "CONTINUE", "next_action": "query_project"}
        restored, reason = self.restore(bare)
        self.assertEqual(reason, "CACHED_VALID_PROGRESS_PROPOSAL")
        self.assertEqual(restored["steps"], self.valid["steps"])
        self.assertEqual(restored["evidence_refs"], ["A-LIST"])
        self.assertEqual(restored["action_id"], "P-REPAIRED")
        self.assertEqual(read_progress("TASK-TX", self.root).to_dict(), before)
        self.assertEqual(validate_model_progress(restored, self.ledger)["current_step"], 1)

    def test_no_cross_request_epoch_or_ledger_reuse(self):
        self.assertTrue(self.capture())
        bare = {"tool": "report_progress", "action_id": "P",
                "decision": "CONTINUE", "next_action": "query_project"}
        for args in (
            {"request_id": "RR-FOREIGN"},
            {"task_epoch": "EP-FOREIGN"},
            {"evidence": {"A-LIST": {"status": "REVOKED"}}},
        ):
            _, reason = self.restore(bare, **args)
            self.assertFalse(reason, args)
        self.ledger.current_step = 1.5
        _, reason = self.restore(bare)
        self.assertFalse(reason)

    def test_no_reuse_for_conflicting_goal_step_evidence_or_next_tool(self):
        self.assertTrue(self.capture())
        bare = {"tool": "report_progress", "action_id": "P",
                "decision": "CONTINUE", "next_action": "query_project"}
        corrupt = [
            {"outcome": "SUCCESS"}, {"evidence_refs": ["FORGED"]},
            {"matched_condition_id": "COND-FAKE"},
            {"steps": [{"step": 1, "desc": "new", "status": "COMPLETED"}]},
            {"completion_contract": {"success": ["fake"]}},
            {"current_step": 2}, {"next_action": "write_file"},
            {"goal_transition": {"goal": "new"}},
        ]
        for changed in corrupt:
            _, reason = self.restore({**bare, **changed})
            self.assertFalse(reason, changed)
        _, reason = self.restore(bare, actions=[{"tool": "write_file", "action_id": "A-QUERY"}])
        self.assertFalse(reason)
        _, reason = self.restore(bare, actions=[query_project("DIFFERENT")])
        self.assertFalse(reason)

    def test_unverified_progress_does_not_enter_transaction(self):
        malformed = dict(self.valid, outcome="SUCCESS")
        self.assertFalse(self.tx.capture_query_rejection(
            malformed, [self.action], ledger=self.ledger,
            action_results={}, request_id="RR-TX", task_epoch="EP-TX"))
        self.assertEqual(self.tx.revision, 0)
        self.assertFalse(self.tx.capture_query_rejection(
            self.valid, [{"tool": "write_file", "action_id": "M"}],
            ledger=self.ledger, action_results={},
            request_id="RR-TX", task_epoch="EP-TX"))
        self.assertFalse(self.tx.capture_query_rejection(
            self.valid, [self.action], ledger=self.ledger,
            action_results={}, request_id="OTHER", task_epoch="EP-TX"))

    def test_patch_inherits_only_identical_accepted_fields(self):
        payload = {
            "tool": "report_progress", "action_id": "P",
            "decision": "CONTINUE", "outcome": "PENDING",
            "progress_update": "PATCH", "current_focus": "重新讀取",
            "next_action": "query_project", "decision_reason": "需要資料",
            "evidence_refs": ["A-LIST"], "matched_condition": "需要讀取來源",
            "steps": deepcopy(self.ledger.steps),
            "total_steps": self.ledger.total_steps,
            "current_step": self.ledger.current_step,
            "completion_contract": deepcopy(self.ledger.completion_contract),
            "base_evaluation": self.ledger.base_evaluation,
        }
        canonical, redundant = reconcile_redundant_patch_fields(payload, self.ledger)
        self.assertEqual(set(redundant), {
            "steps", "total_steps", "current_step",
            "completion_contract", "base_evaluation"})
        self.assertEqual(validate_model_progress(canonical, self.ledger)["current_step"], 1)
        contradictory = dict(payload, current_step=2)
        unchanged, redundant = reconcile_redundant_patch_fields(contradictory, self.ledger)
        self.assertEqual(unchanged["current_step"], 2)
        self.assertNotIn("current_step", redundant)
        full = dict(payload, progress_update="FULL")
        unchanged, redundant = reconcile_redundant_patch_fields(full, self.ledger)
        self.assertFalse(redundant)
        self.assertIn("steps", unchanged)

    def test_ccrc_classifies_structured_query_issue_without_semantic_replan(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR-TX", task_epoch="EP-TX", goal="unchanged")
        calls = [self.valid, self.action]
        d = [{"reason": "query_project_structured_queries_required"}]
        inspection = inspect_cross_contract(calls, d)
        self.assertTrue(inspection["relevant"])
        self.assertTrue(any(row["origin"] == "QUERY" for row in inspection["field_issues"]))
        first = unit.observe(calls, d, declared_tools=["query_project"])
        self.assertEqual(first["mode"], FIELD_REPAIR)
        self.assertIn("query_project.queries[]", repair_instruction(first))
        self.assertFalse(first["authorization_granted"])
        self.assertTrue(first["full_admission_required"])

    def test_repair_convergence_never_resets_bounded_budget(self):
        budget = BoundedRepairBudget(max_without_evidence=6, max_total=16,
                                     max_same_reason=6)
        for i in range(6):
            reason = ("invalid_progress_payload" if i % 2
                      else "query_project_structured_queries_required")
            result = budget.observe([{"reason": reason}], evidence_generation=0)
        self.assertEqual(result["status"], "EXHAUSTED")
        self.assertEqual(result["rejected_turns_without_new_evidence"], 6)
        self.assertFalse(result["terminal_outcome_inferred"])

    def test_end_to_end_synthetic_replay_query_progress_convergence(self):
        # Simulates RCA round 4: valid Progress, missing Query operation;
        # next response: minimal PATCH and explicitly chosen search_text.
        turns = []
        tool_actions = []
        request = "請唯讀驗證\n" + str(self.root)
        def planner(prompt, expected, attachments):
            turns.append(prompt)
            if len(turns) == 1:
                return response(
                    progress(),
                    {"tool": "list_directory", "action_id": "A-LIST",
                     "path": str(self.root)},
                )
            if len(turns) == 2:
                p = progress(current=1, next_action="query_project", action_id="P-QUERY")
                return response(p, query_project(
                    queries=[{"query": "probe"}], project_root=str(self.root)))
            if len(turns) == 3:
                self.assertIn("query_project", prompt)
                # Planner does not regenerate earlier Progress fields; Runtime
                # reconstructs the preserved valid proposal, then performs
                # FULL Action/Progress/Scope admission.
                p = {"tool": "report_progress", "action_id": "P-Q-REPAIRED",
                     "decision": "CONTINUE", "next_action": "query_project"}
                return response(p, query_project(
                    queries={"operation": "search_text", "query": "probe"},
                    project_root=str(self.root)))
            self.assertEqual(len(turns), 4, "unexpected repair cycle")
            self.assertIn("probe", prompt)
            p = {
                "tool": "report_progress", "action_id": "P-COMPLETE",
                "decision": "COMPLETE", "outcome": "SUCCESS",
                "current_step": 2,
                "steps": [
                    {"step": 1, "desc": "觀測", "status": "COMPLETED"},
                    {"step": 2, "desc": "查詢並驗證", "status": "COMPLETED"},
                ],
                "current_focus": "已實際查詢資料",
                "decision_reason": "原始資料確實存在",
                "matched_condition": "實際讀取並驗證資料",
                "evidence_refs": ["A-QUERY"],
            }
            return response(
                p, {"tool": "final_response", "action_id": "FINAL",
                    "content": "PROTOCOL_REPAIR_TX_PASS"})
        loop = WebAgentProtocolLoop(
            self.root, planner, progress_root=self.root, max_turns=12)
        original = loop._execute_action
        def tracked(action):
            tool_actions.append(action["tool"])
            return original(action)
        loop._execute_action = tracked
        answer = loop.run(request, trusted_authorized_paths=[str(self.root)])
        self.assertEqual(answer, "PROTOCOL_REPAIR_TX_PASS")
        self.assertEqual(len(turns), 4)
        self.assertEqual(tool_actions.count("query_project"), 1)
        self.assertEqual(tool_actions.count("list_directory"), 1)
        self.assertEqual(loop.terminal_outcome, "SUCCESS")
        self.assertEqual(loop._protocol_repair_tx.revision, 1)
        self.assertNotIn("A-QUERY-BAD", loop.action_result_ledger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
