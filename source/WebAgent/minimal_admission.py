"""Single-action admission on top of existing canonical SmartAgent executor policy."""
from __future__ import annotations

import re
import uuid

from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_capabilities import get_allowed_tools
from agent_core.action_dependencies import command_is_proven_observation


_READ_ONLY_TOOLS = frozenset({
    "list_directory", "inspect_directory", "find_file", "read_file",
    "inspect_project_scope", "inspect_project_working_set",
    "inspect_semantic_map", "query_project", "query_project_history",
    "inspect_project_ledger", "compare_project_snapshot",
    "observe_fact", "extract_project_dependencies", "web_search",
})


def action_is_mutating(action: dict) -> bool:
    if action.get("tool") == "run_command":
        return not command_is_proven_observation(action)
    return action.get("tool") not in _READ_ONLY_TOOLS


def admit_minimal_action(decision, tool_context) -> tuple[dict | None, str]:
    """Schema, authority and classification are checked before side effects."""
    if decision.kind != "tool_call":
        return None, "not_a_tool_call"
    if decision.tool not in get_allowed_tools(tool_context.interface_name):
        return None, "tool_not_available_for_interface"
    arguments = dict(decision.arguments or {})
    forbidden = {
        "tool", "action_id", "request_id", "task_id", "task_epoch",
        "run_id", "turn_id", "local_nonce", "web_ack_id", "ack_result_id",
    }
    if forbidden.intersection(arguments):
        return None, "runtime_owned_argument_rejected"
    action = {
        "tool": decision.tool,
        **arguments,
        "action_id": "MIN-" + uuid.uuid4().hex[:24].upper(),
    }
    valid, diag = validate_tool_envelope(action)
    if not valid:
        return None, str((diag or {}).get("reason") or "schema_invalid")
    if decision.tool == "run_command":
        operation = str(action.get("operation", "")).upper()
        read_proven = command_is_proven_observation(action)
        if operation in {"INSPECT", "GIT_INSPECT", "VERIFY"} and not read_proven:
            return None, "command_not_proven_read_only"
        if not read_proven:
            if not str(action.get("success_criteria", "")).strip():
                return None, "mutation_success_criteria_required"
            verification = action.get("verify")
            if not isinstance(verification, (list, dict)) or not verification:
                return None, "mutation_verification_required"
    if decision.tool == "delete_path":
        request = str(tool_context.current_request_text or "")
        target = str(action.get("path", "") or "")
        if not re.search(r"刪除|移除|delete|remove", request, re.I):
            return None, "delete_not_requested"
        if not target or target not in request:
            return None, "delete_target_not_explicit"
    # The existing dispatcher also performs security and write-ceiling checks.
    # Preflight can reject, but cannot authorize beyond original user scope.
    try:
        scope = tool_context.preview_tool_scope(action)
    except Exception as exc:
        return None, "scope_preflight_error:" + type(exc).__name__
    if not scope.get("allowed"):
        reason = (scope.get("scope_diagnostic") or {}).get("reason_code")
        return None, "scope_rejected:" + str(reason or scope.get("error") or "unknown")[:128]
    return action, ""
