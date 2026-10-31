"""Tri-One 5A/5B isolated regression: no Browser, Worker or external traffic.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_tri_one_5a_answer_only.py
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.tri_one_answer_only import (
    accept_terminal_candidate, classify_original_request, enabled,
    explicit_tool_restrictions, tool_restriction_diagnostic,
    classify_answer_only_evidence_refs,
)
from agent_core.tri_one_task_result import (
    TaskResultContractError, make_task_result, validate_task_result,
)


def _loop(request: str, **changes):
    values = dict(
        task_request=request, authorized_paths=[], image_delivery_plan={},
        action_ledger={}, action_result_ledger={}, condition_result_ledger={},
        pending_verification_requirement={}, pending_result_recovery={},
        deferred_action_repairs={}, pending_progress_repair_actions=[],
        pending_action_repair={}, active_stage_manifest=None,
        action_loop_state={"blockers":[]}, continuation_source=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _receipt():
    return {
        "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
        "admission_status": "ACCEPTED",
        "request_id": "RR-5A-01", "task_id": "TASK-5A-01", "task_epoch": "EPOCH-5A-01",
        "task_outcome": "SUCCESS", "runtime_state": "COMPLETED",
        "receipt_id": "TERM-FIXTURE-5A", "evidence_refs": [],
        "evidence_verdict": "UNVERIFIED", "completion_kind": "ANSWER_ONLY",
        "admission_reason": "ANSWER_ONLY_RESPONSE_ADMITTED",
    }


def _make(receipt=None, **kwargs):
    args = dict(
        request_id="RR-5A-01", task_id="TASK-5A-01", task_epoch="EPOCH-5A-01",
        intent_digest="fixture-original-request-hash",
        source_interface="WEB_COPILOT",
        trusted_reply_route={"transport": "WEB_COPILOT"},
        state="COMPLETED", outcome="SUCCESS",
        terminal_acceptance=_receipt() if receipt is None else receipt,
        answer_text="RAII binds resources to object lifetime.",
        evidence_refs=[], operational_action_count=0,
    )
    args.update(kwargs)
    with patch.dict(os.environ, {"TRI_ONE_TASK_RESULT_V1":"1", "TRI_ONE_TEXT_ONLY_V1":"1"}):
        return make_task_result(**args)


class AnswerOnlyPolicyTests(unittest.TestCase):
    def test_flag_off_and_per_origin_switch(self):
        self.assertFalse(enabled({}, source="WEB_COPILOT"))
        self.assertTrue(enabled({"TRI_ONE_TEXT_ONLY_V1": "1"}, source="WEB_COPILOT"))
        self.assertFalse(enabled({
            "TRI_ONE_TEXT_ONLY_V1": "1", "TRI_ONE_TEXT_ONLY_TELEGRAM": "off",
        }, source="TELEGRAM"))
        self.assertFalse(enabled({"TRI_ONE_TEXT_ONLY_V1": "1"}, source="LOCAL_AGENT"))

    def test_text_intents_and_negative_origins(self):
        for request in (
            "Explain RAII in C++.",
            "Rewrite this: I has two lens.",
            "Translate the following: A camera uses a shutter.",
            "請解釋 C++ 的 RAII 概念",
            "請翻譯以下文字：相機曝光控制",
        ):
            with self.subTest(request=request):
                self.assertTrue(classify_original_request(request)[0])
        for request in (
            "Explain the latest GitHub status of my repository.",
            "What is the exchange rate today?",
            "What is the status of my project?",
            "Explain the attached document.",
            "Read this workspace file then summarize.",
            "請解釋今天新竹的天氣",
            "請修改這個專案的檔案",
            "I want a PowerPoint presentation.",
            "Hello",
        ):
            with self.subTest(request=request):
                self.assertFalse(classify_original_request(request)[0])
        self.assertFalse(classify_original_request(
            "Explain RAII", attachments=True)[0])
        self.assertFalse(classify_original_request(
            "Explain RAII", authorized_paths=True)[0])
        self.assertFalse(classify_original_request(
            "Explain RAII", continuation=True)[0])

    def test_negative_tool_instruction_is_not_positive_tool_intent(self):
        requests = (
            "請說明 C++ RAII 的概念，以及 std::unique_ptr 如何實現 RAII。"
            "給出簡短的 C++ 範例。本任務不需要操作本機檔案或執行任何命令。",
            "Explain C++ RAII. Do not execute local commands or modify files.",
            "請解釋 C++ RAII。不得使用任何工具。",
        )
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(classify_original_request(request),
                                 (True, "bounded_text_only_intent"))
                self.assertTrue(explicit_tool_restrictions(request)[0])
                self.assertEqual(
                    accept_terminal_candidate(
                        _loop(request),
                        {"decision": "COMPLETE", "outcome": "SUCCESS",
                         "evidence_refs": []},
                        {"tool": "final_response", "content": "RAII owns lifetime."},
                    ),
                    (True, "ANSWER_ONLY_RESPONSE_ADMITTED"),
                )
        # Removing the restriction must not hide positive external intent.
        for request in (
            "請說明目前 Git HEAD，但不要執行命令。",
            "請讀取工作區 README 並解釋，但不要執行命令。",
            "Explain the latest project status. Do not use tools.",
            "請說明 RAII，然後實際執行測試。本任務不需要操作本機檔案或執行任何命令。",
        ):
            with self.subTest(negative=request):
                self.assertFalse(classify_original_request(request)[0])

    def test_original_user_tool_prohibition(self):
        request = "請解釋 C++ RAII。本任務不需要操作本機檔案或執行任何命令。"
        self.assertIsNone(tool_restriction_diagnostic(
            request, [{"tool": "final_response"}]))
        for tool in ("run_command", "read_file", "list_directory",
                     "inspect_project_scope", "observe_fact"):
            with self.subTest(tool=tool):
                denied = tool_restriction_diagnostic(request, [{"tool": tool}])
                self.assertEqual(denied["reason"], "text_only_tool_forbidden")
        # A command-only prohibition is *not* a ban on all read tools.
        request = "請讀取 README，但不要執行命令。"
        self.assertEqual(explicit_tool_restrictions(request), (False, True))
        self.assertIsNone(tool_restriction_diagnostic(
            request, [{"tool": "read_file"}]))
        self.assertIsNotNone(tool_restriction_diagnostic(
            request, [{"tool": "run_command"}]))

    def test_5a_reference_classification_is_type_only_not_pass(self):
        classify = classify_answer_only_evidence_refs
        self.assertEqual(classify([]), "NO_EVIDENCE")
        for refs in (
            ["REQUEST_ACCEPTED"], ["RUNTIME_STATUS"],
            ["REQUEST_ACCEPTED", "RUNTIME_STATUS"],
        ):
            with self.subTest(refs=refs):
                self.assertEqual(classify(refs), "RUNTIME_CONTEXT")
        self.assertEqual(classify(["PROTOCOL_SPEC_REJECTION"]), "PROTOCOL_CONTROL")
        self.assertEqual(
            classify(["REQUEST_ACCEPTED", "ACT-1"],
                     action_result_ids={"ACT-1"}),
            "ACTION_RESULT",
        )
        self.assertEqual(
            classify(["COND-1"], condition_result_ids={"COND-1"}),
            "CONDITION_VERIFICATION",
        )
        for refs in (
            ["ACT-1"], ["ACT-OLD"], ["RUNTIME_STATUS", "ACT-UNKNOWN"],
            ["REQUEST_ACCEPTED", "PROTOCOL_SPEC_REJECTION"],
        ):
            with self.subTest(refs=refs):
                self.assertNotEqual(classify(refs), "RUNTIME_CONTEXT")
        for refs in (["ACT-1"], ["RUNTIME_STATUS", "UNKNOWN"], None, "RUNTIME_STATUS"):
            self.assertEqual(classify(refs), "INVALID_OR_UNTRUSTED")

    def test_5a_gate_still_refuses_unnormalized_action_evidence(self):
        progress = {"decision": "COMPLETE", "outcome": "SUCCESS"}
        final = {"tool": "final_response", "content": "RAII is scope-bound."}
        for refs in (["REQUEST_ACCEPTED"], ["RUNTIME_STATUS"],
                     ["ACT-1"], ["PROTOCOL_SPEC_REJECTION"]):
            with self.subTest(refs=refs):
                self.assertEqual(
                    accept_terminal_candidate(
                        _loop("Explain RAII."), {**progress, "evidence_refs": refs},
                        final,
                    )[1], "action_evidence_cannot_be_answer_only"
                )
        # Only protocol_loop's tightly scoped, clean-runtime normalization
        # may produce [] before this unchanged admission gate is reached.

    def test_terminal_gate_positive_and_no_action_evidence_fabricated(self):
        progress = {"decision":"COMPLETE", "outcome":"SUCCESS", "evidence_refs":[]}
        final = {"tool":"final_response", "content":"RAII releases resources on scope exit."}
        self.assertEqual(
            accept_terminal_candidate(_loop("Explain RAII in C++."), progress, final),
            (True, "ANSWER_ONLY_RESPONSE_ADMITTED"),
        )

    def test_terminal_gate_denies_dirty_or_incomplete_runtime(self):
        progress = {"decision":"COMPLETE", "outcome":"SUCCESS", "evidence_refs":[]}
        final = {"tool":"final_response", "content":"RAII is a language idiom."}
        fixtures = (
            {"action_ledger":{"action":{}}},
            {"action_result_ledger":{"action":{}}},
            {"pending_result_recovery":{"active":True}},
            {"pending_verification_requirement":{"required_action":"observe_fact"}},
            {"action_loop_state":{"blockers":[{"reason":"OPEN_WRITE_TRANSACTION","satisfied":False}]}},
            {"continuation_source":{"task_id":"old"}},
            {"authorized_paths":["/tmp/private-project"]},
            {"image_delivery_plan":{"delivery":"PNG"}},
        )
        for changes in fixtures:
            with self.subTest(changes=changes):
                self.assertFalse(accept_terminal_candidate(
                    _loop("Explain RAII", **changes), progress, final)[0])
        self.assertFalse(accept_terminal_candidate(
            _loop("Explain RAII"), {**progress, "evidence_refs":["ACT-X"]}, final)[0])
        self.assertFalse(accept_terminal_candidate(
            _loop("Explain RAII"), progress,
            {"tool":"final_response","content":"I checked your files."})[0])

    def test_result_roundtrip_and_no_fake_action_pass(self):
        result = _make()
        self.assertEqual(result["completion_kind"], "ANSWER_ONLY")
        self.assertEqual(result["terminal_acceptance"]["evidence_verdict"], "UNVERIFIED")
        self.assertEqual(result["terminal_acceptance"]["admission_reason"],
                         "ANSWER_ONLY_RESPONSE_ADMITTED")
        self.assertEqual(result["operational_action_count"], 0)
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(validate_task_result(result), result)
        self.assertEqual(result["delivery"]["status"], "PENDING")

    def test_refuse_model_forged_result_and_remote_state_claims(self):
        for altered in (
            {"terminal_acceptance":{**_receipt(), "admission_reason":"MODEL_SAID_DONE"}},
            {"terminal_acceptance":{**_receipt(), "receipt_id":""}},
            {"terminal_acceptance":{**_receipt(), "evidence_verdict":"PASS"}},
            {"evidence_refs":["forged-action"]},
            {"operational_action_count":1},
            {"state":"PAUSED", "outcome":"UNKNOWN"},
            {"artifacts":[{"path":"fake.png"}]},
        ):
            with self.subTest(altered=altered):
                with self.assertRaises(TaskResultContractError):
                    _make(**altered)

    def test_no_duplicate_semantic_identity_between_routes(self):
        a = _make()
        b = _make(source_interface="TELEGRAM",
                  trusted_reply_route={"transport":"TELEGRAM"})
        self.assertEqual(a["result_id"], b["result_id"])
        self.assertNotEqual(a["reply_route_id"], b["reply_route_id"])
        self.assertEqual(a["task_state"], b["task_state"])
        self.assertEqual(a["delivery"]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
