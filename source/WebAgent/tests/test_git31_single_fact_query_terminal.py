"""Git31: confirmed negative existence is a successful single fact question.

A fact value is not the status of the tool, Executor or Goal Driver.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.evidence_foundation import observation
from agent_core.tool_result_contract import attach_runtime_result, encode_result
from agent_core.tri_one_runtime_terminal import (
    GoalEvidence, evaluate_goal_evidence, accept_runtime_terminal,
    terminal_verdict_from_loop, _single_fact_query_contract,
)
from agent_core.executor_blackboard import read_executor_terminal
from agent_core.tri_layer_handoff import project_handoff
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json

TARGET = r"E:\SmartAgent_TriLayer_Blackboard_Test_20261109"
GOAL = "這個資料夾存在嗎?\n" + TARGET


def _runtime_fact(action, value, *, status="CONFIRMED", tool_status="SUCCEEDED",
                  source="filesystem.stat", raw_ref=None, freshness=None):
    fact = observation(
        "file.exists", value, source=source, scope=action["path"],
        raw_ref=action["action_id"] if raw_ref is None else raw_ref,
        status=status,
    )
    if freshness is not None:
        fact["freshness"] = freshness
    return attach_runtime_result(
        "{}", tool_status,
        "FACT_OBSERVED" if tool_status == "SUCCEEDED" else "FACT_OBSERVATION_UNKNOWN",
        tool="observe_fact", executed=True,
        requested_path=action["path"], resolved_path=action["path"],
        action_id=action["action_id"],
        data={
            "observations": [fact], "observation_status": status,
            "terminal_eligible": status == "CONFIRMED",
        },
    )


@pytest.mark.parametrize("question,kind", [
    ("這個資料夾存在嗎?\n" + TARGET, "DIRECTORY"),
    (TARGET + "\n請問這個資料夾是否存在？", "DIRECTORY"),
    ("這個目錄存不存在？\n" + TARGET, "DIRECTORY"),
    ("這個檔案存在嗎?\n" + TARGET, "FILE"),
    ("這個路徑是否存在？\n" + TARGET, "PATH"),
])
def test_strict_single_fact_query_grammar(question, kind):
    canonical, actual_kind = _single_fact_query_contract(question, TARGET)
    assert canonical == TARGET.casefold()
    assert actual_kind == kind


@pytest.mark.parametrize("goal", [
    "請檢查資料夾是否存在，不存在就建立\n" + TARGET,
    "這個資料夾存在嗎?\n" + TARGET + "\n如果不存在請建立",
    "這個資料夾存在嗎?\n" + TARGET + "\n執行 pytest",
    "這個資料夾存在嗎?\n" + TARGET + "\n再確認另一個目標",
    "這個資料夾存在嗎?\n" + TARGET + "\n" + r"E:\Other",
    "這個資料夾存在嗎?\n" + TARGET + "\n" + TARGET,
])
def test_compound_or_ambiguous_goal_is_not_auto_terminal(goal):
    assert _single_fact_query_contract(goal, TARGET) == ("", "")


def test_git31_live_negative_fact_auto_finishes_first_tool_and_persists_all_layers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    loop = FakeLoop(tmp_path, GOAL, [
        tool_json("observe_fact", path=TARGET),
        '{"type":"finish","summary":"I should not be used"}',
    ], "")
    def execute(action):
        loop.executed.append(dict(action))
        return _runtime_fact(action, False)
    loop._execute_action = execute
    result = run_minimal_core(loop, GOAL)

    assert "不存在" in result
    assert TARGET in result
    assert "I should not be used" not in result
    assert len(loop.executed) == 1
    assert len(loop.expected_protocols) == 1
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["matched_condition_id"] == "SINGLE_FACT_QUERY"
    assert loop.terminal_acceptance["evidence_verdict"] == "PASS"
    fact = loop.terminal_acceptance["fact_observation"]
    assert fact["fact_field"] == "file.exists"
    assert fact["fact_observed_value"] is False
    assert fact["fact_observation_status"] == "CONFIRMED"
    assert fact["fact_type_verdict"] == "ABSENT"
    assert fact["fact_result_not_mutation"] is True
    assert loop.executor_result["terminal_status"] == "VERIFIED"
    assert loop.executor_result["fact_observation"] == fact
    assert terminal_verdict_from_loop(loop).is_complete

    persisted = read_executor_terminal(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch,
    )
    assert persisted["fact_observation"] == fact
    assert persisted["terminal_status"] == "VERIFIED"
    assert persisted["goal_verified"] is True
    handoff = project_handoff(persisted)
    assert handoff["executor_handoff"]["executor_terminal_status"] == "VERIFIED"
    assert handoff["executor_handoff"]["executor_fact_observation"] == fact
    assert handoff["prerequisite_handoff"]["prerequisite_readiness_verdict"] == "NOT_EVALUATED"
    assert handoff["goal_stage_handoff"]["goal_stage_verification_verdict"] == "NOT_EVALUATED"
    assert handoff["stage_transition_committed"] is False


def test_true_directory_stat_does_not_prove_directory_type(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    loop = FakeLoop(tmp_path, GOAL, [
        tool_json("observe_fact", path=TARGET),
        '{"type":"finish","summary":"是資料夾"}',
    ], "")
    def execute(action):
        loop.executed.append(dict(action))
        return _runtime_fact(action, True)
    loop._execute_action = execute
    message = run_minimal_core(loop, GOAL)
    assert "尚無足夠證據" in message
    assert loop.terminal_outcome == "UNKNOWN"  # Goal not accepted; Executor still 3-state.
    assert loop.terminal_acceptance == {}
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.executor_result["goal_verified"] is False


def test_positive_generic_path_query_completes_without_claiming_directory_kind(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    request = "這個路徑存在嗎？\n" + TARGET
    loop = FakeLoop(tmp_path, request, [
        tool_json("observe_fact", path=TARGET),
    ], "")
    loop._execute_action = lambda action: _runtime_fact(action, True)
    answer = run_minimal_core(loop, request)
    assert "路徑存在" in answer
    assert "未驗證物件類型" in answer
    assert loop.terminal_acceptance["fact_observation"]["fact_observed_value"] is True
    assert loop.terminal_acceptance["fact_observation"]["fact_requested_kind"] == "PATH"
    assert loop.executor_result["terminal_status"] == "VERIFIED"


def test_positive_directory_requires_independent_typed_read_before_final_fact():
    from agent_core.tool_result_contract import decode_result
    typed_action = {
        "tool": "list_directory", "path": TARGET, "action_id": "A-DIR-TYPE",
    }
    fact_action = {
        "tool": "observe_fact", "path": TARGET, "action_id": "A-DIR-STAT",
    }
    typed = decode_result(encode_result(
        "SUCCEEDED", "DIRECTORY_LISTED",
        tool="list_directory", executed=True,
        requested_path=TARGET, resolved_path=TARGET,
        data={"terminal_eligible": True, "entries": []},
    ))
    fact = decode_result(_runtime_fact(fact_action, True))
    proof = evaluate_goal_evidence(GOAL, [
        {"action": typed_action, "receipt": typed},
        {"action": fact_action, "receipt": fact},
    ])
    assert proof.verified
    assert proof.goal_kind == "SINGLE_FACT_QUERY"
    assert proof.reason == "SINGLE_FACT_QUERY_DIRECTORY_PRESENT_CONFIRMED"
    assert proof.evidence_refs == ("A-DIR-TYPE", "A-DIR-STAT")
    # A model finish cannot upgrade file.exists=True to directory kind on
    # its own; typed list evidence is independently required.


def test_positive_file_query_does_not_assume_existing_directory_is_file():
    target = TARGET
    request = "這個檔案存在嗎?\n" + target
    action = {"tool": "observe_fact", "path": target, "action_id": "A-FACT"}
    from agent_core.tool_result_contract import decode_result
    receipt = decode_result(_runtime_fact(action, True))
    proof = evaluate_goal_evidence(request, [{"action": action, "receipt": receipt}])
    assert not proof.verified
    assert proof.reason != "SINGLE_FACT_QUERY_PATH_PRESENT_CONFIRMED"


@pytest.mark.parametrize("case", ["foreign_raw_ref", "stale", "unknown", "rejected", "wrong_path"])
def test_stale_rejected_or_wrong_identity_fact_never_completes(case):
    request = GOAL
    action = {"tool": "observe_fact", "path": TARGET, "action_id": "A-FACT"}
    if case == "wrong_path":
        action = {**action, "path": r"E:\Other"}
    output = _runtime_fact(
        action, False,
        raw_ref="A-FOREIGN" if case == "foreign_raw_ref" else None,
        freshness="STALE" if case == "stale" else None,
        status="UNKNOWN" if case == "unknown" else "CONFIRMED",
        tool_status="REJECTED" if case == "rejected" else "SUCCEEDED",
    )
    from agent_core.tool_result_contract import decode_result
    proof = evaluate_goal_evidence(request, [
        {"action": action, "receipt": decode_result(output)},
    ])
    assert proof.status != "PASS"


def test_replay_rejects_changed_tool_bytes_after_valid_proof(tmp_path):
    action = {"tool": "observe_fact", "path": TARGET, "action_id": "A-FACT"}
    from agent_core.tool_result_contract import decode_result
    raw = _runtime_fact(action, False)
    receipt = decode_result(raw)
    proof = evaluate_goal_evidence(GOAL, [{"action": action, "receipt": receipt}])
    assert proof.verified and proof.reason == "SINGLE_FACT_QUERY_ABSENCE_CONFIRMED"

    import hashlib
    loop = SimpleNamespace(
        task_id="TASK-GIT31", run_id="RR-GIT31", task_epoch="EP-GIT31",
        turn_id=1, task_request=GOAL,
        _canonical_action_proposals={"A-FACT": action},
        action_result_ledger={
            "A-FACT": {
                "request_id": "RR-GIT31", "task_id": "TASK-GIT31",
                "task_epoch": "EP-GIT31", "executed": True,
                "result_status": "SUCCEEDED", "evidence_status": "ACTIVE",
                "result": raw, "result_sha256": "0"*64,
            },
        },
    )
    with pytest.raises(ValueError, match="digest_mismatch"):
        accept_runtime_terminal(loop, proof)


def test_unverified_prose_never_completes_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    loop = FakeLoop(tmp_path, GOAL, [
        '{"type":"finish","summary":"不存在，已經確認"}',
    ], "")
    text = run_minimal_core(loop, GOAL)
    assert "尚無足夠證據" in text
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.terminal_acceptance == {}
