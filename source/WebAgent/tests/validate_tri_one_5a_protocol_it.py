"""Tri-One 5A integration: real V9 parser/Progress/Terminal with fake Planner.

Never opens Browser, sends Telegram or touches user files. Every fixture uses a
temporary workspace; flags are isolated. OFFLINE IT, not Live PASS.

Run from repo root with PYTHONPATH=source:
    python -B source/WebAgent/tests/validate_tri_one_5a_protocol_it.py
"""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop


def fence(payload: dict) -> str:
    ticks = chr(96) * 3
    return ticks + "smartagent_tool\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ) + "\n" + ticks


def answer_only_response(*, text: str = "RAII releases owned resources.",
                         omit_evidence: bool = False,
                         evidence_refs: list[str] | None = None) -> str:
    progress = {
        "tool": "report_progress", "action_id": "P-ANSWER-01",
        "total_steps": 1, "current_step": 1,
        "steps": [{"step": 1, "desc": "直接提供文字解說",
                   "status": "COMPLETED"}],
        "base_evaluation": "使用者要求一般概念說明，無須存取外部狀態。",
        "current_focus": "已提供一般概念說明",
        "next_action": "回覆使用者",
        "completion_contract": {
            "success": ["已提供一般概念的文字解說"],
            "failure": ["無法提供可靠說明"],
            "in_progress": ["正在準備說明"],
            "interrupted": ["無法繼續回答"],
        },
        "decision": "COMPLETE", "outcome": "SUCCESS",
        "matched_condition": "已提供一般概念的文字解說",
        "decision_reason": "原始目標只需要文字說明且未執行工具。",
    }
    if not omit_evidence:
        progress["evidence_refs"] = [] if evidence_refs is None else evidence_refs
    final = {"tool": "final_response", "action_id": "A-ANSWER-01",
             "content": text}
    commit = {"tool": "turn_commit", "action_count": 2}
    return "\n".join(fence(x) for x in (progress, final, commit))


class AnswerOnlyV9IT(unittest.TestCase):
    def _loop_case(self, request: str, source_tag: str, *, omit_evidence=False):
        with tempfile.TemporaryDirectory(prefix="goal1-v9-it-") as directory:
            root = Path(directory)
            turns = []
            def planner(prompt, expected, attachments):
                turns.append((prompt, dict(expected), list(attachments or [])))
                return answer_only_response(omit_evidence=omit_evidence)
            loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=2)
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                answer = loop.run(
                    request, request_id="RR-5A-IT-01",
                    task_id="TASK-5A-IT-01", task_epoch="EPOCH-5A-IT-01",
                    source_tag=source_tag,
                )
            self.assertEqual(answer, "RAII releases owned resources.")
            self.assertEqual(len(turns), 1, "eligible text should take one planner turn")
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.condition_result_ledger, {})
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertTrue(loop._answer_only_admitted)
            receipt = loop.terminal_acceptance
            self.assertEqual(receipt["admission_status"], "ACCEPTED")
            self.assertEqual(receipt["completion_kind"], "ANSWER_ONLY")
            self.assertEqual(receipt["admission_reason"],
                             "ANSWER_ONLY_RESPONSE_ADMITTED")
            self.assertEqual(receipt["evidence_refs"], [])
            self.assertEqual(receipt["evidence_verdict"], "UNVERIFIED")
            self.assertTrue(receipt["receipt_id"].startswith("TERM-"))
            self.assertIn("[TRI_ONE_ANSWER_ONLY_CANDIDATE]", turns[0][0])

    def test_real_telegram_raii_negative_noop_live_text_acceptance(self):
        # Exact wording from a previously failed RemoteAgent Live IT. The
        # Runtime must admit this no-side-effect answer in one V9 turn.
        request = (
            "請解釋 C++ RAII（Resource Acquisition Is Initialization）的設計原理，"
            "並提供一個使用 std::unique_ptr 的簡短範例。\\n"
            "只需要文字解釋與程式碼範例，不需要執行程式、讀取檔案或修改任何資料。\\n"
            "請完成後直接回報結果。"
        )
        for source in ("REMOTEAGENT_TELEGRAM", "REMOTEAGENT_WEBCOPILOT_V9", "WEB_DIRECT"):
            with self.subTest(source=source):
                self._loop_case(request, source)
                self._loop_case(request, source, omit_evidence=True)

    def test_previous_no_tool_terminal_prerequisite_can_be_released(self):
        with tempfile.TemporaryDirectory(prefix="goal1-advisory-") as directory:
            root = Path(directory)
            turns, events = [], []
            def planner(prompt, expected, attachments):
                turns.append(prompt)
                # An unverified boast is not a pure text answer; it creates
                # the legacy advisory no-execution prerequisite first.
                if len(turns) == 1:
                    return answer_only_response(text="I checked your files.")
                return answer_only_response(text="RAII ties ownership to scope.")
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=3,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1":"1",
                "TRI_ONE_TEXT_ONLY_V1":"1",
            }), patch.object(loop.tools, "execute",
                             side_effect=AssertionError("tools must not run")):
                answer = loop.run(
                    "請解釋 RAII，不需要執行程式、讀取檔案或修改任何資料。",
                    request_id="RR-ADVISORY-5A", task_id="TASK-ADVISORY-5A",
                    task_epoch="EPOCH-ADVISORY-5A",
                    source_tag="REMOTEAGENT_TELEGRAM",
                )
            self.assertEqual(answer, "RAII ties ownership to scope.")
            self.assertEqual(len(turns), 2)
            self.assertFalse(loop.pending_verification_requirement)
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.condition_result_ledger, {})
            self.assertEqual(loop.terminal_acceptance["completion_kind"], "ANSWER_ONLY")
            self.assertEqual(loop.terminal_acceptance["evidence_refs"], [])
            self.assertEqual(loop.terminal_acceptance["evidence_verdict"], "UNVERIFIED")
            self.assertEqual(len([1 for name, _ in events if name ==
                                  "tri_one_answer_only_advisory_prerequisite_released"]), 1)
            candidates = [row for name, row in events
                          if name == "tri_one_answer_only_candidate"]
            self.assertFalse(candidates[0]["eligible"])
            self.assertTrue(candidates[-1]["eligible"])
            self.assertTrue(all(row["response_received"] for row in candidates))

    def test_telegram_original_route_candidate(self):
        self._loop_case("Explain RAII in C++.", "REMOTEAGENT_TELEGRAM")

    def test_webcopilot_original_route_candidate(self):
        self._loop_case("Explain RAII in C++.", "REMOTEAGENT_WEBCOPILOT_V9")

    def test_omitted_evidence_routes_share_one_v9_admission(self):
        for source in (
            "REMOTEAGENT_TELEGRAM",
            "REMOTEAGENT_WEBCOPILOT_V9",
            "REMOTEAGENT_TELEGRAM_SIMULATION",
            "WEB_DIRECT",
        ):
            with self.subTest(source=source):
                self._loop_case("Explain RAII in C++.", source,
                                omit_evidence=True)

    def test_runtime_context_refs_are_classified_without_action_evidence(self):
        request = "Explain RAII in C++."
        for refs in (
            ["REQUEST_ACCEPTED"], ["RUNTIME_STATUS"],
            ["REQUEST_ACCEPTED", "RUNTIME_STATUS"],
        ):
            with self.subTest(refs=refs), tempfile.TemporaryDirectory(
                prefix="goal1-status-classified-"
            ) as directory:
                root = Path(directory)
                responses, events = [], []
                def planner(prompt, expected, attachments):
                    responses.append(prompt)
                    return answer_only_response(evidence_refs=refs)
                loop = WebAgentProtocolLoop(
                    root, planner, progress_root=root, max_turns=2,
                    event_sink=lambda event, **kw: events.append((event, kw)),
                )
                with patch.dict(os.environ, {
                    "TRI_ONE_TASK_RESULT_V1": "1", "TRI_ONE_TEXT_ONLY_V1": "1",
                }), patch.object(loop.tools, "execute",
                                 side_effect=AssertionError("no tools allowed")) as execute:
                    answer = loop.run(
                        request, request_id="RR-5A-MARKER",
                        task_id="TASK-5A-MARKER", task_epoch="EPOCH-5A-MARKER",
                        source_tag="REMOTEAGENT_WEBCOPILOT_V9")
                self.assertEqual(answer, "RAII releases owned resources.")
                self.assertEqual(len(responses), 1)
                self.assertEqual(execute.call_count, 0)
                self.assertEqual(loop.action_ledger, {})
                self.assertEqual(loop.action_result_ledger, {})
                self.assertEqual(loop.terminal_acceptance["evidence_refs"], [])
                self.assertEqual(loop.terminal_acceptance["completion_kind"], "ANSWER_ONLY")
                self.assertEqual(loop.terminal_acceptance["evidence_verdict"], "UNVERIFIED")
                classified = [data for name, data in events
                              if name == "tri_one_answer_only_evidence_classified"]
                self.assertEqual(len(classified), 1)
                self.assertEqual(classified[0]["evidence_class"], "RUNTIME_CONTEXT")
                self.assertTrue(classified[0]["normalized"])

    def test_unknown_refs_never_become_answer_only_evidence(self):
        with tempfile.TemporaryDirectory(prefix="goal1-unknown-") as directory:
            root = Path(directory)
            loop = WebAgentProtocolLoop(
                root, lambda *_: answer_only_response(evidence_refs=["ACT-FAKE"]),
                progress_root=root, max_turns=2,
            )
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1", "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                with self.assertRaises(Exception):
                    loop.run("Explain RAII in C++.")
            self.assertFalse(loop._answer_only_admitted)
            self.assertFalse(loop.terminal_acceptance)

    def test_external_facts_never_use_answer_only_terminal(self):
        with tempfile.TemporaryDirectory(prefix="goal1-negative-") as directory:
            root = Path(directory)
            def planner(prompt, expected, attachments):
                return answer_only_response(text="The current value is 123.")
            loop = WebAgentProtocolLoop(root, planner, progress_root=root,
                                        max_turns=2)
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                with self.assertRaises(Exception):
                    loop.run("Explain the latest GitHub status of my project.",
                             source_tag="REMOTEAGENT_WEBCOPILOT_V9")
            self.assertFalse(loop._answer_only_admitted)
            self.assertFalse(loop.terminal_acceptance)
            self.assertEqual(loop.action_result_ledger, {})

    def test_feature_off_keeps_evidence_gate(self):
        with tempfile.TemporaryDirectory(prefix="goal1-feature-off-") as directory:
            root = Path(directory)
            def planner(prompt, expected, attachments):
                return answer_only_response()
            loop = WebAgentProtocolLoop(root, planner, progress_root=root,
                                        max_turns=2)
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "0",
            }):
                with self.assertRaises(Exception):
                    loop.run("Explain RAII in C++.")
            self.assertFalse(loop._answer_only_admitted)
            self.assertFalse(loop.terminal_acceptance)


if __name__ == "__main__":
    unittest.main(verbosity=2)
