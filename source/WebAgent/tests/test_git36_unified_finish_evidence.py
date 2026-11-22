"""Git36: one Finish, upstream minimum evidence, text/tool/mixed receipts."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core.executor_run_blackboard import read_executor_run
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement
from agent_core.tool_result_contract import encode_result
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from agent_core.tri_one_task_result import (
    make_task_result_from_remote_event, make_task_result_from_webagent_loop,
    validate_task_result,
)
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET
from WebAgent.tests.test_minimal_core_v10 import tool_json


def _no_tool(tmp_path, monkeypatch, *, goal: str, answer: str,
             expected_runtime=None, goal_checks=None):
    finish = {"type": "finish", "summary": answer}
    if expected_runtime is not None:
        finish["expected_runtime"] = expected_runtime
    if goal_checks is not None:
        finish["goal_checks"] = goal_checks
    loop, state = _loop(
        tmp_path, monkeypatch,
        [json.dumps(finish, ensure_ascii=False)], request=goal,
    )
    reply = run_minimal_core(loop, goal)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    return loop, state, reply, board


@pytest.mark.parametrize("goal,answer", [
    ("你好 說個雨天的笑話", "雨傘說我最近心情很潮。"),
    ("請翻譯以下文字：Hello world", "你好，世界。"),
    ("你好", "你好！"),
    ("請幫我寫一段生日祝福", "生日快樂，天天開心！"),
])
def test_text_only_finishes_without_tool_result(tmp_path, monkeypatch, goal, answer):
    loop, state, reply, board = _no_tool(
        tmp_path, monkeypatch, goal=goal, answer=answer,
        expected_runtime=[],
    )
    assert len(loop.executed) == 0
    assert state["mutations"] == 0
    assert reply == answer
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert loop.terminal_acceptance["completion_kind"] == "ANSWER_ONLY"
    assert loop.terminal_acceptance["evidence_verdict"] == "PASS"
    assert loop.terminal_acceptance["evidence_refs"] == []
    assert loop.terminal_acceptance["goal_coverage_basis"] == "RUNTIME_TEXT_ADMISSION"
    assert loop.terminal_acceptance["answer_sha256"] == hashlib.sha256(answer.encode()).hexdigest()
    assert board["evidence_requirement"]["answer_only_eligible"] is True
    assert board["evidence_requirement"]["tool_evidence_required"] is False
    assert board["terminal"]["finish_comparison"]["completion_kind"] == "ANSWER_ONLY"
    assert board["terminal"]["goal_verified"] is True
    assert terminal_verdict_from_loop(loop).outcome == "SUCCESS"


def test_text_only_task_result_and_runtime_terminal_are_same_contract(tmp_path, monkeypatch):
    goal = "你好 說個雨天的笑話"
    answer = "雨傘說：今天又被雨淋得很有感。"
    loop, _, reply, board = _no_tool(
        tmp_path, monkeypatch, goal=goal, answer=answer,
        expected_runtime=[],
    )
    loop.intent_digest = "a" * 64
    standalone = make_task_result_from_webagent_loop(
        loop, reply, source_interface="WEB_DIRECT",
    )
    assert standalone["outcome"] == "SUCCESS"
    assert standalone["final_verdict"] == "PASS"
    assert standalone["completion_kind"] == "ANSWER_ONLY"
    assert standalone["evidence_refs"] == []
    assert standalone["terminal_acceptance"]["evidence_verdict"] == "PASS"
    assert standalone["goal_acceptance"]["coverage_basis"] == "RUNTIME_TEXT_ADMISSION"
    validate_task_result(standalone)

    task = SimpleNamespace(
        request_id=loop.run_id, task_id=loop.task_id, task_epoch=loop.task_epoch,
        state="COMPLETED", completed_at=1700000000,
        reply_route={"transport": "TELEGRAM", "endpoint": "private"},
        metadata={"transport": "TELEGRAM", "intent_digest": "a" * 64},
        result_ledger={"final": {
            "response": reply, "intent_digest": "a" * 64,
            "task_outcome": "SUCCESS",
            "terminal_acceptance": loop.terminal_acceptance,
            "action_result_ledger": {},
        }},
        action_ledger={},
    )
    remote = make_task_result_from_remote_event(
        task, "TASK_COMPLETED", {"summary": reply},
    )
    assert remote["completion_kind"] == "ANSWER_ONLY"
    assert remote["final_verdict"] == "PASS"
    assert remote["goal_acceptance"]["coverage_basis"] == "RUNTIME_TEXT_ADMISSION"
    assert remote["source_interface"] == "TELEGRAM"
    validate_task_result(remote)


@pytest.mark.parametrize("goal", [
    "請建立資料夾 " + TARGET,
    "請確認這個資料夾是否存在 " + TARGET,
    "你好，說個笑話，並建立資料夾 " + TARGET,
    "現在查詢台北即時天氣",
])
def test_tool_required_request_cannot_pass_with_text_only(tmp_path, monkeypatch, goal):
    loop, state, _, board = _no_tool(
        tmp_path, monkeypatch, goal=goal, answer="已經完成了",
    )
    assert len(loop.executed) == 0
    assert loop.terminal_outcome == "FAILED"
    assert loop.terminal_acceptance["final_verdict"] == "FAIL"
    assert board["evidence_requirement"]["tool_evidence_required"] is True
    assert board["terminal"]["finish_comparison"]["reason_code"] == "TOOL_EVIDENCE_REQUIRED"


def test_text_finish_with_fake_tool_claim_fails(tmp_path, monkeypatch):
    loop, _, _, board = _no_tool(
        tmp_path, monkeypatch,
        goal="說個笑話",
        answer="這個笑話很有趣",
        expected_runtime=[{"evidence_ref": "MIN-EV-FAKE", "fields": {"status": "SUCCEEDED"}}],
    )
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == (
        "TEXT_ONLY_FINISH_HAS_UNBACKED_TOOL_CLAIMS"
    )


def test_unverified_execution_claim_is_not_text_pass(tmp_path, monkeypatch):
    loop, _, _, board = _no_tool(
        tmp_path, monkeypatch,
        goal="說個笑話",
        answer="已執行命令並完成所有資料修改。",
    )
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"].startswith(
        "ANSWER_ONLY_ADMISSION_REJECTED:"
    )


def test_mixed_task_tool_evidence_and_text_share_one_finish(tmp_path, monkeypatch):
    request = "請檢查資料夾 " + TARGET + " 並用一句話報告"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    action_calls = []
    def execute(action):
        action_calls.append(dict(action))
        return encode_result(
            "SUCCEEDED", "DIRECTORY_INSPECTED", tool="inspect_directory",
            executed=True, requested_path=TARGET, resolved_path=TARGET,
            data={
                "observed_exists": True,
                "observed_is_directory": True,
                "observation_status": "CONFIRMED",
            },
        )
    loop._execute_action = execute
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json("inspect_directory", paths=[TARGET], recursive=False)
        import json as _json
        result = _json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1].split("\n", 1)[0]
        )
        evidence_ref = result[-1]["evidence_ref"]
        return _json.dumps({
            "type": "finish", "summary": "資料夾存在且是目錄。",
            "expected_runtime": [{
                "evidence_ref": evidence_ref,
                "fields": {"status": "SUCCEEDED", "data.observed_exists": True},
            }],
            "goal_checks": [{
                "criterion": "目標資料夾應存在",
                "evidence_ref": evidence_ref,
                "field": "data.observed_exists", "operator": "eq", "expected": True,
            }],
        }, ensure_ascii=False)
    loop.planner = planner
    reply = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(action_calls) == 1
    assert board["evidence_requirement"]["tool_evidence_required"] is True
    assert board["terminal"]["finish_comparison"]["completion_kind"] == "EVIDENCE_BACKED"
    assert board["terminal"]["finish_comparison"]["coverage_basis"] == (
        "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH"
    )
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert loop.terminal_acceptance["evidence_refs"]
    assert "資料夾存在" in reply
    assert "Runtime 最終驗收：PASS" in reply


def test_evidence_requirement_is_non_authorizing(tmp_path):
    item = assess_finish_evidence_requirement("請建立資料夾 " + TARGET)
    assert item["tool_evidence_required"] is True
    assert item["answer_only_eligible"] is False
    assert item["authorizes_operation"] is False
    text = assess_finish_evidence_requirement("你好 說個雨天的笑話")
    assert text["answer_only_eligible"] is True
    assert text["authorizes_operation"] is False
