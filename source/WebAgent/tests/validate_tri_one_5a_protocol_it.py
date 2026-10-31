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


def answer_only_response(*, text: str = "RAII releases owned resources.", evidence_refs=None) -> str:
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
        "evidence_refs": [] if evidence_refs is None else list(evidence_refs),
        "decision_reason": "原始目標只需要文字說明且未執行工具。",
    }
    final = {"tool": "final_response", "action_id": "A-ANSWER-01",
             "content": text}
    commit = {"tool": "turn_commit", "action_count": 2}
    return "\n".join(fence(x) for x in (progress, final, commit))


class AnswerOnlyV9IT(unittest.TestCase):
    def _loop_case(self, request: str, source_tag: str):
        with tempfile.TemporaryDirectory(prefix="goal1-v9-it-") as directory:
            root = Path(directory)
            turns = []
            def planner(prompt, expected, attachments):
                turns.append((prompt, dict(expected), list(attachments or [])))
                return answer_only_response()
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

    def test_telegram_original_route_candidate(self):
        self._loop_case("Explain RAII in C++.", "REMOTEAGENT_TELEGRAM")

    def test_webcopilot_original_route_candidate(self):
        self._loop_case("Explain RAII in C++.", "REMOTEAGENT_WEBCOPILOT_V9")


    def test_webdirect_no_tools_first_turn_terminal(self):
        self._loop_case(
            "請說明 C++ RAII 的概念，以及 std::unique_ptr 如何實現 RAII。"
            "給出簡短的 C++ 範例。本任務不需要操作本機檔案或執行任何命令。",
            "",
        )

    def test_status_refs_are_not_action_evidence_in_5a(self):
        request = (
            "請說明 C++ RAII 的概念，以及 std::unique_ptr 如何實現 RAII。"
            "給出簡短的 C++ 範例。本任務不需要操作本機檔案或執行任何命令。"
        )
        for refs in (
            ["RUNTIME_STATUS"], ["REQUEST_ACCEPTED"],
            ["REQUEST_ACCEPTED", "RUNTIME_STATUS"],
        ):
            with self.subTest(refs=refs), tempfile.TemporaryDirectory(
                prefix="tri5a-status-"
            ) as directory:
                root = Path(directory)
                turns, events = [], []
                def planner(prompt, expected, attachments):
                    turns.append(prompt)
                    return answer_only_response(evidence_refs=refs)
                loop = WebAgentProtocolLoop(
                    root, planner, progress_root=root, max_turns=2,
                    event_sink=lambda kind, **kw: events.append((kind, kw)),
                )
                flags = {
                    "TRI_ONE_TASK_RESULT_V1": "1",
                    "TRI_ONE_TEXT_ONLY_V1": "1",
                }
                with patch.dict(os.environ, flags), patch.object(
                    loop.tools, "execute",
                    side_effect=AssertionError("status refs must not cause tool I/O"),
                ) as execute:
                    answer = loop.run(request, request_id="WA-5A-STATUS")
                self.assertEqual(answer, "RAII releases owned resources.")
                self.assertEqual(len(turns), 1)
                execute.assert_not_called()
                self.assertEqual(loop.progress_ledger.evidence_refs, [])
                self.assertEqual(loop.action_result_ledger, {})
                self.assertEqual(loop.action_ledger, {})
                self.assertEqual(loop.condition_result_ledger, {})
                receipt = loop.terminal_acceptance
                self.assertEqual(receipt["completion_kind"], "ANSWER_ONLY")
                self.assertEqual(receipt["evidence_refs"], [])
                self.assertEqual(receipt["evidence_verdict"], "UNVERIFIED")
                self.assertEqual(receipt["admission_reason"],
                                 "ANSWER_ONLY_RESPONSE_ADMITTED")
                normalized = [e for kind,e in events
                              if kind == "tri_one_answer_only_evidence_classified"]
                self.assertEqual(len(normalized), 1)
                self.assertEqual(normalized[0]["evidence_class"], "RUNTIME_CONTEXT")
                self.assertEqual(normalized[0]["original_ref_count"], len(refs))
                self.assertTrue(normalized[0]["normalized"])
                self.assertTrue(normalized[0]["clean_runtime"])
                self.assertFalse(any(kind == "tool_started" for kind,_ in events))
                self.assertFalse(any(kind == "protocol_rejected" for kind,_ in events))

    def test_reference_normalization_never_bypasses_existing_gates(self):
        cases = (
            # Model-invented refs must be rejected, never reinterpreted as status.
            ("Explain RAII in C++.", ["UNKNOWN-ACT"], "INVALID_OR_UNTRUSTED"),
            # An action/fresh external task is never a 5A Answer-only candidate.
            ("Read this workspace file then summarize.", ["RUNTIME_STATUS"], None),
        )
        for request, refs, expected_class in cases:
            with self.subTest(request=request), tempfile.TemporaryDirectory(
                prefix="tri5a-reject-"
            ) as directory:
                root = Path(directory)
                events = []
                loop = WebAgentProtocolLoop(
                    root,
                    lambda *_: answer_only_response(evidence_refs=refs),
                    progress_root=root, max_turns=2,
                    event_sink=lambda kind, **kw: events.append((kind, kw)),
                )
                with patch.dict(os.environ, {
                    "TRI_ONE_TASK_RESULT_V1": "1",
                    "TRI_ONE_TEXT_ONLY_V1": "1",
                }), patch.object(
                    loop.tools, "execute",
                    side_effect=AssertionError("rejected task must not run tools"),
                ) as execute:
                    with self.assertRaises(Exception):
                        loop.run(request)
                execute.assert_not_called()
                self.assertFalse(loop._answer_only_admitted)
                self.assertFalse(loop.terminal_acceptance)
                classified = [e for kind,e in events
                              if kind == "tri_one_answer_only_evidence_classified"]
                if expected_class is None:
                    self.assertEqual(classified, [])
                else:
                    self.assertTrue(classified)
                    self.assertEqual(classified[0]["evidence_class"], expected_class)
                    self.assertFalse(classified[0]["normalized"])

    def test_pending_verification_blocks_status_ref_normalization(self):
        with tempfile.TemporaryDirectory(prefix="tri5a-pending-") as directory:
            root = Path(directory)
            events = []
            loop = WebAgentProtocolLoop(
                root, lambda *_: answer_only_response(
                    evidence_refs=["RUNTIME_STATUS"]),
                progress_root=root, max_turns=2,
                event_sink=lambda kind, **kw: events.append((kind, kw)),
            )
            # Inject a real Runtime-owned blocker after request initialization,
            # before admission, without creating fake Action Evidence.
            def planner(prompt, expected, attachments):
                loop.pending_verification_requirement = {
                    "required_action": "run_command",
                    "reason_code": "POSTCONDITION_REQUIRED",
                }
                return answer_only_response(evidence_refs=["RUNTIME_STATUS"])
            loop.planner = planner
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }), patch.object(
                loop.tools, "execute", side_effect=AssertionError("no tools"),
            ) as execute:
                with self.assertRaises(Exception):
                    loop.run("Explain RAII in C++.")
            execute.assert_not_called()
            self.assertFalse(loop._answer_only_admitted)
            self.assertFalse(loop.terminal_acceptance)
            events_classified = [data for kind,data in events
                                 if kind == "tri_one_answer_only_evidence_classified"]
            self.assertTrue(events_classified)
            self.assertFalse(events_classified[0]["normalized"])
            self.assertFalse(events_classified[0]["clean_runtime"])

    def test_tool_filler_is_rejected_without_executing_then_answer_only(self):
        request = (
            "請說明 C++ RAII 的概念，以及 std::unique_ptr 如何實現 RAII。"
            "本任務不需要操作本機檔案或執行任何命令。"
        )
        with tempfile.TemporaryDirectory(prefix="tri5a-blocked-tool-") as directory:
            root = Path(directory)
            turns, events = [], []
            illicit = answer_only_response().replace(
                '"tool":"final_response"', '"tool":"run_command"', 1
            )
            def planner(prompt, expected, attachments):
                turns.append(prompt)
                return illicit if len(turns) == 1 else answer_only_response()
            loop = WebAgentProtocolLoop(
                root, planner, event_sink=lambda event, **kw: events.append(
                    (event, kw)), progress_root=root, max_turns=3,
            )
            with patch.dict(os.environ, {
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }), patch.object(loop.tools, "execute",
                           side_effect=AssertionError("no tool must execute")) as execute:
                answer = loop.run(request, request_id="WA-5A-NOTOOLS")
            self.assertEqual(answer, "RAII releases owned resources.")
            self.assertEqual(len(turns), 2)
            self.assertEqual(execute.call_count, 0)
            self.assertTrue(loop._answer_only_admitted)
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.terminal_acceptance["completion_kind"], "ANSWER_ONLY")
            rejected = [v for e,v in events if e == "protocol_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(any(d.get("reason") == "text_only_tool_forbidden"
                                for d in rejected[0]["diagnostics"]))

    def test_direct_execution_boundary_no_side_effect(self):
        request = "請解釋 RAII。本任務不需要操作本機檔案或執行任何命令。"
        with tempfile.TemporaryDirectory(prefix="tri5a-direct-guard-") as directory:
            root = Path(directory)
            loop = WebAgentProtocolLoop(
                root, lambda *_: "", progress_root=root,
            )
            loop.task_request = request
            loop._answer_only_scope = True
            loop.run_id = "WA-DIRECT-GUARD"
            with patch.object(loop.tools, "execute",
                              side_effect=AssertionError("tool was dispatched")) as execute:
                outcome = loop._execute_action({
                    "tool":"run_command", "action_id":"NO-EXEC",
                    "command":"echo never executed",
                })
            self.assertIn("TEXT_ONLY_TOOL_FORBIDDEN", outcome)
            execute.assert_not_called()
            self.assertFalse(loop.action_ledger)

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
