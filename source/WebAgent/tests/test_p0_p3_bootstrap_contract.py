#!/usr/bin/env python3
"""P0-P3 offline bootstrap, CCRC Action hold, target binding, READY tests."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.initial_progress_normalization import normalize_initial_progress
from agent_core.session_protocol import SessionProtocol
from agent_core.task_progress import initialize_progress, validate_model_progress
from agent_core.target_path_contract import extract_target_contract, verify_target_completion
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def initial():
    return {
        "tool": "report_progress", "action_id": "P-INITIAL",
        "steps": [{"step": "1", "desc": "讀取現有檔案",
                   "status": "IN_PROGRESS"}],
        "current_focus": "檢查指定目錄", "next_action": "list_directory",
        "completion_contract": {
            "success": ["指定目錄已驗證"],
            "failure": ["指定目錄讀取失敗"],
            "in_progress": ["需要讀取目錄"],
            "interrupted": ["工具無法取得目錄狀態"],
        },
        "decision": "CONTINUE", "matched_condition": "需要讀取目錄",
        "decision_reason": "先觀測環境，不假設成功",
    }


class BootstrapContractIT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_initial_fields_derived_only_from_model_data(self):
        progress, audit = normalize_initial_progress(
            initial(), has_plan=False, action_result_count=0)
        self.assertEqual(progress["total_steps"], 1)
        self.assertEqual(progress["current_step"], 0)
        self.assertEqual(progress["base_evaluation"], "檢查指定目錄")
        self.assertEqual(progress["outcome"], "PENDING")
        self.assertEqual(progress["evidence_refs"], ["REQUEST_ACCEPTED"])
        self.assertTrue(audit["status_marker_only"])
        self.assertFalse(audit["terminal_evidence_created"])
        ledger = initialize_progress(
            "TASK-P0", request_id="RR-P0", goal="唯讀目錄", root=self.root)
        normalized = validate_model_progress(progress, ledger)
        self.assertEqual(normalized["decision"], "CONTINUE")
        self.assertEqual(normalized["current_step"], 0)

    def test_no_terminal_or_forged_ref_normalization(self):
        p = initial()
        p.update(decision="COMPLETE", outcome="SUCCESS", evidence_refs=[])
        copy, info = normalize_initial_progress(
            p, has_plan=False, action_result_count=0)
        self.assertFalse(info["applied"])
        self.assertEqual(copy["evidence_refs"], [])
        p = initial()
        p["evidence_refs"] = ["OTHER_TASK_ACTION"]
        copy, info = normalize_initial_progress(
            p, has_plan=False, action_result_count=0)
        self.assertEqual(copy["evidence_refs"], ["OTHER_TASK_ACTION"])
        self.assertNotIn("evidence_refs", info["normalized_fields"])
        for has_plan, count in ((True, 0), (False, 1)):
            copy, info = normalize_initial_progress(
                initial(), has_plan=has_plan, action_result_count=count)
            self.assertFalse(info["applied"])

    def test_invalid_explicit_plan_fields_not_rewritten(self):
        p = initial()
        p.update(total_steps=0, current_step=-1, base_evaluation=42,
                 evidence_refs=["REQUEST_ACCEPTED"], outcome="PENDING")
        copy, _ = normalize_initial_progress(
            p, has_plan=False, action_result_count=0)
        for key in ("total_steps", "current_step", "base_evaluation"):
            self.assertEqual(copy[key], p[key])

    def test_full_action_loop_admits_first_progress_without_schema_cycle(self):
        rounds = []
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "sample.txt").write_text("EVIDENCE", encoding="utf-8")
        def planner(prompt, expected, attachments):
            rounds.append(prompt)
            if len(rounds) == 1:
                return response(
                    initial(),
                    {"tool": "list_directory", "action_id": "A-ROOT",
                     "path": str(destination)},
                )
            self.assertEqual(len(rounds), 2, "unexpected repair loop")
            return response(
                {"tool": "report_progress", "action_id": "P-DONE",
                 "decision": "COMPLETE", "outcome": "SUCCESS",
                 "current_step": 1,
                 "steps": [{"step": 1, "desc": "讀取現有檔案",
                            "status": "COMPLETED"}],
                 "current_focus": "目錄已觀測",
                 "matched_condition": "指定目錄已驗證",
                 "decision_reason": "list_directory 完成",
                 "evidence_refs": ["A-ROOT"]},
                {"tool": "final_response", "action_id": "A-END",
                 "content": "BOOTSTRAP_PROGRESS_OK"},
            )
        loop = WebAgentProtocolLoop(
            self.root, planner, progress_root=self.root, max_turns=8)
        answer = loop.run("唯讀檢查目錄 " + str(destination),
                          trusted_authorized_paths=[str(destination)])
        self.assertEqual(answer, "BOOTSTRAP_PROGRESS_OK")
        self.assertEqual(len(rounds), 2)
        self.assertIn("A-ROOT", loop.action_result_ledger)

    def test_semantically_identical_unexecuted_action_restored(self):
        loop = WebAgentProtocolLoop(self.root, lambda *_: "", progress_root=self.root)
        loop.run_id = "RR-HELD"
        loop.turn_id = 4
        loop._ccrc = SimpleNamespace(replan_required=False)
        held = {"tool": "list_directory", "action_id": "HOLD",
                "path": str(self.root)}
        loop.pending_progress_repair_actions = [held]
        revised = loop._restore_progress_repair_actions([
            {"tool": "report_progress", "action_id": "P-R",
             "decision": "CONTINUE"},
            dict(held, action_id="NEW-ID"),
            {"tool": "turn_commit", "action_count": 2},
        ])
        self.assertEqual(revised[1], held)
        self.assertEqual(revised[-1]["action_count"], 2)
        self.assertNotIn("HOLD", loop.action_ledger)

    def test_mismatched_unexecuted_action_not_auto_restored(self):
        loop = WebAgentProtocolLoop(self.root, lambda *_: "", progress_root=self.root)
        loop.run_id = "RR-HELD"
        loop.turn_id = 4
        loop._ccrc = SimpleNamespace(replan_required=False)
        held = {"tool": "list_directory", "action_id": "HOLD",
                "path": str(self.root)}
        loop.pending_progress_repair_actions = [held]
        altered = dict(held, action_id="NEW-ID", path=str(self.root / "other"))
        revised = loop._restore_progress_repair_actions([
            {"tool": "report_progress", "action_id": "P-R"},
            altered, {"tool": "turn_commit", "action_count": 2},
        ])
        self.assertEqual(revised[1], altered)
        self.assertEqual(loop.pending_progress_repair_actions, [held])

    def test_replan_releases_unexecuted_hold_only(self):
        loop = WebAgentProtocolLoop(self.root, lambda *_: "", progress_root=self.root)
        loop.run_id = "RR-REPLAN"
        loop.turn_id = 5
        loop._ccrc = SimpleNamespace(replan_required=True)
        held = {"tool": "list_directory", "action_id": "HOLD",
                "path": str(self.root)}
        loop.pending_progress_repair_actions = [held]
        corrected = [dict(held, action_id="NEW-ACTION"),
                     {"tool": "turn_commit", "action_count": 1}]
        self.assertEqual(loop._restore_progress_repair_actions(corrected), corrected)
        self.assertEqual(loop.pending_progress_repair_actions, [])
        self.assertFalse(loop.action_ledger)

    def test_markdown_targets_and_malformed_binding_fail_closed(self):
        target = "E:\\CameraLab\\fixture"
        examples = [
            "預期部署根目錄：" + target,
            "預期部署根目錄：" + chr(96) + target + chr(96),
            '預期部署根目錄："' + target + '"',
            "**預期部署根目錄：** " + chr(96) + target + chr(96),
            "預期部署根目錄：E:/CameraLab/fixture",
        ]
        for input_text in examples:
            contract = extract_target_contract(
                "請重新驗證並完成部署。\n" + input_text)
            self.assertIsNotNone(contract, input_text)
            self.assertEqual(contract["binding_status"], "BOUND", contract)
            self.assertEqual(contract["requested_target"].lower().replace("/", "\\"),
                             target.lower())
        invalid = extract_target_contract(
            "請完成部署。預期部署根目錄：" + chr(96) + "[malformed]" + chr(96))
        self.assertIsNotNone(invalid)
        self.assertEqual(invalid["binding_status"], "AMBIGUOUS")
        unverified = verify_target_completion(
            invalid, {}, None, evidence_pass=True)
        self.assertEqual(unverified["status"], "UNVERIFIED")
        self.assertEqual(unverified["reason"], "REQUEST_TARGET_AMBIGUOUS")

    def test_marker_only_bootstrap_is_not_identity_or_task_evidence(self):
        session = SessionProtocol("web_agent_direct", 9, "protocol")
        for reply in ("AGENT_PROTOCOL_READY", "[AGENT_PROTOCOL_READY]",
                      " \n [AGENT_PROTOCOL_READY] \n"):
            self.assertTrue(session.parse_protocol_ready_marker_only(reply))
            self.assertFalse(session.parse_protocol_ready(reply, session_id="SES-1"))
        for reply in ("[AGENT_PROTOCOL_READY] extra",
                      "[AGENT_PROTOCOL_READY]\n{\"protocol_hash\":\"WRONG\"}",
                      "[AGENT_SESSION_READY]", "ready", ""):
            self.assertFalse(session.parse_protocol_ready_marker_only(reply))


if __name__ == "__main__":
    unittest.main(verbosity=2)
