#!/usr/bin/env python3
"""Source Candidate Grounding P0–P3, including Case D, without real disk writes."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_core.tool_result_contract import encode_result
from agent_core.goal_gap_ledger import reconstruct_goal_gaps
from agent_core.source_candidate_ledger import build_source_candidate_ledger
from agent_core.source_integrity_resolution import reconcile_source_integrity
from agent_core.interrupt_readiness import interrupt_readiness, _read_attempts
from agent_core.goal_prerequisite_resolution import (
    resolve_goal_prerequisites, premature_source_interrupt_diagnostic,
)
from agent_core.target_path_contract import extract_target_contract
from agent_core.source_history_registry import load_source_history, record_source_history
from WebAgent.tests.test_goal_grounded_gap_p0_p3 import runtime, add_probe, ROOT
from WebAgent.tests.test_conditional_goal_interrupt_case_d import CASE_D

PARENT = r"E:\KnownSourceWorkspace"
SOURCE = PARENT + r"\SmartAgent_GoalDriven_IT"
REQUIRED = ("case_a", "case_b", "case_c", "fixture_manifest.json", "check_results.py")


def task(goal=CASE_D, paths=None):
    r = runtime(goal=goal)
    r.authorized_paths = paths if paths is not None else [ROOT, PARENT]
    r._target_path_contract = extract_target_contract(CASE_D)
    add_probe(r, "INSPECT-TARGET", exists=False)
    return r


def listed(r, path, entries, aid, *, epoch=None, status="SUCCEEDED"):
    result = encode_result(
        status, "DIRECTORY_LISTED" if status == "SUCCEEDED" else "DIRECTORY_IO_ERROR",
        executed=status == "SUCCEEDED", requested_path=path,
        data={"entries": entries, "recursive": False}, tool="list_directory",
    )
    r.action_ledger[aid] = {"action": {"action_id": aid, "tool": "list_directory",
                                      "path": path}, "result": result}
    r.action_result_ledger[aid] = {
        "tool": "list_directory", "result_status": status,
        "executed": status == "SUCCEEDED",
        "execution_status": status,
        "evidence_status": "ACTIVE", "request_id": r.run_id,
        "task_epoch": epoch or r.task_epoch,
    }


class SourceCandidateGroundingTests(unittest.TestCase):
    def test_live_parent_listing_exposes_candidate_before_general_exploration(self):
        r = task()
        listed(r, PARENT, [{"name": "SmartAgent_GoalDriven_IT", "kind": "DIRECTORY"}],
               "PARENT-LIST")
        gap = reconstruct_goal_gaps(r)
        candidates = build_source_candidate_ledger(r, gap)
        self.assertEqual(candidates["candidate_count"], 1)
        row = candidates["candidates"][0]
        self.assertEqual(row["path"], SOURCE)
        self.assertEqual(row["origin"], "COMMITTED_PARENT_DIRECTORY_LISTING")
        self.assertEqual(row["provenance_ref"], "PARENT-LIST")
        self.assertEqual(row["scope_status"], "WITHIN_DECLARED_PATH")
        self.assertFalse(row["read_authorized"])
        interrupt = interrupt_readiness(
            gap, {"authorized_paths": r.authorized_paths,
                  "read_tools": ["inspect_directory", "list_directory"]},
            attempted_reads=_read_attempts(r), source_candidates=candidates,
        )
        self.assertEqual(interrupt["candidate_reads"][0]["tool"], "list_directory")
        self.assertEqual(interrupt["candidate_reads"][0]["path"], SOURCE)
        decision = resolve_goal_prerequisites(gap, interrupt, candidates)
        self.assertEqual(decision["next_category"], "DISCOVER_SOURCE_WITHIN_SCOPE")
        self.assertFalse(decision["write_authorization_granted"])
        bad_interrupt = premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT", "detail": "請提供來源資料夾完整路徑",
        }, decision)
        self.assertEqual(bad_interrupt["reason"],
                         "scoped_grounded_source_candidate_unexhausted")
        self.assertIsNone(premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT", "detail": "請提供來源目錄授權",
        }, decision))

    def test_structure_proven_only_after_real_source_listing(self):
        r = task()
        listed(r, PARENT, [{"name": "SmartAgent_GoalDriven_IT", "kind": "DIRECTORY"}],
               "PARENT-LIST")
        listed(r, SOURCE, [
            {"name": name, "kind": "FILE" if "." in name else "DIRECTORY"}
            for name in REQUIRED
        ], "SOURCE-LIST")
        gap = reconstruct_goal_gaps(r)
        candidates = build_source_candidate_ledger(r, gap)
        integrity = reconcile_source_integrity(r, candidates)
        self.assertEqual(integrity["verified_structure_count"], 1)
        source = integrity["sources"][0]
        self.assertEqual(source["structure_state"], "STRUCTURE_VERIFIED")
        self.assertEqual(source["source_list_evidence_ref"], "SOURCE-LIST")
        self.assertEqual(source["content_integrity"], "NOT_VERIFIED")
        self.assertFalse(source["source_ready"])
        interrupt = interrupt_readiness(
            gap, {"authorized_paths": r.authorized_paths,
                  "read_tools": ["inspect_directory", "list_directory"]},
            attempted_reads=_read_attempts(r), source_candidates=candidates)
        decision = resolve_goal_prerequisites(gap, interrupt, candidates, integrity)
        self.assertEqual(decision["next_category"], "VERIFY_SOURCE_CONTENTS")
        self.assertNotEqual(decision["next_category"], "CHECK_DEPLOYMENT_ADMISSION")

    def test_incomplete_source_never_claims_verification(self):
        r = task()
        listed(r, PARENT, [{"name": "SmartAgent_GoalDriven_IT", "kind": "DIRECTORY"}],
               "PARENT-LIST")
        listed(r, SOURCE, [{"name": "case_a", "kind": "FILE"}], "SOURCE-LIST")
        gap = reconstruct_goal_gaps(r)
        integrity = reconcile_source_integrity(r, build_source_candidate_ledger(r, gap))
        source = integrity["sources"][0]
        self.assertEqual(source["structure_state"], "STRUCTURE_MISMATCH")
        self.assertIn("case_b", source["missing_entries"])
        self.assertIn("case_a", source["wrong_type_entries"])
        self.assertFalse(source["source_ready"])

    def test_source_out_of_scope_only_requests_specific_authorization(self):
        goal = CASE_D + "\n先前部署來源路徑：" + SOURCE
        r = task(goal, paths=[ROOT])
        gap = reconstruct_goal_gaps(r)
        sources = build_source_candidate_ledger(r, gap)
        self.assertEqual(sources["candidates"][0]["scope_status"],
                         "OUTSIDE_DECLARED_PATHS")
        readiness = interrupt_readiness(
            gap, {"authorized_paths": [ROOT],
                  "read_tools": ["inspect_directory", "list_directory"]},
            source_candidates=sources)
        self.assertEqual(readiness["candidate_count"], 0)
        decision = resolve_goal_prerequisites(gap, readiness, sources)
        self.assertEqual(decision["next_category"], "SOURCE_SCOPE_AUTHORIZATION_REQUIRED")
        self.assertEqual(decision["out_of_scope_source_candidates"][0]["path"], SOURCE)

    def test_absence_not_proof_history_is_missing(self):
        r = task(paths=[ROOT])
        gap = reconstruct_goal_gaps(r)
        sources = build_source_candidate_ledger(r, gap)
        self.assertFalse(sources["historical_index_available"])
        self.assertEqual(sources["candidate_count"], 0)
        self.assertEqual(resolve_goal_prerequisites(gap, source_view=sources)["next_category"],
                         "SOURCE_LOCATION_OR_SCOPE_REQUIRED")

    def test_prior_history_lead_not_automatically_authorized(self):
        r = task(paths=[ROOT])
        r._source_candidate_history = {
            "schema": "SMARTAGENT_SOURCE_HISTORY_V1",
            "candidates": [{
                "path": SOURCE,
                "origin": "PREVIOUS_TASK_RECEIPT",
                "provenance_ref": "PREVIOUS-TASK-LEDGER-ENTRY",
            }],
        }
        source = build_source_candidate_ledger(r, reconstruct_goal_gaps(r))
        self.assertEqual(source["candidate_count"], 1)
        self.assertTrue(source["historical_index_available"])
        self.assertFalse(source["historical_leads_are_live_evidence"])
        self.assertEqual(source["candidates"][0]["existence"], "UNKNOWN")
        self.assertFalse(source["candidates"][0]["read_authorized"])

    def test_stale_listing_and_failed_listing_do_not_create_candidates(self):
        r = task()
        listed(r, PARENT, [{"name": "SmartAgent_GoalDriven_IT", "kind": "DIRECTORY"}],
               "STALE", epoch="ANOTHER-EPOCH")
        listed(r, PARENT, [{"name": "FakeSource", "kind": "DIRECTORY"}],
               "FAILED", status="FAILED")
        self.assertEqual(build_source_candidate_ledger(r, reconstruct_goal_gaps(r))
                         ["candidate_count"], 0)

    def test_list_already_attempted_does_not_repeat(self):
        r = task()
        listed(r, PARENT, [{"name": "SmartAgent_GoalDriven_IT", "kind": "DIRECTORY"}],
               "PARENT-LIST")
        listed(r, SOURCE, [], "SOURCE-LIST")
        gaps = reconstruct_goal_gaps(r)
        sources = build_source_candidate_ledger(r, gaps)
        interrupt = interrupt_readiness(
            gaps, {"authorized_paths": r.authorized_paths,
                   "read_tools": ["inspect_directory", "list_directory"]},
            attempted_reads=_read_attempts(r), source_candidates=sources)
        self.assertFalse(any(
            c.get("path") == SOURCE and c.get("tool") == "list_directory"
            for c in interrupt["candidate_reads"]
        ))


    def test_verified_structure_persisted_only_as_cross_task_history_hint(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt = encode_result(
                "SUCCEEDED", "DIRECTORY_LISTED", executed=True,
                requested_path=SOURCE, tool="list_directory",
                data={"entries": [
                    {"name": name, "kind": "FILE" if "." in name else "DIRECTORY"}
                    for name in REQUIRED
                ]},
            )
            ok = record_source_history(
                Path(temp), target=ROOT, source=SOURCE,
                expected_entries=list(REQUIRED),
                actual_tool="list_directory", result=receipt,
                action_id="ACT-LIVE", task_id="TASK-OLD",
            )
            self.assertTrue(ok)
            history = load_source_history(Path(temp), ROOT)
            self.assertEqual(len(history["candidates"]), 1)
            self.assertEqual(history["candidates"][0]["path"], SOURCE)
            self.assertEqual(history["candidates"][0]["origin"], "PREVIOUS_TASK_RECEIPT")
            self.assertEqual(load_source_history(Path(temp), r"E:\OtherGoal")["candidates"], [])
            new_task = task(paths=[ROOT])
            new_task._source_candidate_history = history
            source_view = build_source_candidate_ledger(new_task, reconstruct_goal_gaps(new_task))
            self.assertEqual(source_view["candidates"][0]["existence"], "UNKNOWN")
            self.assertEqual(source_view["candidates"][0]["scope_status"],
                             "OUTSIDE_DECLARED_PATHS")
            self.assertFalse(source_view["candidates"][0]["read_authorized"])

    def test_failed_listing_never_persists_source_history(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = encode_result(
                "FAILED", "DIRECTORY_IO_ERROR", executed=False,
                requested_path=SOURCE, tool="list_directory",
                data={"entries": [
                    {"name": name, "kind": "FILE" if "." in name else "DIRECTORY"}
                    for name in REQUIRED
                ]},
            )
            self.assertFalse(record_source_history(
                Path(temp), target=ROOT, source=SOURCE,
                expected_entries=list(REQUIRED),
                actual_tool="list_directory", result=fake,
                action_id="ACT-FAIL", task_id="TASK-OLD",
            ))
            self.assertEqual(load_source_history(Path(temp), ROOT)["candidates"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
