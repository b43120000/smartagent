"""Git68 Finish V2: 65/91/256 real ledger readbacks and fail-closed checks."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from WebAgent.minimal_decision import (
    MinimalDecisionError, parse_minimal_decision,
)
from agent_core.finish_contract_adapter_v2 import (
    adapt_finish_contract_v2, validate_finish_contract_v2,
)
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement
from agent_core.requirement_goal_state import compile_requirement_goal_state
from agent_core.runtime_finish_comparator import compare_runtime_finish
from agent_core.tool_result_contract import encode_result


def _fixture(monkeypatch, count=91, *, broken_index=None):
    from agent_core import runtime_finish_comparator as comparator

    goal = "請執行多步 Fixture Reset，驗證所有 Runtime Evidence"
    contract = compile_requirement_goal_state(goal)
    assert contract["status"] == "UNCOMPILED"
    loop = SimpleNamespace(
        task_id="TASK-GIT68-TEST", run_id="RR-GIT68-TEST",
        task_epoch="epoch-git68", action_result_ledger={},
        _canonical_action_proposals={},
    )
    events, refs = [], []
    for index in range(count):
        ref = "MIN-EV-" + format(index + 1, "024x")
        action_id = "MIN-" + format(index + 1, "024X")
        action = {
            "action_id": action_id, "tool": "inspect_directory",
            "paths": ["E:\\\\AIspace\\\\case_a"],
        }
        raw = encode_result(
            "SUCCEEDED", "DIRECTORY_INSPECTED", tool="inspect_directory",
            executed=True,
            data={"observed_exists": True, "observation_status": "CONFIRMED"},
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if index == broken_index:
            digest = "f" * 64
        loop._canonical_action_proposals[action_id] = action
        loop.action_result_ledger[action_id] = {
            "action_id": action_id, "task_id": loop.task_id,
            "request_id": loop.run_id, "task_epoch": loop.task_epoch,
            "result_id": ref, "result": raw, "result_sha256": digest,
            "result_status": "SUCCEEDED", "executed": True,
            "evidence_status": "ACTIVE",
        }
        events.append({
            "kind": "TOOL_RESULT", "action_id": action_id,
            "toolName": "inspect_directory", "runtimeEvidence": {
                "evidence_ref": ref, "status": "SUCCEEDED",
                "executed": True, "result_sha256": digest,
            }, "toolResult": {"isSuccess": True},
        })
        refs.append(ref)

    board = {
        "events": events, "event_count_total": len(events),
        "request_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        "goal_state_contract": contract,
        "evidence_requirement": assess_finish_evidence_requirement(
            goal, authorized_paths=True,
        ),
        "terminal": {"tool_evidence_refs": refs},
    }
    monkeypatch.setattr(comparator, "read_executor_run", lambda *a, **k: board)
    check = {
        "criterion": "Fixture 檔案存在性已確認",
        "evidence_ref": refs[-1],
        "field": "data.observed_exists", "operator": "eq", "expected": True,
    }
    decision = parse_minimal_decision(json.dumps({
        "type": "finish", "contract_version": 2,
        "summary": "已完成唯讀確認，提交 Runtime 終態驗收",
        "goal_checks": [check],
    }, ensure_ascii=False))
    canonical = adapt_finish_contract_v2(
        decision, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch, frozen_goal_contract=contract,
    )
    return loop, goal, board, canonical, [check], refs


@pytest.mark.parametrize("count", [65, 91, 256])
def test_v2_full_ledger_scales_and_reads_all_evidence(tmp_path, monkeypatch, count):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch, count)
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=checks,
        allow_open=True, answer=canonical["planner_intent"]["summary"],
        canonical_finish=canonical,
    )
    assert result["verdict"] == "PASS", result
    assert len(result["tool_results"]) == count
    assert result["evidence_refs"] == refs
    assert result["coverage_basis"] == "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH"


def test_legacy_v1_still_rejects_history_beyond_64(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch, 91)
    result = compare_runtime_finish(
        loop, tmp_path, goal, [], goal_checks=checks, allow_open=True,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "EVIDENCE_MISSING_OR_EXCESSIVE"


def test_v2_does_not_accept_missing_goal_checks(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch)
    # Proposal and comparator arguments must represent the SAME submitted Finish.
    canonical["planner_intent"]["goal_checks"] = None
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, allow_open=True,
        answer=canonical["planner_intent"]["summary"],
        canonical_finish=canonical,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] in {
        "MODEL_FINISH_GOAL_FACT_NOT_DECLARED", "MODEL_GOAL_CHECKS_REQUIRED",
    }


def test_v2_never_overrides_runtime_mismatch(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch)
    wrong = [dict(checks[0], expected=False)]
    canonical["planner_intent"]["goal_checks"] = wrong
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=wrong,
        allow_open=True, answer=canonical["planner_intent"]["summary"],
        canonical_finish=canonical,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "GOAL_CHECK_RUNTIME_MISMATCH"


def test_v2_fails_on_tampered_ledger_digest(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(
        monkeypatch, 91, broken_index=17,
    )
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=checks,
        allow_open=True, canonical_finish=canonical,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "EVIDENCE_DIGEST_MISMATCH"


def test_canonical_finish_task_binding_is_immutable(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch)
    assert validate_finish_contract_v2(canonical, loop, board)
    forged = {**canonical, "task_binding": {
        **canonical["task_binding"], "request_id": "RR-FORGED",
    }}
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=checks,
        allow_open=True, canonical_finish=forged,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "CANONICAL_FINISH_BINDING_INVALID"


def test_v2_parser_and_legacy_parser_fails_closed():
    legacy = parse_minimal_decision('{"type":"finish","summary":"legacy"}')
    assert legacy.contract_version == 1
    v2 = parse_minimal_decision(
        '{"type":"finish","contract_version":2,"summary":"done"}')
    assert v2.contract_version == 2
    assert v2.expected_runtime is None
    with pytest.raises(MinimalDecisionError, match="finish_v2_legacy_claims_forbidden"):
        parse_minimal_decision(json.dumps({
            "type": "finish", "contract_version": 2, "summary": "done",
            "expected_runtime": [{"evidence_ref": "MIN-EV-x", "fields": {}}],
        }))
    with pytest.raises(MinimalDecisionError, match="finish_contract_version_invalid"):
        parse_minimal_decision('{"type":"finish","contract_version":3,"summary":"done"}')


def test_v2_rejects_mismatched_proposal_arguments(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch)
    wrong = [dict(checks[0], expected=False)]
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=wrong,
        allow_open=True, canonical_finish=canonical,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "CANONICAL_FINISH_PROPOSAL_MISMATCH"


def test_v1_small_finish_still_passes_with_exact_claims(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch, 1)
    expected_runtime = [{
        "evidence_ref": refs[0],
        "fields": {
            "status": "SUCCEEDED",
            "data.observed_exists": True,
        },
    }]
    result = compare_runtime_finish(
        loop, tmp_path, goal, expected_runtime,
        goal_checks=checks, allow_open=True,
    )
    assert result["verdict"] == "PASS", result
    assert result["evidence_refs"] == refs


def test_v1_small_finish_wrong_claim_fails(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch, 1)
    expected_runtime = [{
        "evidence_ref": refs[0],
        "fields": {
            "status": "SUCCEEDED",
            "data.observed_exists": False,
        },
    }]
    result = compare_runtime_finish(
        loop, tmp_path, goal, expected_runtime,
        goal_checks=checks, allow_open=True,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "MODEL_RUNTIME_MISMATCH"


def test_real_executor_loop_v2_produces_comparator_receipt(tmp_path, monkeypatch):
    """Real Planner -> Executor -> Adapter -> Comparator -> Terminal; fake OS only."""
    from WebAgent.pokeclaw_executor_loop import run_pokeclaw_executor
    from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
    from WebAgent.tests.test_minimal_core_v10 import tool_json
    from agent_core.executor_run_blackboard import read_executor_run

    request = "檢視執行紀錄，依實際程序退出碼確認是否符合本次檢查目標"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.assess",
        lambda *a, **kw: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **kw: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    actions = []

    def execute(action):
        actions.append(dict(action))
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
            data={"process_state": "EXITED", "exit_code": 0,
                  "stdout": "", "stderr": "", "timed_out": False,
                  "verification_status": "PASS"},
        )

    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json(
                "run_command", command="git status --short",
                operation="GIT_INSPECT",
            )
        recent = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1].split("\n", 1)[0]
        )
        ref = recent[-1]["evidence_ref"]
        return json.dumps({
            "type": "finish", "contract_version": 2,
            "summary": "已由 Runtime 確認 exit_code 為 0",
            "goal_checks": [{
                "criterion": "實際程序已正常退出",
                "evidence_ref": ref, "field": "data.exit_code",
                "operator": "eq", "expected": 0,
            }],
        }, ensure_ascii=False)

    loop.planner = planner
    reply = run_pokeclaw_executor(loop, request)
    board = read_executor_run(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch,
    )
    assert len(actions) == 1
    assert loop.terminal_outcome == "SUCCESS", reply
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert loop.terminal_acceptance["receipt_id"].startswith("TERM-")
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    proposals = [event for event in board["events"]
                 if event["kind"] == "MODEL_FINISH_PROPOSAL"]
    assert len(proposals) == 1
    assert proposals[0]["contract_version"] == 2
    assert proposals[0]["canonical_finish_schema"] == (
        "SMARTAGENT_CANONICAL_FINISH_CONTRACT_V2"
    )
