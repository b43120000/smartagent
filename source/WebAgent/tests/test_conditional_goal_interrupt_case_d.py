#!/usr/bin/env python3
"""Live Case D RCA: read-only-first, conditional repair, scoped interruption.

No real E: drive access, mutation, source discovery, permissions or network.
Evidence is the same trusted inspect_directory fixture used by the previous
Goal-Grounded test; terminal acceptance remains under normal Runtime gates.
"""
from __future__ import annotations

import unittest

from agent_core.conditional_goal_policy import conditional_goal_policy
from agent_core.goal_gap_ledger import reconstruct_goal_gaps
from agent_core.interrupt_readiness import (
    interrupt_readiness, proposed_interrupt_constraint,
)
from WebAgent.tests.test_goal_grounded_gap_p0_p3 import (
    ROOT, add_probe, runtime,
)

CASE_D = (
    "驗證並完成 Case D 的部署目標。\n"
    "預期部署根目錄：\n" + ROOT + "\n"
    "預期包含：case_a、case_b、case_c、fixture_manifest.json、check_results.py。\n"
    "以上五個項目必須直接位於指定根目錄。\n"
    "先進行唯讀環境檢查，確認指定根目錄與先前的部署來源。"
    "如果五個項目已經完整存在，請直接進行獨立內容完整性驗證。\n"
    "如果有缺少項目，請先確認來源完整性、寫入授權及衝突情況，"
    "再以不覆蓋既有檔案的方式補齊。\n"
    "禁止刪除、移動或覆蓋既有資料。"
    "不得為了滿足 Terminal Gate 而製造不必要的檔案異動。"
)


class ConditionalGoalCaseD(unittest.TestCase):
    def test_original_case_d_is_conditional_not_globally_read_only(self):
        policy = conditional_goal_policy(CASE_D)
        self.assertEqual(policy["intent"], "CONDITIONAL_REMEDIATION")
        self.assertTrue(policy["read_only_first"])
        self.assertTrue(policy["conditional_remediation"])
        self.assertFalse(policy["mutation_is_authorized"])
        self.assertIn("NO_OVERWRITE", policy["restrictions"])
        self.assertIn("NO_UNNECESSARY_MUTATION", policy["restrictions"])
        r = runtime(goal=CASE_D)
        add_probe(r, "dcaseD-inspect-003")
        view = reconstruct_goal_gaps(r)
        self.assertEqual(view["active_count"], 1)
        gap = view["active_gaps"][0]
        self.assertEqual(gap["observed_fact_ref"], "dcaseD-inspect-003")
        self.assertEqual(gap["status"], "UNRESOLVED")
        self.assertEqual(gap["goal_link"], "CONDITIONAL_REMEDIATION_CANDIDATE")
        self.assertEqual(gap["prerequisites"]["source_integrity"], "UNKNOWN")
        self.assertEqual(gap["prerequisites"]["write_authorization"], "UNKNOWN")
        self.assertEqual(gap["prerequisites"]["target_conflicts"], "UNKNOWN")
        self.assertIn("DISCOVER_SOURCE_IF_SCOPED", gap["repair_candidates"])
        self.assertIn("CREATE_DIRECTORY_IF_AUTHORIZED", gap["repair_candidates"])
        self.assertFalse(gap["authorization_granted"])
        self.assertFalse(gap["goal_completion_verified"])

    def test_read_only_only_and_explicit_absolute_prohibitions(self):
        for goal in (
            "請唯讀檢查 " + ROOT + " 是否存在，不得寫入或建立",
            "只做唯讀缺失判定，不要補救；目標 " + ROOT,
            "全程只允許唯讀檢查 " + ROOT + "，禁止任何實際寫入",
        ):
            with self.subTest(goal=goal):
                self.assertEqual(conditional_goal_policy(goal)["intent"], "READ_ONLY_ONLY")
                r = runtime(goal=goal)
                add_probe(r)
                v = reconstruct_goal_gaps(r)
                self.assertEqual(v["active_count"], 0)
                self.assertFalse(v["known_absences"][0]["authorization_granted"])

    def test_unrelated_file_restrictions_are_not_global_no_write(self):
        for goal in (
            "先唯讀檢查 " + ROOT + "，若缺少則補齊，不得修改原始 data.csv",
            "請在 " + ROOT + " 建立缺少的資料；禁止刪除及覆蓋既有檔案",
        ):
            with self.subTest(goal=goal):
                p = conditional_goal_policy(goal)
                self.assertNotEqual(p["intent"], "READ_ONLY_ONLY")
                self.assertFalse(p["mutation_is_authorized"])

    def test_unknown_or_unbound_goal_does_not_create_unverified_gap(self):
        p = conditional_goal_policy("請唯讀確認有無資料，無需補救")
        self.assertEqual(p["intent"], "DISCOVERY_OR_OBSERVATION")
        r = runtime(goal="請查看其他目錄，不要推定 " + ROOT + " 是部署目標")
        add_probe(r)
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)

    def test_scoped_read_candidate_does_not_use_missing_target_or_parent(self):
        r = runtime(goal=CASE_D)
        add_probe(r)
        gap = reconstruct_goal_gaps(r)
        no_scope = interrupt_readiness(gap, {
            "authorized_paths": [ROOT],
            "read_tools": ["inspect_directory", "list_directory"],
        })
        self.assertEqual(no_scope["candidate_count"], 0)
        self.assertEqual(no_scope["next_safe_category"],
                         "USER_INPUT_OR_OBJECTIVE_BLOCKER_IF_NEEDED")
        self.assertFalse(no_scope["write_authorization_granted"])
        self.assertFalse(no_scope["scope_expansion_granted"])
        self.assertIsNone(proposed_interrupt_constraint({
            "kind": "USER_INPUT",
            "detail": "請提供無法在目前授權範圍內確認的部署來源路徑",
        }, no_scope))
        # Even though ROOT is on drive E:, E:\\ is never inferred.
        self.assertNotIn(r"E:\\", str(no_scope["candidate_reads"]))

        other = r"E:\AuthorizedExistingSource"
        scoped = interrupt_readiness(gap, {
            "authorized_paths": [ROOT, other],
            "read_tools": ["inspect_directory"],
        })
        self.assertEqual(scoped["candidate_count"], 1)
        self.assertEqual(scoped["candidate_reads"][0]["paths"], [other])
        self.assertTrue(scoped["candidate_reads"][0]["normal_admission_required"])
        blocked = proposed_interrupt_constraint({
            "kind": "USER_INPUT", "detail": "請上傳可由已授權目錄讀取的檔案內容",
        }, scoped)
        self.assertEqual(blocked["reason"], "known_scoped_discovery_not_exhausted")
        self.assertFalse(blocked["candidate_reads"][0]["authorization_granted"])
        self.assertEqual(blocked["active_goal_gap_ids"], [
            gap["active_gaps"][0]["gap_id"]
        ])

    def test_genuine_user_permission_or_blocker_is_not_suppressed(self):
        r = runtime(goal=CASE_D)
        add_probe(r)
        gap = reconstruct_goal_gaps(r)
        readiness = interrupt_readiness(gap, {
            "authorized_paths": [r"E:\AuthorizedExistingSource"],
            "read_tools": ["inspect_directory"],
        })
        self.assertIsNone(proposed_interrupt_constraint({
            "kind": "USER_INPUT",
            "detail": "請確認是否同意授權寫入部署根目錄",
        }, readiness))
        self.assertIsNone(proposed_interrupt_constraint({
            "kind": "BLOCKED",
            "detail": "Actual runtime permission denied",
            "blocking_action_id": "ACT-DENIED",
        }, readiness))
        self.assertIsNone(proposed_interrupt_constraint({
            "kind": "RUNTIME_READ",
        }, readiness))

    def test_no_invented_gap_from_failed_unexecuted_list(self):
        r = runtime(goal=CASE_D)
        r.action_ledger["dcaseD-list-002"] = {
            "action": {
                "tool": "list_directory", "action_id": "dcaseD-list-002",
                "path": ROOT,
            },
            "result": "DIRECTORY_NOT_FOUND",
        }
        r.action_result_ledger["dcaseD-list-002"] = {
            "tool": "list_directory",
            "result_status": "FAILED",
            "result_reason": "DIRECTORY_NOT_FOUND",
            "executed": False,
            "execution_status": "FAILED",
            "evidence_status": "ACTIVE",
            "request_id": r.run_id,
            "task_epoch": r.task_epoch,
        }
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
