"""Runtime-owned Action Loop state contract.

The model chooses semantic work.  Runtime owns the objective facts that bound
that choice: whether an action ran, whether it was verified, whether new
evidence was obtained, and which transition is currently legal.  This module
keeps that contract independent from WebAgent transport details so RemoteAgent
and WebCopilot can share it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .command_operation import operation_phase


ACTION_LOOP_STATE_SCHEMA = "SMARTAGENT_ACTION_LOOP_RUNTIME_STATE_V2"

PHASES = {"DISCOVERY", "PLAN", "EXECUTE", "VERIFY", "TERMINAL"}
TRANSITIONS = {
    "CONTINUE_PLAN",
    "PREREQUISITE_REQUIRED",
    "REPLAN_REQUIRED",
    "VERIFY_REQUIRED",
    "TERMINAL",
}

DISCOVERY_TOOLS = {
    "list_directory", "inspect_directory", "find_file", "read_file",
    "inspect_project_scope", "inspect_project_working_set", "query_project", "inspect_project_ledger",
    "query_project_history", "project_sync", "inspect_semantic_map",
    "extract_project_dependencies", "compare_project_snapshot", "web_search",
}
PLAN_TOOLS = {
    "propose_task_plan", "repair_task_plan", "propose_task_plan_file",
    "validate_edit_plan",
}
VERIFY_TOOLS = {"aggregate_verification"}
MUTATION_TOOLS = {
    "apply_edit_plan", "execute_frozen_plan", "write_file", "delete_path",
    "begin_file_write", "write_file_chunk", "commit_file_write",
    "abort_file_write", "run_command", "update_semantic_map",
    "update_semantic_map_file", "web_edit_file",
}
DELIVERY_TOOLS = {
    "upload_file", "upload_files", "download_artifact", "return_artifact",
    "google_drive_upload", "execute_artifact_bundle",
}
EXECUTION_TOOLS = MUTATION_TOOLS | DELIVERY_TOOLS | {
    "build_project_bundle", "build_project_delta", "ask_executor",
    "save_session_summary",
}
KNOWN_ACTION_TOOLS = DISCOVERY_TOOLS | PLAN_TOOLS | VERIFY_TOOLS | EXECUTION_TOOLS | {
    "final_response",
}


def semantic_action_signature(action: Mapping[str, Any]) -> str:
    """Return an action signature that ignores transport correlation fields."""
    payload = {
        str(key): value
        for key, value in dict(action or {}).items()
        if key not in {
            "action_id", "run_id", "turn_id", "request_id", "task_id",
            "task_epoch", "local_nonce", "protocol_version",
        }
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def phase_for_tool(tool: str) -> str:
    name = str(tool or "").strip()
    if name in DISCOVERY_TOOLS:
        return "DISCOVERY"
    if name in PLAN_TOOLS:
        return "PLAN"
    if name in VERIFY_TOOLS:
        return "VERIFY"
    if name == "final_response":
        return "TERMINAL"
    if name in EXECUTION_TOOLS:
        return "EXECUTE"
    raise ValueError(f"action_loop_tool_phase_unmapped:{name or '(empty)'}")


def phase_for_action(action: Mapping[str, Any] | None) -> str:
    """Classify the semantic role of one proposed action.

    Tool identity alone is insufficient for commands: a normal ``run_command``
    performs requested work, while a command explicitly linked with
    ``verifies_action_id`` is a verifier for an already executed action.
    """
    payload = dict(action or {})
    tool = str(payload.get("tool", "") or "").strip()
    if tool == "run_command":
        # Persisted ledgers from before the typed-operation migration may not
        # contain operation.  Keep them readable without admitting new model
        # actions that omit the now-required protocol field.
        legacy_operation = (
            "VERIFY"
            if str(payload.get("verifies_action_id", "") or "").strip()
            else "GENERAL"
        )
        return operation_phase(payload.get("operation") or legacy_operation)
    return phase_for_tool(tool)


def action_lifecycle(
    action: Mapping[str, Any] | None,
    execution_status: str,
    verification_status: str,
    evidence_state: str,
    expectation_status: str = "",
) -> str:
    """Return one Runtime-owned lifecycle state for a concrete action."""
    if not action:
        return "NOT_STARTED"
    execution = str(execution_status or "UNKNOWN").upper()
    verification = str(verification_status or "UNKNOWN").upper()
    evidence = str(evidence_state or "MISSING").upper()
    expectation = str(expectation_status or "").upper()
    if expectation == "PASS":
        return "EXPECTED_FAILURE_PASS"
    if expectation in {"FAIL", "SPEC_INVALID"}:
        return "EXPECTATION_FAILED"
    if execution == "FAILED":
        return "EXECUTION_FAILED"
    if verification in {"FAIL", "SPEC_INVALID"}:
        return "VERIFICATION_FAILED"
    if verification == "PASS":
        return "VERIFIED_PASS"
    if execution == "SUCCEEDED" and evidence == "UNVERIFIED":
        return "EXECUTED_UNVERIFIED"
    if execution == "SUCCEEDED":
        return "EXECUTED"
    return "ADMITTED"


def _query_result_has_evidence(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("status", "") or "").upper() != "READY":
        return False
    for result in list(payload.get("results") or []):
        if not isinstance(result, Mapping) or str(result.get("status", "") or "").upper() != "OK":
            continue
        for key in ("matches", "entries", "files", "symbols", "content", "text"):
            value = result.get(key)
            if isinstance(value, (list, dict)) and value:
                return True
            if isinstance(value, str) and value.strip():
                return True
    return False


def classify_evidence_state(
    action: Mapping[str, Any] | None,
    result: str,
    execution_status: str,
    verification_status: str,
    expectation_status: str = "",
) -> tuple[str, list[str]]:
    """Classify objective evidence without deciding task semantics."""
    tool = str((action or {}).get("tool", "") or "")
    execution = str(execution_status or "UNKNOWN").upper()
    verification = str(verification_status or "UNKNOWN").upper()
    expectation = str(expectation_status or "").upper()
    if expectation == "PASS":
        return "SUFFICIENT", []
    if expectation in {"FAIL", "SPEC_INVALID"}:
        return "CONTRADICTED", ["observed result did not match the declared expected failure"]
    if execution == "FAILED":
        return "CONTRADICTED", ["last action did not execute successfully"]
    if verification in {"FAIL", "SPEC_INVALID"}:
        return "CONTRADICTED", ["postcondition verification did not pass"]
    if verification == "PASS":
        return "SUFFICIENT", []
    if not action:
        return "MISSING", ["no Runtime action evidence exists yet"]
    if tool == "query_project":
        try:
            payload = json.loads(str(result or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if _query_result_has_evidence(payload):
            return "AVAILABLE", []
        return "MISSING", ["query_project returned no usable source evidence"]
    if tool in DISCOVERY_TOOLS:
        text = str(result or "").strip()
        if not text or any(marker in text for marker in (
            "[PROTOCOL_ERROR]", "[TOOL_SCOPE_REJECTED]", "status\":\"REJECTED",
        )):
            return "MISSING", [f"{tool} returned no usable evidence"]
        return "AVAILABLE", []
    if tool in MUTATION_TOOLS and verification in {"UNKNOWN", "UNVERIFIED"}:
        return "UNVERIFIED", ["the executed change still requires postcondition verification"]
    return "AVAILABLE", []


def _compact_recovery_actions(recovery: Mapping[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for action in list(recovery.get("next_actions") or []):
        if not isinstance(action, Mapping):
            continue
        tool = str(action.get("tool", "") or "").strip()
        if not tool:
            continue
        row: dict[str, Any] = {"tool": tool}
        for key in ("project_root", "path", "start_line", "end_line", "verifies_action_id"):
            if key in action:
                row[key] = action[key]
        if isinstance(action.get("queries"), list):
            row["queries"] = list(action["queries"])
        compact.append(row)
    return compact


def build_action_loop_state(
    *,
    transport_state: str = "IDLE",
    progress: Mapping[str, Any] | None = None,
    action: Mapping[str, Any] | None = None,
    result: str = "",
    execution_status: str = "UNKNOWN",
    verification_status: str = "UNKNOWN",
    expectation_status: str = "",
    pending_recovery: Mapping[str, Any] | None = None,
    pending_verification: Mapping[str, Any] | None = None,
    action_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one complete, model-readable, Runtime-owned state snapshot."""
    progress = dict(progress or {})
    recovery = dict(pending_recovery or {})
    verification = dict(pending_verification or {})
    tool = str((action or {}).get("tool", "") or "")
    phase = phase_for_action(action) if action else "DISCOVERY"
    evidence_state, missing = classify_evidence_state(
        action, result, execution_status, verification_status, expectation_status,
    )
    allowed: list[str] = []
    blocked = [str(item) for item in list(recovery.get("forbidden_actions") or []) if str(item)]
    blocked_signatures: list[str] = []

    decision = str(progress.get("decision", "CONTINUE") or "CONTINUE").upper()
    if decision in {"COMPLETE", "INTERRUPT"}:
        phase = "TERMINAL"
        transition = "TERMINAL"
        allowed = ["final_response"]
    elif recovery.get("active") and not recovery.get("prerequisite_satisfied"):
        transition = "PREREQUISITE_REQUIRED"
        allowed = [
            str(item.get("tool", "") or "")
            for item in _compact_recovery_actions(recovery)
        ]
        allowed = [item for item in allowed if item]
        missing = [str(item) for item in list(recovery.get("missing_content_paths") or []) if str(item)] or [
            str(recovery.get("blocking_reason", "") or recovery.get("reason", "") or "required prerequisite evidence is missing")
        ]
        evidence_state = "MISSING"
    elif verification and str(verification_status or "UNKNOWN").upper() not in {"PASS", "FAIL"}:
        phase = "VERIFY"
        transition = "VERIFY_REQUIRED"
        requested_verify_tools = [
            str(item) for item in list(verification.get("supported_verify_actions") or [])
            if str(item)
        ] or [str(verification.get("required_action", "run_command") or "run_command")]
        # file_exists/file_contains/expect_regex are verify[] check kinds, not
        # top-level smartagent tools.  Never advertise them as executable actions.
        allowed = [item for item in requested_verify_tools if item in KNOWN_ACTION_TOOLS]
        if not allowed:
            allowed = ["run_command"]
        missing = [str(verification.get("missing_condition", "verification evidence is missing"))]
        evidence_state = "UNVERIFIED"
    elif (
        str(execution_status or "UNKNOWN").upper() == "FAILED"
        and str(expectation_status or "").upper() != "PASS"
    ) or evidence_state == "CONTRADICTED":
        transition = "REPLAN_REQUIRED"
        allowed = ["propose_task_plan", "repair_task_plan", "query_project", "read_file", "run_command"]
    elif action and evidence_state == "MISSING":
        transition = "REPLAN_REQUIRED"
        allowed = ["propose_task_plan", "repair_task_plan", "query_project", "read_file", "list_directory"]
        blocked_signatures = [semantic_action_signature(action)]
    elif action and tool in MUTATION_TOOLS and evidence_state == "UNVERIFIED":
        phase = "VERIFY"
        transition = "VERIFY_REQUIRED"
        allowed = ["run_command", "aggregate_verification", "query_project", "read_file"]
    else:
        transition = "CONTINUE_PLAN"
        allowed = sorted(KNOWN_ACTION_TOOLS)

    states: list[dict[str, Any]] = []
    for record in list(action_records or []):
        record_action = dict(record.get("action") or {})
        record_execution = str(record.get("execution_status", "UNKNOWN") or "UNKNOWN").upper()
        record_verification = str(record.get("verification_status", "UNKNOWN") or "UNKNOWN").upper()
        record_expectation = str(record.get("expectation_status", "") or "").upper()
        record_result = str(record.get("result", "") or "")
        record_evidence, record_missing = classify_evidence_state(
            record_action, record_result, record_execution, record_verification,
            record_expectation,
        )
        states.append({
            "action_id": str(record_action.get("action_id", "") or ""),
            "tool": str(record_action.get("tool", "") or ""),
            "operation": str(record_action.get("operation", "") or ""),
            "role": phase_for_action(record_action),
            "lifecycle": action_lifecycle(
                record_action, record_execution, record_verification, record_evidence,
                record_expectation,
            ),
            "execution_status": record_execution,
            "verification_status": record_verification,
            "expectation_status": record_expectation,
            "evidence_state": record_evidence,
            "missing_evidence": record_missing,
            "verifies_action_id": str(record_action.get("verifies_action_id", "") or ""),
            "semantic_signature": semantic_action_signature(record_action),
        })

    terminal = transition == "TERMINAL"
    response_contract = {
        "required_blocks": ["report_progress", "final_response" if terminal else "canonical_action"],
        "allowed_action_tools": list(dict.fromkeys(allowed)),
        "runtime_owned_progress_fields": [
            "runtime_state_ref", "next_phase", "selected_action",
        ],
        "model_owned_progress_fields": [
            "decision", "outcome", "matched_condition", "evidence_refs",
            "decision_reason", "current_step", "total_steps", "current_focus",
            "next_action", "steps", "completion_contract",
        ],
    }

    payload = {
        "schema": ACTION_LOOP_STATE_SCHEMA,
        "phase": phase if phase in PHASES else "DISCOVERY",
        "execution_status": str(execution_status or "UNKNOWN").upper(),
        "verification_status": str(verification_status or "UNKNOWN").upper(),
        "expectation_status": str(expectation_status or "").upper(),
        "evidence_state": evidence_state,
        "required_transition": transition if transition in TRANSITIONS else "REPLAN_REQUIRED",
        "allowed_next_actions": list(dict.fromkeys(allowed)),
        "blocked_actions": list(dict.fromkeys(blocked)),
        "blocked_action_signatures": blocked_signatures,
        "missing_evidence": [item for item in missing if item],
        "last_action": {
            "tool": tool,
            "semantic_signature": semantic_action_signature(action) if action else "",
        },
        "action_states": states,
        "prerequisite_actions": _compact_recovery_actions(recovery),
        "response_contract": response_contract,
    }
    task_state = (
        "COMPLETED" if decision == "COMPLETE"
        else "INTERRUPTED" if decision == "INTERRUPT"
        else "VERIFYING" if phase == "VERIFY"
        else "RUNNING" if action else "PLANNING"
    )
    payload["state_layers"] = {
        "transport": {
            "state": str(transport_state or "IDLE").upper(),
            "owner": "runtime",
        },
        "action": {
            "state": states[-1]["lifecycle"] if states else "NOT_STARTED",
            "phase": payload["phase"],
            "owner": "runtime",
        },
        "task": {
            "state": task_state,
            "model_decision": decision,
            "owner": "runtime",
        },
    }
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["state_id"] = "ALS-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20].upper()
    return payload


def render_action_loop_state(state: Mapping[str, Any]) -> str:
    return (
        "[ACTION_LOOP_RUNTIME_STATE]\n"
        + json.dumps(dict(state), ensure_ascii=False, separators=(",", ":"))
        + "\n[/ACTION_LOOP_RUNTIME_STATE]\n"
        + "此狀態由 Runtime 持有。完整使用其 evidence、限制與 prerequisite。"
        "runtime_state_ref、next_phase、selected_action、execution_status、verification_status "
        "皆為 Runtime-owned；模型不得複製或推測。模型只輸出語意 decision 欄位與本輪 canonical action。"
    )


__all__ = [
    "ACTION_LOOP_STATE_SCHEMA", "PHASES", "TRANSITIONS", "build_action_loop_state",
    "action_lifecycle", "classify_evidence_state", "phase_for_action", "phase_for_tool", "render_action_loop_state",
    "semantic_action_signature",
]
