"""Regression R17: B-P1 terminal must use four real, fresh observation receipts.

This does not install a wheel, mutate the project, or verify full PR-B/C/D.
Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_pr_bcd_b_p1_terminal_r17.py
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.bp1_observation_terminal import (
    evaluate_bp1_terminal, is_bp1_request,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


ROOT = r"E:\\SmartAgent_PR_BCD_LIVE"
IDS = ["BP1-READ-01", "BP1-READ-02", "BP1-INSPECT-03", "BP1-READ-04"]


def request(root: str = ROOT) -> str:
    return (
        f"在 {root} 執行 PR-B/C/D 的 B-P1 Evidence Live 唯讀驗證。\n"
        "本輪只驗證 Runtime Observation、PR-A Action Evidence 與 Evidence Freshness。\n"
        "禁止任何實際寫入、安裝、網路下載、MUTATE、VERIFY、Resume。\n"
        f"允許套件安裝目標：{root}\\deps\n"
        f"另允許必要的 Project Environment metadata 寫入：{root}\\.agents\\project_context\\environment.json\n"
        "禁止其他專案路徑寫入。\n"
        "依賴：smartagent-pr-bcd-live>=1.0\n"
        "Wheel：wheelhouse\\smartagent_pr_bcd_live-1.0-py3-none-any.whl"
    )


def actions(root: str = ROOT):
    wh = root + r"\\wheelhouse"
    deps = root + r"\\deps"
    wheel = wh + r"\\smartagent_pr_bcd_live-1.0-py3-none-any.whl"
    return [
        {"tool": "list_directory", "action_id": IDS[0], "path": wh},
        {"tool": "inspect_directory", "action_id": IDS[1],
         "paths": [root, wh, deps], "recursive": False},
        {"tool": "run_command", "action_id": IDS[2], "operation": "INSPECT",
         "working_directory": root,
         "command": f"Get-Item -LiteralPath '{wheel}'", "verify": []},
        {"tool": "list_directory", "action_id": IDS[3], "path": deps},
    ]


class StubGraph:
    def __init__(self, ids=IDS):
        self.ids = ids
    def read(self, task_id):
        return {
            "identity": {"request_id": "RR-R17", "task_id": task_id,
                         "task_epoch": "EPOCH-R17"},
            "nodes": {"action:" + key.lower(): {"state": "SATISFIED"}
                      for key in self.ids},
        }


def make_loop():
    acts = actions()
    observations = [
        "NATIVE_DIRECTORY_LIST_CURRENT", "NATIVE_DIRECTORY_INSPECT_CURRENT",
        "PROVEN_INSPECT_EXIT_ZERO", "NATIVE_DIRECTORY_LIST_CURRENT",
    ]
    ledger = {}
    evidence = {}
    for i, action in enumerate(acts):
        key = action["action_id"]
        ledger[key] = {"action": copy.deepcopy(action)}
        evidence[key] = {
            "request_id": "RR-R17", "task_id": "TASK-R17", "task_epoch": "EPOCH-R17",
            "tool": action["tool"], "executed": True,
            "execution_status": "SUCCEEDED", "result_status": "SUCCEEDED",
            "observation_proof_admitted": True,
            "observation_proof_reason": observations[i],
            "verification_status": "UNVERIFIED" if i == 2 else "UNKNOWN",
            "effective_verification_status": "UNVERIFIED" if i == 2 else "UNKNOWN",
            "evidence_status": "ACTIVE", "expectation_status": "",
        }
    return SimpleNamespace(
        task_request=request(), run_id="RR-R17", task_id="TASK-R17",
        task_epoch="EPOCH-R17", _pr_v3_project_root=ROOT,
        _pr_v3_project_root_explicit=True, action_ledger=ledger,
        action_result_ledger=evidence, condition_result_ledger={},
        _pr_a_ledger=StubGraph(),
        pending_progress_repair_actions=[], pending_action_repair={},
        deferred_action_repairs={}, pending_result_recovery={},
        pending_verification_requirement={}, pending_command_field_repairs={},
        active_stage_manifest=None,
    )


class BP1TerminalR17(unittest.TestCase):
    def test_gate_admits_only_complete_current_four_receipts(self):
        loop = make_loop()
        progress = {"decision": "COMPLETE", "outcome": "SUCCESS",
                    "evidence_refs": list(IDS)}
        self.assertTrue(is_bp1_request(loop.task_request))
        result = evaluate_bp1_terminal(loop, progress)
        self.assertTrue(result["admitted"], result)
        self.assertEqual(result["full_pr_bcd"], "NOT_VERIFIED")

        def reject(mutator, why):
            bad = make_loop()
            p = copy.deepcopy(progress)
            mutator(bad, p)
            result = evaluate_bp1_terminal(bad, p)
            self.assertFalse(result["admitted"], (why, result))

        cases = [
            ("missing ref", lambda l, p: p["evidence_refs"].pop()),
            ("status marker substitution", lambda l, p:
                p["evidence_refs"].__setitem__(0, "RUNTIME_STATUS")),
            ("stale", lambda l, p: l.action_result_ledger[IDS[0]].update(
                evidence_status="STALE")),
            ("not executed", lambda l, p: l.action_result_ledger[IDS[1]].update(
                executed=False)),
            ("spoofed proof", lambda l, p: l.action_result_ledger[IDS[2]].update(
                observation_proof_reason="NATIVE_DIRECTORY_LIST_CURRENT")),
            ("unverified unrelated mutation", lambda l, p:
                l.action_ledger.update({"EXTRA": {"action": {"tool": "write_file"}}})),
            ("foreign request", lambda l, p:
                l.action_result_ledger[IDS[3]].update(request_id="OTHER")),
            ("unsatisfied PR-A", lambda l, p: setattr(l, "_pr_a_ledger",
                StubGraph(IDS[:3]))),
            ("bad literal path", lambda l, p:
                l.action_ledger[IDS[2]]["action"].update(
                    command="Get-Item -LiteralPath 'C:\\outside.whl'")),
            ("wrong command", lambda l, p:
                l.action_ledger[IDS[2]]["action"].update(
                    command="Set-Content -LiteralPath 'a.txt' -Value x")),
            ("undecided side effect", lambda l, p:
                setattr(l, "pending_result_recovery", {"active": True})),
            ("condition-result spoof", lambda l, p:
                l.condition_result_ledger.update({"FAKE": {"status": "PASS"}})),
            ("no explicit scope", lambda l, p:
                setattr(l, "_pr_v3_project_root_explicit", False)),
            ("non B-P1 request", lambda l, p:
                setattr(l, "task_request", "verify package installation")),
        ]
        for why, mutate in cases:
            with self.subTest(case=why):
                reject(mutate, why)

    @unittest.skipUnless(os.name == "nt", "end-to-end PowerShell observation is Windows-only")
    def test_real_four_read_actions_close_with_bounded_receipt(self):
        from agent_core.task_progress import read_progress
        with tempfile.TemporaryDirectory(prefix="sa-bp1-r17-") as td:
            base = Path(td)
            workspace = base / "default"
            workspace.mkdir()
            root = base / "SmartAgent_PR_BCD_LIVE"
            wh = root / "wheelhouse"
            deps = root / "deps"
            wh.mkdir(parents=True)
            deps.mkdir()
            wheel = wh / "smartagent_pr_bcd_live-1.0-py3-none-any.whl"
            wheel.write_bytes(b"not-an-installed-package")
            original = wheel.read_bytes()
            ids = list(IDS)
            clauses = {
                "success": ["四個 B-P1 Action 的 Observation Evidence 均有效"],
                "failure": ["B-P1 唯讀觀測失敗"],
                "in_progress": ["尚未完成四個指定唯讀 Action"],
                "interrupted": ["Runtime 無法安全執行指定唯讀 Action"],
            }
            turns = []
            def planner(prompt, expected, attachments):
                turns.append(prompt)
                if len(turns) == 1:
                    p = {
                        "tool": "report_progress", "action_id": "P-R17-INITIAL",
                        "base_evaluation": "只需要四個 B-P1 唯讀觀測",
                        "total_steps": 1, "current_step": 0.5,
                        "steps": [{"step": 1, "desc": "四個唯讀證據", "status": "IN_PROGRESS"}],
                        "current_focus": "執行 B-P1 四個唯讀觀測", "next_action": "list_directory",
                        "completion_contract": clauses, "decision": "CONTINUE",
                        "outcome": "PENDING", "matched_condition": clauses["in_progress"][0],
                        "evidence_refs": ["RUNTIME_STATUS"], "decision_reason": "尚需執行",
                    }
                    return response(p, *[
                        {"tool": "list_directory", "action_id": ids[0], "path": str(wh)},
                        {"tool": "inspect_directory", "action_id": ids[1],
                         "paths": [str(root), str(wh), str(deps)], "recursive": False},
                        {"tool": "run_command", "action_id": ids[2], "operation": "INSPECT",
                         "working_directory": str(root),
                         "command": f"Get-Item -LiteralPath '{wheel}'", "verify": []},
                        {"tool": "list_directory", "action_id": ids[3], "path": str(deps)},
                    ])
                return response({
                    "tool": "report_progress", "action_id": "P-R17-FINAL",
                    "decision": "COMPLETE", "outcome": "SUCCESS",
                    "current_step": 1,
                    "steps": [{"step": 1, "desc": "四個唯讀證據", "status": "COMPLETED"}],
                    "current_focus": "四個 Action 的 Runtime Observation Evidence 已取得",
                    "matched_condition": clauses["success"][0],
                    "evidence_refs": ids,
                    "decision_reason": "四筆 Observation 為 ACTIVE 且 PR-A SATISFIED",
                }, {
                    "tool": "final_response", "action_id": "F-R17",
                    "content": "B-P1 Observation SUCCESS; full PR-B/C/D NOT VERIFIED",
                })
            loop = WebAgentProtocolLoop(workspace, planner,
                                        progress_root=workspace, max_turns=3)
            before_deps = list(deps.iterdir())
            with patch("agent_core.security_context._configured_access_roots",
                       return_value=((workspace.resolve(),), ())), \
                 patch("agent_core.windows_security.restricted_executor_required",
                       return_value=False), \
                 patch.dict(os.environ, {
                     "SMARTAGENT_PREREQ_V3_PR_A": "1",
                     "SMARTAGENT_PREREQ_V3_BCD": "1",
                     "SMARTAGENT_GAR_V1_L4": "0",
                 }):
                out = loop.run(request(str(root)), request_id="RR-R17-E2E",
                               task_id="TASK-R17-E2E", task_epoch="EPOCH-R17-E2E",
                               trusted_authorized_paths=[str(root), str(deps),
                                   str(root / ".agents" / "project_context" / "environment.json")])
            self.assertIn("B-P1 Observation SUCCESS", out)
            self.assertEqual(len(turns), 2)
            self.assertEqual(loop.terminal_acceptance["admission_status"], "ACCEPTED")
            self.assertEqual(loop.terminal_acceptance["completion_kind"], "B_P1_OBSERVATION_ONLY")
            self.assertEqual(loop.terminal_acceptance["observation_acceptance"], "PASS")
            self.assertEqual(loop.terminal_acceptance["full_pr_bcd"], "NOT_VERIFIED")
            self.assertNotEqual(loop.terminal_acceptance["verification_status"], "PASS")
            self.assertEqual(len(loop.action_result_ledger), 4)
            self.assertEqual(wheel.read_bytes(), original)
            self.assertEqual(before_deps, list(deps.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
