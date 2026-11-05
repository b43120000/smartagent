"""Command Repair Readiness V1: deterministic, side-effect-free diagnostics.

Only the Planner chooses command/operation/verification semantics. This module
does not repair commands, execute scripts, or override Tool Envelope Admission.
It classifies independent *known* defects before prompting another Planner
turn, while preserving the original request/epoch/action identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCHEMA = "SMARTAGENT_COMMAND_REPAIR_READINESS_V1"
FIELD_REPAIR = "FIELD_REPAIR"
ACTION_RECONSTRUCTION = "ACTION_RECONSTRUCTION"
READY_FOR_ADMISSION = "READY_FOR_ADMISSION"
ALLOWLISTED_FIELDS = frozenset({
    "command", "operation", "verify", "success_criteria",
    "working_directory", "timeout", "expected_failure",
    "expected_verification", "result_transport", "result_purpose",
    "full_result_required", "condition_id", "verifies_action_id",
    "depends_on", "replaces_action_id", "action_id", "tool",
})
RECONSTRUCT_FIELDS = frozenset({"command", "tool", "action_id"})


def analyze_command_repair(action: Mapping[str, Any]) -> dict:
    """Batch *independent* structural and semantic contradictions.

    An absent/invalid script command, unsupported extra key, or command
    operation mismatch cannot be repaired by merely supplying a verifier.
    A replacement with a NEW action_id still faces ordinary full admission.
    """
    from .smartagent_protocol import (
        TOOL_ENVELOPE_SCHEMAS, _validate_run_command_complexity,
    )
    from .command_operation import (
        normalize_command_operation, operation_mismatch_detail,
    )
    from .command_security import inspect_command

    issues = []
    seen = set()
    reconstruct = False

    def add(field: str, code: str, *, needs_reconstruction: bool = False) -> None:
        nonlocal reconstruct
        if (field, code) not in seen:
            issues.append({"field": field, "code": code})
            seen.add((field, code))
        reconstruct = reconstruct or needs_reconstruction

    if not isinstance(action, Mapping) or action.get("tool") != "run_command":
        return {
            "schema": SCHEMA, "mode": ACTION_RECONSTRUCTION,
            "issue_count": 1,
            "field_issues": [{"field": "tool", "code": "run_command_action_required"}],
            "repairable_fields": [], "authorization_granted": False,
            "full_admission_required": True,
        }

    schema = TOOL_ENVELOPE_SCHEMAS["run_command"]
    extras = set(action) - {
        "tool", "action_id", "depends_on", "replaces_action_id",
        *schema["required"], *schema["optional"],
    }
    if extras:
        add("tool", "unexpected_action_fields", needs_reconstruction=True)
    for field, spec in {**schema["required"], **schema["optional"]}.items():
        if field not in action:
            if field in schema["required"]:
                add(field, "required_field_missing",
                    needs_reconstruction=field in RECONSTRUCT_FIELDS)
            continue
        value = action[field]
        allowed = spec if isinstance(spec, tuple) else (spec,)
        # bool is a subclass of int but does not satisfy the JSON integer type.
        if type(value) not in allowed:
            add(field, "field_type_invalid",
                needs_reconstruction=field in RECONSTRUCT_FIELDS)

    action_id = action.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        add("action_id", "action_id_missing_or_invalid",
            needs_reconstruction=True)
    if "depends_on" in action:
        depends = action["depends_on"]
        if (not isinstance(depends, list) or len(depends) > 32
                or any(not isinstance(x, str) or not x.strip() for x in depends)
                or len(set(depends)) != len(depends)):
            add("depends_on", "dependencies_invalid",
                needs_reconstruction=True)

    command = action.get("command")
    if type(command) is str:
        if not command.strip():
            add("command", "command_empty", needs_reconstruction=True)
        else:
            command_ok, command_error = _validate_run_command_complexity(
                command, field_path="command",
            )
            if not command_ok:
                add("command", str(command_error.get("reason", "command_invalid")),
                    needs_reconstruction=True)
            else:
                admission = inspect_command(command)
                if not admission.allowed:
                    add("command", str(admission.code or "command_security_denied"),
                        needs_reconstruction=True)

    operation = action.get("operation")
    normalized_op = None
    if type(operation) is str:
        try:
            normalized_op = normalize_command_operation(operation)
        except (TypeError, ValueError):
            add("operation", "operation_not_in_registry")
    if normalized_op is not None and type(command) is str and command.strip():
        try:
            if operation_mismatch_detail(action):
                add("operation", "operation_command_mismatch",
                    needs_reconstruction=True)
        except (TypeError, ValueError):
            add("operation", "operation_semantic_invalid",
                needs_reconstruction=True)

    verify = action.get("verify")
    if "verify" in action and verify is not None and type(verify) not in {dict, list}:
        add("verify", "verify_object_or_object_list_required")
    if type(verify) in {list, dict}:
        steps = verify if isinstance(verify, list) else [verify]
        if not steps:
            add("verify", "verify_nonempty_required")
        if len(steps) > 32:
            add("verify", "verify_steps_exceed_diagnostic_bound")
        for step in steps[:32]:
            if not isinstance(step, dict) or not step:
                add("verify", "verify_nonempty_object_required")
                continue
            primary, alias = step.get("action"), step.get("type")
            kind = primary if primary is not None else alias if alias is not None else "run_command"
            if (primary is not None and alias is not None and primary != alias
                    or not isinstance(kind, str)
                    or kind not in {"run_command", "file_exists", "file_contains"}):
                add("verify", "verification_action_invalid")
                continue
            if kind != "run_command":
                continue
            check = step.get("command", "")
            if not isinstance(check, str) or not check.strip():
                add("verify", "verification_command_missing_or_invalid")
                continue
            valid, diagnostic = _validate_run_command_complexity(
                check, field_path="verify.command",
            )
            if not valid:
                add("verify", str(diagnostic.get("reason", "verify_command_invalid")))
                continue
            if not inspect_command(check).allowed:
                add("verify", "verify_command_security_denied")

    # The pre-execution Verification Contract is separate from the Tool
    # Envelope, but it must be included in the same diagnostic batch.
    observation = normalized_op in {"INSPECT", "GIT_INSPECT"}
    negative_assertion = (
        type(action.get("expected_failure")) is dict
        and bool(action["expected_failure"])
        and normalized_op in {"INSPECT", "GIT_INSPECT", "BUILD", "TEST", "VERIFY"}
    )
    if normalized_op is not None and not observation:
        criteria = action.get("success_criteria")
        if type(criteria) is not str or not criteria.strip():
            add("success_criteria", "success_criteria_required")
        if not negative_assertion and (verify is None or verify == []):
            add("verify", "verify_required")
    elif normalized_op is None:
        # Warn about conditional prerequisites in the SAME round as an
        # unknown operation; never assume which legal operation Planner chose.
        if not str(action.get("success_criteria", "") or "").strip():
            add("success_criteria", "required_if_non_observation")
        if verify is None or verify == []:
            add("verify", "required_if_non_observation")

    fields = sorted({issue["field"] for issue in issues})
    if reconstruct:
        mode = ACTION_RECONSTRUCTION
    elif issues:
        mode = FIELD_REPAIR
    else:
        mode = READY_FOR_ADMISSION
    repairable = [field for field in fields if field not in RECONSTRUCT_FIELDS]
    return {
        "schema": SCHEMA,
        "mode": mode, "issue_count": len(issues),
        "field_issues": issues[:16], "repairable_fields": repairable,
        "authorization_granted": False,
        "full_admission_required": True,
        "execution_inferred": False,
        "command_contents_exported": False,
    }


def repair_progress(before: Mapping | None, after: Mapping) -> dict:
    """Convergence metadata, not a permission or a basis for resetting budget."""
    baseline = set(
        (issue.get("field"), issue.get("code"))
        for issue in list((before or {}).get("field_issues") or [])
        if isinstance(issue, Mapping)
    )
    current = set(
        (issue.get("field"), issue.get("code"))
        for issue in list(after.get("field_issues") or [])
        if isinstance(issue, Mapping)
    )
    resolved = baseline - current
    introduced = current - baseline
    return {
        "schema": "SMARTAGENT_COMMAND_REPAIR_CONVERGENCE_V1",
        "previous_issue_count": len(baseline) if before else None,
        "current_issue_count": len(current),
        "resolved_issue_count": len(resolved) if before else 0,
        "introduced_issue_count": len(introduced) if before else 0,
        "converging": bool(before and resolved and len(current) < len(baseline)),
        "fully_ready": after.get("mode") == READY_FOR_ADMISSION,
        "authorization_granted": False,
        "budget_reset": False,
    }
