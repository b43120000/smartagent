"""Live A fact-to-completion V9 IT and Live B scoped environment regressions.

Runs on the installed shared WebAgent loop with an offline, scripted Planner.
It never opens Web UI or installs into the user's SmartAgent runtime.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.environment_fact_contract import build_fact_refs, verify_fact_assertion
from agent_core.task_progress import condition_id_for
from agent_core.project_environment import (
    environment_index_path, publish_verified_dependency, read_project_environment,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop


def _fence(value):
    return "```smartagent_tool\n" + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n```"


class EnvironmentFactContractIT(unittest.TestCase):
    def test_bound_fact_cannot_be_faked_or_reused(self):
        fact = {"id": "package:demo", "kind": "PACKAGE", "name": "demo",
                "ecosystem": "python", "state": "MISSING",
                "source": "BUNDLED_PYTHON_METADATA_PROBE", "probe_status": "MISSING"}
        refs = build_fact_refs(request_id="RR-1", task_id="TASK-1",
                               task_epoch="E-1", facts=[fact])
        ref = next(iter(refs))
        condition_id = condition_id_for("success", "missing is observed")
        assertion = {"evidence_ref": ref, "expected_state": "MISSING",
                     "condition_id": condition_id}
        kwargs = dict(evidence_refs=[ref], matched_condition_id=condition_id,
                      request_id="RR-1", task_id="TASK-1",
                      task_epoch="E-1", fact_refs=refs)
        self.assertEqual(verify_fact_assertion(assertion=assertion, **kwargs)[0], "PASS")
        text_bound = {"evidence_ref": ref, "expected_state": "MISSING",
                      "condition_text": "missing is observed"}
        self.assertEqual(verify_fact_assertion(
            assertion=text_bound, matched_condition="missing is observed", **kwargs)[0], "PASS")
        self.assertNotEqual(verify_fact_assertion(
            assertion={**text_bound, "condition_text": "unrelated condition"},
            matched_condition="missing is observed", **kwargs)[0], "PASS")
        self.assertNotEqual(verify_fact_assertion(
            assertion={**assertion, "expected_state": "SATISFIED"}, **kwargs)[0], "PASS")
        self.assertNotEqual(verify_fact_assertion(
            assertion=assertion, **{**kwargs, "task_epoch": "E-2"})[0], "PASS")
        self.assertNotEqual(verify_fact_assertion(
            assertion=assertion, **{**kwargs, "matched_condition_id": "COND-OTHER"})[0], "PASS")
        self.assertNotEqual(verify_fact_assertion(
            assertion=assertion, **{**kwargs, "evidence_refs": [ref, "ACTION-FAIL"]})[0], "PASS")
        refs[ref]["fact"]["state"] = "SATISFIED"
        self.assertNotEqual(verify_fact_assertion(assertion=assertion, **kwargs)[0], "PASS")

    def test_live_a_full_protocol_no_duplicate_tool_probe(self):
        with tempfile.TemporaryDirectory(prefix="smartagent-live-a-it-") as temp:
            root = Path(temp).resolve()
            calls = []
            success = "本次 Runtime 已可信確認 Python distribution 是否缺失"
            def planner(prompt, _expected, _attachments):
                calls.append(prompt)
                match = re.search(r"ENVFACT-[A-F0-9]{24}", prompt)
                if not match:
                    raise AssertionError("Runtime PR-B fact reference missing")
                ref = match.group()
                progress = {
                    "tool": "report_progress", "action_id": "P-LIVE-A",
                    "total_steps": 1, "current_step": 1,
                    "steps": [{"step": 1, "desc": "引用可信套件探測結果",
                               "status": "COMPLETED"}],
                    "base_evaluation": "唯讀探測，不授權安裝或下載。",
                    "current_focus": "已確認依賴狀態", "next_action": "回覆",
                    "completion_contract": {
                        "success": [success],
                        "failure": ["可信探測無法完成"],
                        "in_progress": ["等待 Runtime 探測"],
                        "interrupted": ["缺少必要授權"],
                    },
                    "decision": "COMPLETE", "outcome": "SUCCESS",
                    "matched_condition": success,
                    "fact_assertion": {
                        "evidence_ref": ref, "expected_state": "MISSING",
                        "condition_text": success,
                    },
                    "evidence_refs": [ref],
                    "decision_reason": "已引用 request-scoped PR-B 缺失事實。",
                }
                return "\n".join(_fence(value) for value in (
                    progress,
                    {"tool": "final_response", "action_id": "A-LIVE-A",
                     "content": "smartagent-pr-bcd-live>=1.0: MISSING"},
                    {"tool": "turn_commit", "action_count": 2},
                ))
            from agent_core import dependency_adapters
            from agent_core import prerequisite_runtime_v3
            original_probe = prerequisite_runtime_v3._probe_package
            def probe(name, binary):
                if name == "smartagent-pr-bcd-live":
                    return ("MISSING", "")
                return original_probe(name, binary)
            loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=2)
            with patch.object(prerequisite_runtime_v3, "_probe_package", side_effect=probe), \
                 patch.dict(os.environ, {"SMARTAGENT_PR0_V1": "1",
                     "SMARTAGENT_PREREQ_V3_PR_A": "1", "SMARTAGENT_PREREQ_V3_BCD": "1",
                     "TRI_ONE_TASK_RESULT_V1": "1", "TRI_ONE_TEXT_ONLY_V1": "1"}), \
                 patch.object(loop.tools, "execute",
                     side_effect=AssertionError("No new tool probe permitted for Live A")):
                result = loop.run(
                    "確認 Bundled Python 環境中 smartagent-pr-bcd-live>=1.0 是否缺失，"
                    "僅允許唯讀檢查，禁止安裝、下載、修改或任何補救。",
                    request_id="RR-LIVE-A-IT", task_id="TASK-LIVE-A-IT",
                    task_epoch="EPOCH-LIVE-A-IT", source_tag="REMOTEAGENT_TELEGRAM",
                )
            self.assertIn("MISSING", result)
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertEqual(len(calls), 1)
            self.assertFalse(loop.action_result_ledger)
            self.assertEqual(loop.terminal_acceptance["evidence_verdict"], "PASS")
            self.assertFalse(loop._answer_only_admitted)
            self.assertEqual(len(loop._environment_fact_refs) >= 1, True)

    def test_live_a_repair_mixed_refs_without_gar_l5_or_tool(self):
        # Regression for real Live A: first terminal cites a Runtime fact plus
        # unrelated status ref. No tool has run, so repair the assertion only.
        from agent_core import prerequisite_runtime_v3
        with tempfile.TemporaryDirectory(prefix="smartagent-live-a-repair-") as temp:
            root = Path(temp).resolve()
            turns, events = [], []
            goal = "可信 Runtime 事實確認依賴是否缺失"
            def planner(prompt, _expected, _attachments):
                turns.append(prompt)
                match = re.search(r"ENVFACT-[A-F0-9]{24}", prompt)
                self.assertIsNotNone(match)
                ref = match.group()
                progress = {
                    "tool": "report_progress", "action_id": "PROGRESS-A-REPAIR",
                    "total_steps": 1, "current_step": 1,
                    "steps": [{"step": 1, "desc": "引用 Runtime 套件狀態",
                               "status": "COMPLETED"}],
                    "base_evaluation": "唯讀確認依賴缺失，不允許安裝。",
                    "current_focus": "確認依賴結果", "next_action": "回覆",
                    "completion_contract": {
                        "success": [goal], "failure": ["探測失敗"],
                        "in_progress": ["等待探測"], "interrupted": ["無法觀測"],
                    },
                    "decision": "COMPLETE", "outcome": "SUCCESS",
                    "matched_condition": goal,
                    "evidence_refs": [ref, "RUNTIME_STATUS"] if len(turns) == 1 else [ref],
                    "fact_assertion": {
                        "evidence_ref": ref, "expected_state": "MISSING",
                        "condition_text": goal,
                    },
                    "decision_reason": "可信套件缺失事實已取得。",
                }
                return "\n".join(_fence(x) for x in (
                    progress,
                    {"tool": "final_response", "action_id": "FINAL-A-REPAIR",
                     "content": "smartagent-pr-bcd-live>=1.0 MISSING"},
                    {"tool": "turn_commit", "action_count": 2},
                ))
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=3,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with patch.object(prerequisite_runtime_v3, "_probe_package",
                              return_value=("MISSING", "")), \
                 patch.dict(os.environ, {
                     "SMARTAGENT_PR0_V1": "1",
                     "SMARTAGENT_PREREQ_V3_PR_A": "1",
                     "SMARTAGENT_PREREQ_V3_BCD": "1",
                     "TRI_ONE_TASK_RESULT_V1": "1",
                     "TRI_ONE_TEXT_ONLY_V1": "1",
                 }), \
                 patch.object(loop.tools, "execute",
                              side_effect=AssertionError("Fact reuse must not execute tools")):
                result = loop.run(
                    "確認 Bundled Python 環境中 smartagent-pr-bcd-live>=1.0 是否缺失。"
                    "僅允許唯讀檢查，禁止安裝、下載、修改或任何補救。",
                    request_id="RR-LIVE-A-REPAIR",
                    task_id="TASK-LIVE-A-REPAIR",
                    task_epoch="EPOCH-LIVE-A-REPAIR",
                    source_tag="REMOTEAGENT_TELEGRAM",
                )
            self.assertIn("MISSING", result)
            self.assertEqual(len(turns), 2)
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertEqual(loop.terminal_acceptance["evidence_verdict"], "PASS")
            self.assertEqual(loop.action_result_ledger, {})
            self.assertIn("environment_fact_binding_repair_requested",
                          [name for name, _fields in events])

    def test_requested_root_preferred_over_default_workspace(self):
        with tempfile.TemporaryDirectory(prefix="smartagent-root-it-") as temp:
            base = Path(temp)
            default = base / "remoteagent"
            default.mkdir()
            project = base / "test-project"
            wheel = project / "wheelhouse"
            deps = project / "deps"
            wheel.mkdir(parents=True)
            deps.mkdir()
            result = WebAgentProtocolLoop._authorized_project_root(
                [str(project), str(wheel), str(deps),
                 str(project / ".agents" / "project_context" / "environment.json")],
                default,
            )
            self.assertEqual(result, project.resolve())
            self.assertEqual(WebAgentProtocolLoop._authorized_project_root(
                [str(project / "no-existence")], default), default)

    def test_restricted_project_environment_single_file_write(self):
        with tempfile.TemporaryDirectory(prefix="smartagent-env-it-") as temp:
            root = Path(temp)
            project = root / "project"
            deps = project / "deps"
            deps.mkdir(parents=True)
            index = environment_index_path(project)
            with patch("agent_core.paths.runtime_root", return_value=root / "runtime"):
                publish_verified_dependency(
                    project, distribution="smartagent-pr-bcd-live",
                    version="1.0", target=deps,
                    request_id="RR-META", action_id="RESUME-META",
                    evidence_ref="EVIDENCE-META",
                    authorized_project_write=True,
                    restricted_metadata_write=True,
                )
            self.assertTrue(index.is_file())
            observed = {str(p.relative_to(project)).replace("\\", "/")
                        for p in project.rglob("*") if p.is_file()}
            self.assertEqual(observed, {".agents/project_context/environment.json"})
            self.assertEqual(read_project_environment(project)["dependencies"]
                             ["smartagent-pr-bcd-live"]["version"], "1.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
