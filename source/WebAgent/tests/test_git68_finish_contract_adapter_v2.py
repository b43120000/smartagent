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
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, allow_open=True,
        canonical_finish=canonical,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] in {
        "MODEL_FINISH_GOAL_FACT_NOT_DECLARED", "MODEL_GOAL_CHECKS_REQUIRED",
    }


def test_v2_never_overrides_runtime_mismatch(tmp_path, monkeypatch):
    loop, goal, board, canonical, checks, refs = _fixture(monkeypatch)
    wrong = [dict(checks[0], expected=False)]
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=wrong,
        allow_open=True, canonical_finish=canonical,
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
