#!/usr/bin/env python3
"""Focused regression checks for evidence-backed tolerant completion."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.protocol_v9 import parse_v9_tool_transport
from agent_core.task_progress import initialize_progress
from agent_core.task_progress import record_model_progress
from agent_core.task_state import RemoteTaskQueue, TaskStateStore, TASK_INTERRUPTED
from agent_core.tools import _evaluate_verification_step


def fence_text(text: str) -> str:
    return "```smartagent_tool\n" + text + "\n```"


def progress(action_id: str, *, decision: str = "CONTINUE", outcome: str = "PENDING") -> dict:
    terminal = decision == "COMPLETE"
    return {
        "tool": "report_progress",
        "action_id": action_id,
        "base_evaluation": "Runtime evidence decides completion independently from transport syntax.",
        "total_steps": 1,
        "current_step": 1 if terminal else 0.5,
        "steps": [{"step": 1, "desc": "execute and verify", "status": "COMPLETED" if terminal else "IN_PROGRESS"}],
        "current_focus": "verify result",
        "next_action": "report verified result" if terminal else "execute verified action",
        "completion_contract": {
            "success": ["requested checkpoint exists and verification passes"],
            "failure": ["checkpoint verification fails"],
            "in_progress": ["verification evidence is not available yet"],
            "interrupted": ["Runtime cannot obtain verification evidence"],
        },
        "decision": decision,
        "outcome": outcome,
        "matched_condition": (
            "requested checkpoint exists and verification passes"
            if terminal else "verification evidence is not available yet"
        ),
        "evidence_refs": ["A-COMMIT"] if terminal else ["REQUEST_ACCEPTED"],
        "decision_reason": "Classified from current Runtime evidence.",
    }


def validate_multiple_json_objects_in_one_fence() -> None:
    first = progress("P-DONE", decision="COMPLETE", outcome="SUCCESS")
    final = {"tool": "final_response", "action_id": "A-FINAL", "content": "done"}
    commit = {"tool": "turn_commit", "action_count": 2}
    response = "\n".join((
        fence_text(json.dumps(first)),
        fence_text(json.dumps(final) + "\n" + json.dumps(commit)),
    ))
    calls, diagnostics = parse_v9_tool_transport(response)
    assert diagnostics == []
    assert [item["tool"] for item in calls] == ["report_progress", "final_response", "turn_commit"]

    malformed = "\n".join((
        fence_text(json.dumps(first)),
        fence_text(json.dumps(final) + "\nnot-json\n" + json.dumps(commit)),
    ))
    calls, diagnostics = parse_v9_tool_transport(malformed)
    assert calls == []
    assert diagnostics[0]["reason"] == "JSON_DECODE_ERROR"


def configured_loop(root: Path) -> WebAgentProtocolLoop:
    loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
    loop.run_id = "RR-EVIDENCE"
    loop.task_id = "TASK-EVIDENCE"
    loop.task_epoch = "EPOCH-EVIDENCE"
    loop.intent_digest = "a" * 64
    loop.turn_id = 1
    loop.progress_ledger = initialize_progress(
        loop.task_id, request_id=loop.run_id, goal="create checkpoint", root=root,
    )
    return loop


def validate_run_command_contract_is_required_before_execution() -> None:
    with tempfile.TemporaryDirectory(prefix="evidence-preflight-") as temp:
        loop = configured_loop(Path(temp))
        calls = [
            progress("P-RUN"),
            {"tool": "run_command", "action_id": "A-COMMIT", "command": "git status --short"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert accepted == []
        assert diagnostics[0]["reason"] == "run_command_verification_contract_required"
        assert "success_criteria" in diagnostics[0]["detail"]
        assert "verify" in diagnostics[0]["detail"]
        assert loop.action_ledger == {}


def validate_execution_and_verification_are_separate() -> None:
    action = {"tool": "run_command", "action_id": "A"}
    observed = WebAgentProtocolLoop._classify_action_evidence(
        action, "[COMMAND_RESULT]\nexit_code: 0\nVERIFICATION_STATUS: UNVERIFIED",
    )
    assert observed == {
        "tool": "run_command",
        "execution_status": "SUCCEEDED",
        "verification_status": "UNVERIFIED",
    }
    contradicted = WebAgentProtocolLoop._classify_action_evidence(
        action, "[COMMAND_RESULT]\nexit_code: 0\nVERIFICATION_STATUS: FAIL",
    )
    assert contradicted["execution_status"] == "SUCCEEDED"
    assert contradicted["verification_status"] == "FAIL"


def validate_regex_verification_and_invalid_spec_are_distinct() -> None:
    matched = _evaluate_verification_step({
        "action": "run_command",
        "command": "Write-Output 0123456789abcdef0123456789abcdef01234567",
        "expect_exit_code": 0,
        "expect_regex": r"^[0-9a-f]{40}\r?$",
    })
    assert matched["spec_valid"] is True
    assert matched["passed"] is True
    invalid = _evaluate_verification_step({
        "action": "run_command",
        "command": "Write-Output ok",
        "expect_regex": "[",
    })
    assert invalid["spec_valid"] is False
    assert invalid["passed"] is False
    assert "invalid expect_regex" in invalid["error"]


def validate_scoped_verification_supersedes_prior_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="verification-supersession-") as temp:
        loop = configured_loop(Path(temp))
        original = {
            "tool": "run_command",
            "action_id": "A-COMMIT",
            "condition_id": "checkpoint exists",
        }
        loop._record_action_result_evidence(original, {
            "tool": "run_command",
            "execution_status": "SUCCEEDED",
            "verification_status": "FAIL",
        })
        first_id = loop.action_result_ledger["A-COMMIT"]["effective_verification_id"]
        assert loop._effective_verification_status(["A-COMMIT"], matched_condition="checkpoint exists") == "FAIL"

        repair = {
            "tool": "run_command",
            "action_id": "A-VERIFY-HEAD",
            "verifies_action_id": "A-COMMIT",
            "condition_id": "checkpoint exists",
        }
        loop.turn_id = 2
        loop._record_action_result_evidence(repair, {
            "tool": "run_command",
            "execution_status": "SUCCEEDED",
            "verification_status": "PASS",
        })
        target = loop.action_result_ledger["A-COMMIT"]
        assert target["effective_verification_status"] == "PASS"
        assert target["effective_verification_id"] != first_id
        assert target["verification_history"][-1]["supersedes_verification_id"] == first_id
        assert loop._effective_verification_status(["A-COMMIT"], matched_condition="checkpoint exists") == "PASS"


def validate_unrelated_pass_does_not_override_referenced_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="verification-isolation-") as temp:
        loop = configured_loop(Path(temp))
        loop._record_action_result_evidence({
            "tool": "run_command", "action_id": "A-FAILED", "condition_id": "checkpoint exists",
        }, {
            "tool": "run_command", "execution_status": "SUCCEEDED", "verification_status": "FAIL",
        })
        loop._record_action_result_evidence({
            "tool": "run_command", "action_id": "A-UNRELATED", "condition_id": "other condition",
        }, {
            "tool": "run_command", "execution_status": "SUCCEEDED", "verification_status": "PASS",
        })
        assert loop._effective_verification_status(
            ["A-FAILED", "A-UNRELATED"], matched_condition="checkpoint exists",
        ) == "FAIL"


def validate_action_scoped_pass_overrides_legacy_global_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="verification-terminal-scope-") as temp:
        loop = configured_loop(Path(temp))
        loop.tools.last_verification_status = "FAIL"
        loop._record_action_result_evidence({
            "tool": "run_command",
            "action_id": "A-COMMIT",
            "condition_id": "requested checkpoint exists and verification passes",
        }, {
            "tool": "run_command",
            "execution_status": "SUCCEEDED",
            "verification_status": "PASS",
        })
        calls = [
            progress("P-DONE", decision="COMPLETE", outcome="SUCCESS"),
            {"tool": "final_response", "action_id": "A-FINAL", "content": "done"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert diagnostics == []
        assert accepted[0]["decision"] == "COMPLETE"


def validate_final_step_may_continue_only_with_action() -> None:
    with tempfile.TemporaryDirectory(prefix="verification-final-step-") as temp:
        loop = configured_loop(Path(temp))
        payload = progress("P-VERIFY")
        payload.update({
            "current_step": 1,
            "steps": [{"step": 1, "desc": "repair verification", "status": "IN_PROGRESS"}],
            "next_action": "run corrective verification",
        })
        calls = [
            payload,
            {
                "tool": "run_command",
                "action_id": "A-VERIFY",
                "command": "Write-Output ok",
                "success_criteria": "command is observable",
                "verify": [{"action": "run_command", "command": "Write-Output ok", "expect_regex": "^ok\\r?$"}],
            },
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert diagnostics == []
        assert accepted[1]["action_id"] == "A-VERIFY"

        progress_only = [payload, {"tool": "turn_commit", "action_count": 1}]
        accepted, diagnostics = loop._accept_ack(progress_only, {"ack_web_ack_id": ""})
        assert accepted == []
        assert diagnostics[0]["reason"] == "final_step_continue_requires_action"


def validate_terminal_candidate_survives_verification_gap() -> None:
    with tempfile.TemporaryDirectory(prefix="evidence-candidate-") as temp:
        loop = configured_loop(Path(temp))
        loop.tools.last_verification_status = "UNVERIFIED"
        loop.action_result_ledger["A-COMMIT"] = {
            "status": "COMMITTED",
            "tool": "run_command",
            "execution_status": "SUCCEEDED",
            "verification_status": "UNVERIFIED",
        }
        calls = [
            progress("P-DONE", decision="COMPLETE", outcome="SUCCESS"),
            {"tool": "final_response", "action_id": "A-FINAL", "content": "checkpoint created"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert accepted == []
        assert diagnostics[0]["reason"] == "terminal_success_verification_missing"
        assert loop.terminal_candidate["content"] == "checkpoint created"
        assert loop.terminal_candidate["verification_status"] == "UNVERIFIED"
        context = loop._runtime_evidence_context()
        assert '"execution_status":"SUCCEEDED"' in context
        assert '"verification_status":"UNVERIFIED"' in context


def validate_protocol_only_interruption_preserves_evidence() -> None:
    with tempfile.TemporaryDirectory(prefix="evidence-interruption-") as temp:
        root = Path(temp)
        queue = RemoteTaskQueue(TaskStateStore(root / "tasks.json"))
        task, created = queue.enqueue_remote_request({
            "request_id": "RR-INTERRUPT",
            "request": "create checkpoint",
            "workspace": str(root),
            "conversation_url": "https://chatgpt.com/c/test",
        })
        assert created
        reservation = queue.dispatch_next(max_active=1)
        assert reservation is not None and reservation[0].task_id == task.task_id
        evidence = {
            "execution_state": "SUCCEEDED",
            "protocol_state": "INTERRUPTED",
            "action_result_ledger": {"A-COMMIT": {"execution_status": "SUCCEEDED"}},
        }
        interrupted = queue.interrupt_running(
            task.task_id, "protocol closure malformed", result=evidence,
        )
        assert interrupted is not None
        assert interrupted.state == TASK_INTERRUPTED
        assert interrupted.result_ledger["interrupted"] == evidence


def validate_unambiguous_terminal_continue_is_normalized() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-normalize-") as temp:
        root = Path(temp)
        loop = configured_loop(root)
        initial = progress("P-INITIAL")
        initial["current_step"] = 0
        initial["completion_contract"] = {
            "success": ["directory listing was returned"],
            "failure": ["directory listing failed"],
            "in_progress": ["directory listing is not available yet"],
            "interrupted": ["Runtime cannot access the directory"],
        }
        initial["matched_condition"] = "directory listing is not available yet"
        loop.progress_ledger = record_model_progress(
            loop.task_id, initial, request_id=loop.run_id,
            goal="create checkpoint", round_id=1, root=root,
        )
        loop.action_result_ledger["A-LIST"] = {
            "status": "COMMITTED",
            "tool": "list_directory",
            "execution_status": "SUCCEEDED",
            "verification_status": "UNKNOWN",
        }

        terminal = progress("P-DONE", decision="CONTINUE", outcome="SUCCESS")
        terminal.update({
            "current_step": 1,
            "steps": [{"step": 1, "desc": "execute and verify", "status": "COMPLETED"}],
            "completion_contract": {
                "success": ["directory listing was returned"],
                "failure": ["directory listing failed"],
                "in_progress": [],
                "interrupted": ["Runtime cannot access the directory"],
            },
            "matched_condition": "directory listing was returned",
            "evidence_refs": ["A-LIST"],
            "next_action": "",
        })
        calls = [
            terminal,
            {"tool": "final_response", "action_id": "A-FINAL", "content": "1 file"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert diagnostics == []
        assert accepted[0]["decision"] == "COMPLETE"


def validate_ambiguous_terminal_continue_stays_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-ambiguous-") as temp:
        loop = configured_loop(Path(temp))
        terminal = progress("P-DONE", decision="CONTINUE", outcome="SUCCESS")
        terminal.update({
            "current_step": 1,
            "steps": [{"step": 1, "desc": "execute and verify", "status": "COMPLETED"}],
            "evidence_refs": ["REQUEST_ACCEPTED"],
        })
        calls = [
            terminal,
            {"tool": "final_response", "action_id": "A-FINAL", "content": "done"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert accepted == []
        assert diagnostics[0]["reason"] == "invalid_progress_payload"
        assert terminal["decision"] == "CONTINUE"


if __name__ == "__main__":
    validate_multiple_json_objects_in_one_fence()
    validate_run_command_contract_is_required_before_execution()
    validate_execution_and_verification_are_separate()
    validate_regex_verification_and_invalid_spec_are_distinct()
    validate_scoped_verification_supersedes_prior_failure()
    validate_unrelated_pass_does_not_override_referenced_failure()
    validate_action_scoped_pass_overrides_legacy_global_failure()
    validate_final_step_may_continue_only_with_action()
    validate_terminal_candidate_survives_verification_gap()
    validate_protocol_only_interruption_preserves_evidence()
    validate_unambiguous_terminal_continue_is_normalized()
    validate_ambiguous_terminal_continue_stays_rejected()
    print("WEBAGENT_EVIDENCE_BACKED_COMPLETION_OK")
