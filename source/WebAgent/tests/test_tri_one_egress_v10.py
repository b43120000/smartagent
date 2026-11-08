"""Tri-One identical terminal semantics over Telegram, Local Test, WebCopilot."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.tri_one_runtime_terminal import (
    GoalEvidence, accept_runtime_terminal, terminal_verdict_from_loop,
)
from agent_core.tri_one_task_result import (
    make_task_result_from_remote_event, validate_task_result,
)

SOURCES = ("TELEGRAM", "LOCAL_TEST", "WEBGPT_COPILOT")


def terminal_task(transport: str, *, verified: bool):
    request = "RR-TRI-ONE-SHARED"
    task_id = "TASK-TRI-ONE-SHARED"
    epoch = "TRI-EPOCH-1"
    action_id = "MIN-ACTION-1"
    loop = SimpleNamespace(
        run_id=request, task_id=task_id, task_epoch=epoch, turn_id=1,
        terminal_outcome="UNKNOWN", terminal_acceptance={}, protocol_state="OBSERVED",
        action_result_ledger={
            action_id: {
                "request_id": request, "task_id": task_id, "task_epoch": epoch,
                "result_status": "SUCCEEDED", "executed": True,
                "evidence_status": "ACTIVE",
            },
        },
    )
    if verified:
        proof = GoalEvidence("PASS", "DIRECTORY_CREATED_AND_CONFIRMED",
                             (action_id,), r"E:\Example", "SINGLE_CREATE_DIRECTORY")
        receipt = accept_runtime_terminal(loop, proof)
    else:
        receipt = {}
    verdict = terminal_verdict_from_loop(loop, minimal=True)
    route = {"transport": transport, "endpoint": "opaque-not-exported"}
    key = "final" if verdict.is_complete else "paused"
    task = SimpleNamespace(
        task_id=task_id, request_id=request, task_epoch=epoch,
        state=verdict.state,
        completed_at=1700000000 if verdict.is_complete else None,
        reply_route=route,
        metadata={"transport": transport, "reply_route": route,
                  "intent_digest": "f" * 64},
        result_ledger={
            key: {
                "response": "已建立並確認資料夾",
                "intent_digest": "f" * 64,
                "task_outcome": verdict.outcome,
                "terminal_acceptance": receipt,
                "action_result_ledger": loop.action_result_ledger,
                "outbound_artifacts": [],
            },
        },
        action_ledger={action_id: True},
    )
    event = "TASK_COMPLETED" if verdict.is_complete else "TASK_PAUSED"
    return task, event, verdict


@pytest.mark.parametrize("transport", SOURCES)
def test_all_shared_worker_origins_produce_same_verified_v1(transport):
    task, event, verdict = terminal_task(transport, verified=True)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "已建立並確認資料夾"})
    assert result["task_state"] == verdict.state == "COMPLETED"
    assert result["outcome"] == verdict.outcome == "SUCCESS"
    assert result["completion_kind"] == "EVIDENCE_BACKED"
    assert result["terminal_acceptance"]["status"] == "ACCEPTED"
    assert result["evidence_refs"] == ["MIN-ACTION-1"]
    assert result["answer_text"] == "已建立並確認資料夾"
    assert "endpoint" not in result
    validate_task_result(result)


@pytest.mark.parametrize("transport", SOURCES)
def test_all_shared_worker_origins_preserve_paused_without_receipt(transport):
    task, event, verdict = terminal_task(transport, verified=False)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "尚未通過驗收"})
    assert result["task_state"] == verdict.state == "PAUSED"
    assert result["outcome"] == verdict.outcome == "UNKNOWN"
    assert result["terminal_acceptance"]["status"] == "MISSING"
    assert result["completion_kind"] == "LEGACY_UNVERIFIED"
    validate_task_result(result)


def test_all_origins_same_evidence_and_outcome_except_source_route():
    results = []
    for transport in SOURCES:
        task, event, _ = terminal_task(transport, verified=True)
        results.append(make_task_result_from_remote_event(
            task, event, {"summary": "已建立並確認資料夾"}))
    fields = ("task_id", "request_id", "task_epoch", "task_state",
              "outcome", "completion_kind", "answer_text", "evidence_refs")
    assert all(tuple(record[field] for field in fields) ==
               tuple(results[0][field] for field in fields)
               for record in results[1:])
