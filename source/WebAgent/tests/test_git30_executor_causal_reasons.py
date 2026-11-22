"""Git30: Runtime cause and observed evidence survive all Executor exits."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_core.action_readiness import classify_scope_preflight
from agent_core.executor_blackboard import (
    publish_executor_terminal, read_executor_terminal,
)
from agent_core.tri_layer_handoff import project_handoff
from agent_core.tool_result_contract import attach_runtime_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.minimal_decision import MinimalDecision
from WebAgent.minimal_admission import admit_minimal_action
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json


@pytest.mark.parametrize("original,reason,stage,transition", [
    (
        "directory_target_not_exact_original_request:E:\\Project",
        "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST",
        "ORIGINAL_REQUEST_BINDING", "ACTION_REPAIR",
    ),
    (
        "directory_parent_not_existing_directory:E:\\Missing",
        "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY",
        "PARENT_DIRECTORY", "PREREQUISITE_DISCOVERY",
    ),
    (
        "directory_parent_not_a_directory:E:\\not_dir",
        "DIRECTORY_PARENT_NOT_A_DIRECTORY",
        "PARENT_DIRECTORY", "PREREQUISITE_DISCOVERY",
    ),
    (
        "path_outside_authorized_roots:E:\\Project",
        "PATH_OUTSIDE_AUTHORIZED_ROOTS",
        "WRITE_AUTHORIZATION", "REVIEW_AUTHORIZATION",
    ),
    (
        "reparse_or_symlink_forbidden:E:\\Symbolic",
        "REPARSE_OR_SYMLINK_FORBIDDEN",
        "WRITE_AUTHORIZATION", "REVIEW_AUTHORIZATION",
    ),
    (
        "request_write_ceiling_unresolved",
        "REQUEST_WRITE_CEILING_UNRESOLVED",
        "REQUEST_WRITE_CEILING", "REVIEW_WRITE_CEILING",
    ),
    (
        "unmapped_rejection_that_must_fail_closed:E:\\No",
        "UNMAPPED_SCOPE_PREFLIGHT_CAUSE",
        "UNCLASSIFIED_PREFLIGHT", "RUNTIME_CAUSAL_REVIEW",
    ),
])
def test_preflight_provides_exact_sanitized_cause(original, reason, stage, transition):
    diag = classify_scope_preflight("create_directory", original, allowed=False)
    assert diag["scope_allowed"] is False
    assert diag["reason_code"] == reason
    assert diag["failure_stage"] == stage
    assert diag["eligible_transition"] == transition
    assert "OTHER_SCOPE_REJECTION" not in str(diag)
    assert "E:\\Project" not in str(diag)
    assert len(diag["cause_digest"]) == 64
    assert diag["filesystem_absence_verified"] is False
    assert diag["authorization_granted"] is False


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows security policy")
def test_real_scope_preflight_distinguishes_three_directory_gates(tmp_path):
    from WebAgent.tool_context import WebAgentToolContext

    target = tmp_path / "valid_new_directory"
    ctx = WebAgentToolContext(tmp_path)
    ctx.begin_run("RR-GIT30", "請建立 " + str(target), [str(target)])
    good = ctx.preview_tool_scope({"tool": "create_directory", "path": str(target)})
    assert good["allowed"], good
    proof = good["directory_preconditions"]
    assert proof["write_authorization_verdict"] == "AUTHORIZED_BY_EXISTING_POLICY"
    assert proof["target_binding_verdict"] == "EXACT_ORIGINAL_REQUEST_TARGET"
    assert proof["parent_directory_verdict"] == "EXISTS_AND_IS_DIRECTORY"
    assert not target.exists()  # Preview never mutated.

    sibling = tmp_path / "wrong_sibling"
    wrong = ctx.preview_tool_scope({"tool": "create_directory", "path": str(sibling)})
    assert wrong["allowed"] is False
    assert (wrong["scope_diagnostic"]["reason_code"]
            == "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST")
    assert wrong["scope_diagnostic"]["failure_stage"] == "ORIGINAL_REQUEST_BINDING"
    assert wrong["directory_preconditions"]["write_authorization_verdict"] == "AUTHORIZED_BY_EXISTING_POLICY"

    missing_parent_target = tmp_path / "missing_parent" / "child"
    ctx.begin_run(
        "RR-GIT30-MISSING", "請建立 " + str(missing_parent_target),
        [str(missing_parent_target)],
    )
    absent = ctx.preview_tool_scope({
        "tool": "create_directory", "path": str(missing_parent_target),
    })
    assert absent["allowed"] is False
    assert (absent["scope_diagnostic"]["reason_code"]
            == "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY")
    assert absent["scope_diagnostic"]["failure_stage"] == "PARENT_DIRECTORY"
    assert absent["directory_preconditions"]["parent_directory_verdict"] == "MISSING"

    outside_target = tmp_path.parent / "outside_writer"
    ctx.begin_run("RR-GIT30-OUTSIDE", "請建立 " + str(outside_target),
                  [str(outside_target)])
    unauthorized = ctx.preview_tool_scope({
        "tool": "create_directory", "path": str(outside_target),
    })
    assert unauthorized["allowed"] is False
    assert (unauthorized["scope_diagnostic"]["reason_code"]
            == "PATH_OUTSIDE_AUTHORIZED_ROOTS")
    assert unauthorized["directory_preconditions"]["write_authorization_verdict"] == "REJECTED_OR_UNRESOLVED"
    assert not outside_target.exists()


def test_executor_blocked_retains_actual_absence_and_separate_denied_admission(
    tmp_path, monkeypatch
):
    from agent_core.evidence_foundation import observation
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    target = r"E:\Git30_Executor_Trace_Case"
    goal = (
        "請檢查 " + target
        + " 是否存在；如果不存在就建立資料夾，然後驗證並回報 Blackboard。"
    )
    loop = FakeLoop(tmp_path, goal, [
        tool_json("observe_fact", path=target),
        tool_json("create_directory", path=target),
        '{"type":"blocked","reason":"Runtime denied creation"}',
    ], "")
    def preview(action):
        if action["tool"] != "create_directory":
            return {"allowed": True}
        from agent_core.action_readiness import classify_scope_preflight
        diag = classify_scope_preflight(
            "create_directory",
            "directory_target_not_exact_original_request:E:\\WrongPath",
            allowed=False,
        )
        return {
            "allowed": False, "scope_diagnostic": diag,
            "directory_preconditions": {
                "write_authorization_verdict": "AUTHORIZED_BY_EXISTING_POLICY",
                "target_binding_verdict": "TARGET_NOT_EXACT",
                "parent_directory_verdict": "NOT_EVALUATED",
            },
        }
    loop.tools.preview_tool_scope = preview

    def actual_tool(action):
        if action["tool"] != "observe_fact":
            raise AssertionError("Mutation was denied; no mutation may execute")
        loop.executed.append(action)
        fact = observation(
            "file.exists", False, source="filesystem.stat",
            scope=target, raw_ref=action["action_id"],
        )
        return attach_runtime_result(
            "{}", "SUCCEEDED", "FACT_OBSERVED",
            tool="observe_fact", executed=True,
            requested_path=target, resolved_path=target,
            action_id=action["action_id"],
            data={
                "observations": [fact], "observation_status": "CONFIRMED",
                "terminal_eligible": True,
            },
        )
    loop._execute_action = actual_tool

    run_minimal_core(loop, goal)
    assert len(loop.executed) == 1
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.executor_result["root_cause_code"] == "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST"
    assert len(loop.executor_result["evidence_refs"]) == 1
    assert loop.executor_result["evidence_refs"][0].startswith("MIN-EV-")
    assert len(loop.executor_result["admission_rejection_refs"]) == 1
    assert loop.executor_result["admission_rejection_refs"][0].startswith("MIN-ADM-")
    assert (loop.executor_result["last_admission_rejection"]["scope_failure_stage"]
            == "ORIGINAL_REQUEST_BINDING")
    assert (loop.executor_result["last_admission_rejection"]["directory_preconditions"]
            ["parent_directory_verdict"] == "NOT_EVALUATED")
    receipt = read_executor_terminal(
        tmp_path, task_id=loop.task_id, task_epoch=loop.task_epoch,
        request_id=loop.run_id,
    )
    assert receipt["terminal_status"] == "BLOCKED"
    assert receipt["root_cause_code"] == "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST"
    assert receipt["causal_basis"] == "RUNTIME_ADMISSION_DENIAL"
    assert receipt["evidence_refs"] == loop.executor_result["evidence_refs"]
    assert receipt["admission_rejection_refs"] == loop.executor_result["admission_rejection_refs"]
    assert receipt["last_admission_rejection"]["executed"] is False
    assert receipt["reason_code"] != "UNCLASSIFIED_BLOCKER"
    handoff = project_handoff(receipt)
    assert (handoff["executor_handoff"]["executor_root_cause_code"]
            == "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST")
    assert (handoff["executor_handoff"]["executor_admission_rejection_refs"]
            == receipt["admission_rejection_refs"])
    assert handoff["parent_binding_status"] == "ROOT_TASK_PARENT_NOT_BOUND"
    assert handoff["parent_dispatch_state"] == "NOT_DISPATCHED"
    assert handoff["next_parent_owner"] == ""  # No invented parent worker.
    assert handoff["recommended_parent_owner"] == "PREREQUISITE"


def test_composite_scope_reason_not_lost_on_blackboard_egress(tmp_path):
    loop = SimpleNamespace(
        task_id="TASK-GIT30-CAUSAL", run_id="RR-GIT30-CAUSAL",
        task_epoch="EPOCH-GIT30", terminal_acceptance={},
    )
    result = {
        "outcome": "BLOCKED",
        "reason_code": (
            "GOAL_ACTION_REPAIR_EXHAUSTED:"
            "scope_rejected:DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY"
        ),
        "root_cause_code": "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY",
        "goal_verified": False,
        "admission_rejection_refs": ["MIN-ADM-BLOCKER"],
        "evidence_refs": ["MIN-EV-CONFIRMED-ABSENCE"],
        "last_admission_rejection": {
            "tool": "create_directory",
            "root_cause_code": "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY",
            "scope_failure_stage": "PARENT_DIRECTORY",
            "directory_preconditions": {
                "write_authorization_verdict": "AUTHORIZED_BY_EXISTING_POLICY",
                "target_binding_verdict": "EXACT_ORIGINAL_REQUEST_TARGET",
                "parent_directory_verdict": "MISSING",
            },
        },
        "model_round": 2,
    }
    receipt = publish_executor_terminal(
        loop, result, root=tmp_path, goal="Create exact folder",
        evidence_refs=result["evidence_refs"],
    )
    assert receipt["reason_code"].startswith("GOAL_ACTION_REPAIR_EXHAUSTED:")
    assert receipt["root_cause_code"] == "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY"
    assert receipt["reason_sha256"]
    assert receipt["admission_rejection_refs"] == ["MIN-ADM-BLOCKER"]
    assert receipt["last_admission_rejection"]["directory_preconditions"][
        "parent_directory_verdict"] == "MISSING"
    assert (receipt["tri_layer_handoff"]["prerequisite_handoff"]["prerequisite_issue_code"]
            == "OBJECTIVE_PREREQUISITE_MISSING")
    assert receipt["advances_parent_stage"] is False
