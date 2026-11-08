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


def canonicalize_safe_observation_alias(decision):
    """Narrow semantic translator for noncanonical READ aliases.

    It only changes an alias if the unchanged command passes the positive
    read-only grammar under the exact canonical operation. Full envelope,
    network credentials, Tool Scope and Write Ceiling admission still apply.
    Never silently classify a mutation or verification as an INSPECT.
    """
    if decision.kind != "tool_call" or decision.tool != "run_command":
        return decision, {}
    args = dict(decision.arguments or {})
    old = str(args.get("operation") or "").strip().upper()
    if old not in {"READ", "READ_ONLY", "LIST", "GIT_READ"}:
        return decision, {}
    command = str(args.get("command") or "").strip()
    if not command:
        return decision, {}
    import re
    candidates = (("GIT_INSPECT",) if re.match(r"^git(?:\.exe)?\s", command, re.I)
                  else ("INSPECT",))
    for operation in candidates:
        trial = {**args, "operation": operation}
        if command_is_proven_observation(trial):
            from .minimal_decision import MinimalDecision
            return MinimalDecision(decision.kind, decision.tool, trial, decision.message), {
                "rewrite_reason_code": "PROVEN_READ_ALIAS_TO_CANONICAL_OPERATION",
                "rewrite_changed_fields": ["operation"],
                "original_operation": old,
                "canonical_operation": operation,
                "authorizes_action": False,
            }
    return decision, {}



def canonicalize_exact_target_inspection(decision, tool_context):
    """Narrow one inspect_directory proposal to ONE trusted request target.

    This is a deterministic scope *reduction*, not new authorization.
    The original proposal remains available for causal trace. Only an exact
    target or its ancestor may be replaced; sibling, traversal, UNC, multi-
    path and unbound scopes are never rewritten. Admission still runs after.
    """
    if decision.kind != "tool_call" or decision.tool != "inspect_directory":
        return decision, {}
    from pathlib import Path
    import ntpath
    import hashlib

    roots = list(getattr(tool_context, "_authorized_local_paths", ()) or ())
    if len(roots) != 1:
        return decision, {}
    original_request = str(getattr(tool_context, "current_request_text", "") or "")
    target = str(roots[0]).strip()
    if (not re.fullmatch(r'^[A-Za-z]:[\\/][^:*?"<>|]* target)
            or target not in original_request):
        return decision, {}
    args = dict(decision.arguments or {})
    values = args.get("paths")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return decision, {}
    proposed = values[0].strip()
    if not re.fullmatch(r"[A-Za-z]:[\\/][^:*?"<>|]*", proposed):
        return decision, {}
    t = ntpath.normcase(ntpath.normpath(target))
    p = ntpath.normcase(ntpath.normpath(proposed))
    # Explicitly reduce an ancestor stat to the exact path; no invented path.
    if p != t:
        try:
            if ntpath.commonpath([t, p]) != p:
                return decision, {}
        except ValueError:
            return decision, {}
    if p == t and args.get("recursive") is False:
        return decision, {}
    replacement = {**args, "paths": [target], "recursive": False}
    from .minimal_decision import MinimalDecision
    return MinimalDecision(decision.kind, decision.tool, replacement, decision.message), {
        "schema": "SMARTAGENT_SCOPE_NARROWING_V1",
        "rewrite_reason_code": "EXACT_REQUEST_TARGET_READ_SCOPE_NARROWED",
        "changed_fields": [
            key for key in ("paths", "recursive") if args.get(key) != replacement[key]
        ],
        "proposal_sha256": hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "authorizes_action": False,
        "scope_expanded": False,
        "original_request_bound": True,
    }


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
, target)
            or target not in original_request):
        return decision, {}
    args = dict(decision.arguments or {})
    values = args.get("paths")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return decision, {}
    proposed = values[0].strip()
    if not re.fullmatch(r'^[A-Za-z]:[\\/][^:*?"<>|]* proposed):
        return decision, {}
    t = ntpath.normcase(ntpath.normpath(target))
    p = ntpath.normcase(ntpath.normpath(proposed))
    # Explicitly reduce an ancestor stat to the exact path; no invented path.
    if p != t:
        try:
            if ntpath.commonpath([t, p]) != p:
                return decision, {}
        except ValueError:
            return decision, {}
    if p == t and args.get("recursive") is False:
        return decision, {}
    replacement = {**args, "paths": [target], "recursive": False}
    from .minimal_decision import MinimalDecision
    return MinimalDecision(decision.kind, decision.tool, replacement, decision.message), {
        "schema": "SMARTAGENT_SCOPE_NARROWING_V1",
        "rewrite_reason_code": "EXACT_REQUEST_TARGET_READ_SCOPE_NARROWED",
        "changed_fields": [
            key for key in ("paths", "recursive") if args.get(key) != replacement[key]
        ],
        "proposal_sha256": hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "authorizes_action": False,
        "scope_expanded": False,
        "original_request_bound": True,
    }


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
, proposed):
        return decision, {}
    t = ntpath.normcase(ntpath.normpath(target))
    p = ntpath.normcase(ntpath.normpath(proposed))
    # Explicitly reduce an ancestor stat to the exact path; no invented path.
    if p != t:
        try:
            if ntpath.commonpath([t, p]) != p:
                return decision, {}
        except ValueError:
            return decision, {}
    if p == t and args.get("recursive") is False:
        return decision, {}
    replacement = {**args, "paths": [target], "recursive": False}
    from .minimal_decision import MinimalDecision
    return MinimalDecision(decision.kind, decision.tool, replacement, decision.message), {
        "schema": "SMARTAGENT_SCOPE_NARROWING_V1",
        "rewrite_reason_code": "EXACT_REQUEST_TARGET_READ_SCOPE_NARROWED",
        "changed_fields": [
            key for key in ("paths", "recursive") if args.get(key) != replacement[key]
        ],
        "proposal_sha256": hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "authorizes_action": False,
        "scope_expanded": False,
        "original_request_bound": True,
    }


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
