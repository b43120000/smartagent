#!/usr/bin/env python3
"""Goal-Directed Prerequisite Resolution: pure evidence/decision regressions.

No real file operations, network requests, or permission changes.
"""
from __future__ import annotations

import unittest

from agent_core.goal_gap_ledger import (
    decision_grounding_diagnostics, reconstruct_goal_gaps,
)
from agent_core.goal_prerequisite_resolution import (
    source_requirement, resolve_goal_prerequisites,
    premature_source_interrupt_diagnostic,
)
from agent_core.interrupt_readiness import interrupt_readiness, _read_attempts
from WebAgent.tests.test_goal_grounded_gap_p0_p3 import (
    ROOT, add_probe, runtime,
)
from WebAgent.tests.test_conditional_goal_interrupt_case_d import CASE_D


CREATE_GOAL = (
    "請在 " + ROOT + " 建立新的資料夾並產生新的 config.json。"
    "已經知道該資料夾不存在；不需要既有檔案，禁止覆蓋。"
)
PARENT = r"E:\AuthorizedParent"


def view_for(goal, *, missing=True):
    r = runtime(goal=goal)
    if missing:
        add_probe(r, "OBSERVED-ABSENT")
    return r, reconstruct_goal_gaps(r)


class GoalDirectedPrerequisiteTests(unittest.TestCase):
    def test_local_create_prefers_goal_action_without_source_discovery(self):
        r, gaps = view_for(CREATE_GOAL)
        self.assertEqual(gaps["source_requirement"], "NOT_REQUIRED")
        gap = gaps["active_gaps"][0]
        self.assertEqual(gap["prerequisites"]["source_integrity"], "NOT_REQUIRED")
        self.assertNotIn("DISCOVER_SOURCE_IF_SCOPED", gap["repair_candidates"])
        caps = {
            "authorized_paths": [ROOT, PARENT],
            "read_tools": ["inspect_directory", "list_directory"],
        }
        interrupt = interrupt_readiness(gaps, caps, attempted_reads=_read_attempts(r))
        # The existence of a scoped read candidate does not make an irrelevant
        # source a prerequisite for a standalone create goal.
        self.assertEqual(interrupt["candidate_count"], 1)
        resolution = resolve_goal_prerequisites(gaps, interrupt)
        self.assertEqual(
            resolution["next_category"], "PROPOSE_GOAL_ACTION_FOR_ADMISSION"
        )
        self.assertEqual(resolution["reason_code"], "TARGET_ABSENT_SOURCE_NOT_REQUIRED")
        self.assertEqual(resolution["source_candidate_reads"], [])
        self.assertFalse(resolution["write_authorization_granted"])
        self.assertFalse(resolution["parent_scope_expansion_granted"])
        self.assertFalse(resolution["action_selected_or_executed"])
        self.assertFalse(resolution["repeat_absence_observation_needed"])

    def test_case_d_needs_verified_existing_source(self):
        r, gaps = view_for(CASE_D)
        self.assertEqual(source_requirement(CASE_D), "REQUIRED")
        self.assertEqual(gaps["source_requirement"], "REQUIRED")
        self.assertIn("DISCOVER_SOURCE_IF_SCOPED",
                      gaps["active_gaps"][0]["repair_candidates"])
        read = interrupt_readiness(gaps, {
            "authorized_paths": [ROOT, r"E:\ExistingDeploymentSource"],
            "read_tools": ["inspect_directory", "list_directory"],
        }, attempted_reads=_read_attempts(r))
        resolution = resolve_goal_prerequisites(gaps, read)
        self.assertEqual(resolution["next_category"], "DISCOVER_SOURCE_WITHIN_SCOPE")
        self.assertEqual(resolution["source_candidate_reads"][0]["paths"],
                         [r"E:\ExistingDeploymentSource"])
        self.assertFalse(resolution["source_integrity_verified"])
        self.assertFalse(resolution["write_authorization_granted"])

    def test_case_d_exhausted_scope_can_request_real_source_location(self):
        r, gaps = view_for(CASE_D)
        read = interrupt_readiness(gaps, {
            "authorized_paths": [ROOT],
            "read_tools": ["inspect_directory", "list_directory"],
        }, attempted_reads=_read_attempts(r))
        resolution = resolve_goal_prerequisites(gaps, read)
        self.assertEqual(resolution["next_category"], "SOURCE_LOCATION_OR_SCOPE_REQUIRED")
        self.assertEqual(resolution["source_candidate_reads"], [])
        self.assertIsNone(premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT", "detail": "請提供先前的部署來源完整路徑",
        }, resolution))

    def test_source_request_for_standalone_create_is_rejected(self):
        _, gaps = view_for(CREATE_GOAL)
        resolution = resolve_goal_prerequisites(gaps)
        bad = premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT", "detail": "請提供來源資料夾完整路徑",
        }, resolution)
        self.assertEqual(bad["reason"], "source_not_required_for_explicit_creation_goal")
        self.assertIn("GOAL_PREREQUISITE", bad["marker"])
        self.assertFalse(resolution["write_authorization_granted"])
        self.assertIsNone(premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT", "detail": "請授權在目標資料夾寫入資料",
        }, resolution))
        self.assertIsNone(premature_source_interrupt_diagnostic({
            "kind": "BLOCKED", "detail": "Permission denied",
        }, resolution))

    def test_missing_directory_listing_is_rejected_before_dispatch(self):
        _, gaps = view_for(CREATE_GOAL)
        for action in (
            {"tool": "list_directory", "path": ROOT},
            {"tool": "find_file", "root": ROOT},
            {"tool": "query_project", "project_root": ROOT},
        ):
            with self.subTest(tool=action["tool"]):
                failures = decision_grounding_diagnostics([action], gaps)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["reason"],
                                 "goal_gap_known_missing_workspace")
        # No expansion of scope to parent or sibling; such reads still need
        # normal Tool Admission, but are not disproved by the missing child.
        self.assertEqual(decision_grounding_diagnostics([
            {"tool": "list_directory", "path": PARENT},
        ], gaps), [])

    def test_read_only_goal_has_no_creation_preference(self):
        goal = "全程只允許唯讀檢查 " + ROOT + "，禁止任何實際寫入"
        _, gaps = view_for(goal)
        self.assertEqual(gaps["active_count"], 0)
        self.assertEqual(source_requirement(goal), "NOT_APPLICABLE")
        self.assertEqual(resolve_goal_prerequisites(gaps)["next_category"],
                         "EVALUATE_GOAL_OR_OBSERVE")

    def test_generic_deployment_is_not_assumed_to_be_source_free(self):
        goal = "請在 " + ROOT + " 部署完整環境"
        _, gaps = view_for(goal)
        self.assertEqual(gaps["source_requirement"], "UNDETERMINED")
        self.assertEqual(resolve_goal_prerequisites(gaps)["next_category"],
                         "RESOLVE_SOURCE_SEMANTICS")
        self.assertNotEqual(resolve_goal_prerequisites(gaps)["next_category"],
                            "PROPOSE_GOAL_ACTION_FOR_ADMISSION")

    def test_stale_and_failed_observations_do_not_unlock_creation(self):
        r = runtime(goal=CREATE_GOAL)
        add_probe(r, "OBSERVED-ABSENT")
        r.action_result_ledger["OBSERVED-ABSENT"]["task_epoch"] = "STALE-EPOCH"
        stale = reconstruct_goal_gaps(r)
        self.assertEqual(stale["active_count"], 0)
        self.assertEqual(resolve_goal_prerequisites(stale)["next_category"],
                         "EVALUATE_GOAL_OR_OBSERVE")
        r.action_result_ledger["OBSERVED-ABSENT"]["task_epoch"] = r.task_epoch
        r.action_result_ledger["OBSERVED-ABSENT"]["executed"] = False
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)

    def test_fresh_present_observation_clears_creation_candidate(self):
        r, gaps = view_for(CREATE_GOAL)
        self.assertEqual(gaps["active_count"], 1)
        add_probe(r, "OBSERVED-PRESENT", exists=True)
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)
        self.assertEqual(
            resolve_goal_prerequisites(reconstruct_goal_gaps(r))["next_category"],
            "EVALUATE_GOAL_OR_OBSERVE",
        )

    def test_path_hints_do_not_grant_write_or_parent_scope(self):
        _, gaps = view_for(CREATE_GOAL)
        view = resolve_goal_prerequisites(gaps, {
            "candidate_reads": [{
                "tool": "list_directory", "path": "E:\\",
                "authorization_granted": False,
            }],
        })
        self.assertEqual(view["source_candidate_reads"], [])
        self.assertFalse(view["parent_scope_expansion_granted"])
        self.assertFalse(view["write_authorization_granted"])
        self.assertTrue(view["normal_tool_admission_required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
