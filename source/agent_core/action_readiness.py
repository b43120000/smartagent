"""Deterministic, non-authorizing Scope / Action Readiness diagnostics.

Normalize only Runtime-produced preflight codes. Never treat failed scope
admission as an actual filesystem observation or grant to use a parent path.
"""
from __future__ import annotations

from collections.abc import Mapping

SCHEMA = "SMARTAGENT_ACTION_READINESS_V1"
ACTION_FIELDS = {
    "inspect_project_scope": ("workspace",),
    "inspect_project_working_set": ("workspace", "paths"),
    "list_directory": ("path",),
    "find_file": ("root",),
    "inspect_directory": ("paths",),
    "observe_fact": ("path",),
    "query_project": ("project_root", "queries"),
    "run_command": ("command", "operation", "working_directory"),
    "create_directory": ("path",),
}
PROJECT_WORKSPACE_TOOLS = frozenset({
    "query_project", "inspect_project_scope", "inspect_project_working_set",
})

# Declarative, read-only tool preconditions; an existing directory is a
# necessary prerequisite for these operations, never a permission grant.
DIRECTORY_PRECONDITION_FIELDS = {
    "list_directory": "path",
    "find_file": "root",
    "query_project": "project_root",
    "inspect_project_scope": "workspace",
    "inspect_project_working_set": "workspace",
    "run_command": "working_directory",
}


def tool_readiness_contract(tool: object) -> dict:
    """Field and resource preconditions, NOT an admission decision."""
    name = str(tool or "")
    return {
        "tool": name,
        "required_action_fields_when_explicit_scope": list(ACTION_FIELDS.get(name, ())),
        "existing_project_workspace_required": name in PROJECT_WORKSPACE_TOOLS,
        "directory_precondition": (
            {"field": DIRECTORY_PRECONDITION_FIELDS[name],
             "predicate": "directory.exists", "expected": True,
             "not_an_authorization_grant": True}
            if name in DIRECTORY_PRECONDITION_FIELDS else None
        ),
        "tool_admission_still_required": True,
        "can_expand_authorized_scope": False,
        "missing_path_may_be_a_prerequisite_not_a_project": name in PROJECT_WORKSPACE_TOOLS,
    }


# Stable, non-authorizing causal codes. Never expose the original path or
# exception message in a Planner / Blackboard reason.
_SCOPE_ERROR_CODES = (
    "directory_target_not_exact_original_request",
    "directory_parent_not_existing_directory",
    "directory_parent_not_a_directory",
    "directory_parent_access_denied",
    "directory_parent_stat_failed",
    "workspace_not_found_or_not_directory",
    "explicit_path_scope_requires_workspace",
    "explicit_path_scope_requires_paths",
    "explicit_path_scope_requires_path",
    "explicit_path_scope_requires_root",
    "inspect_project_scope_unsupported_fields",
    "project_working_set_paths_invalid",
    "project_working_set_path_outside_workspace",
    "semantic_capability_binding_identity_mismatch",
    "semantic_capability_not_selected",
    "resolved_scope_expands_beyond_explicit_path",
    "workspace_not_authorized",
    "path_outside_authorized_roots",
    "path_outside_authorized_root",
    "path_independent_command_cwd_outside_bound_workspace",
    "command_working_directory_outside_request_scope",
    "command_working_directory_not_directory",
    "command_working_directory_invalid",
    "request_write_ceiling_tool_not_authorized",
    "request_write_ceiling_unresolved",
    "request_write_ceiling_command_unverified",
    "runtime_owned_security_state",
    "absolute_path_required",
    "parent_traversal_forbidden",
    "alternate_data_stream_forbidden",
    "device_path_forbidden",
    "unc_path_forbidden",
    "glob_path_forbidden",
    "nul_in_path",
    "empty_path",
    "relative_path_without_base",
    "path_resolution_failed",
    "reparse_or_symlink_forbidden",
    "authorized_root_delete_forbidden",
    "project_sync_target_outside_request_scope",
    "project_sync_root_required",
    "project_sync_root_conflict",
    "project_sync_strategy_invalid",
    "workspace_scope",
    "workspace_not_authorized",
)
_SCOPE_CODE_SET = frozenset(_SCOPE_ERROR_CODES)


def _error_code(raw: object) -> str:
    """Preserve exact *known* Runtime cause; unknown causes fail closed."""
    value = str(raw or "").strip()
    if not value:
        return "SCOPE_REJECTION_MISSING_CAUSE"
    # PathSecurityError's original string is <stable-code>:<target-path>.
    # Never parse/forward the target path itself.
    head = value.split(":", 1)[0].strip().lower()
    if head in _SCOPE_CODE_SET:
        return head.upper()
    if head == "permissionerror":
        return "SCOPE_PERMISSION_DENIED"
    if head == "filenotfounderror":
        return "SCOPE_RESOURCE_NOT_FOUND"
    if head == "notadirectoryerror":
        return "SCOPE_PARENT_NOT_DIRECTORY"
    if head in {"oserror", "valueerror", "runtimeerror"}:
        return "SCOPE_FILESYSTEM_OR_POLICY_EXCEPTION"
    return "UNMAPPED_SCOPE_PREFLIGHT_CAUSE"


def classify_scope_preflight(tool: object, error: object = "", *, allowed: bool) -> dict:
    """Typed causal stage / recovery, never a filesystem fact or scope grant."""
    import hashlib

    tool_name = str(tool or "")
    code = _error_code(error) if not allowed else "SCOPE_ALLOWED"
    field = {
        "EXPLICIT_PATH_SCOPE_REQUIRES_WORKSPACE": "workspace",
        "EXPLICIT_PATH_SCOPE_REQUIRES_PATHS": "paths",
        "EXPLICIT_PATH_SCOPE_REQUIRES_PATH": "path",
        "EXPLICIT_PATH_SCOPE_REQUIRES_ROOT": "root",
        "PROJECT_WORKING_SET_PATHS_INVALID": "paths",
        "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST": "path",
        "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY": "path",
        "DIRECTORY_PARENT_NOT_A_DIRECTORY": "path",
    }.get(code, "")
    if allowed:
        category, transition, stage = "READY_FOR_TOOL_ADMISSION", "CONTINUE", "ADMITTED_SCOPE"
    elif code in {
        "DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY", "DIRECTORY_PARENT_NOT_A_DIRECTORY",
        "SCOPE_RESOURCE_NOT_FOUND", "SCOPE_PARENT_NOT_DIRECTORY",
    }:
        category, transition, stage = (
            "PREREQUISITE_NOT_MET", "PREREQUISITE_DISCOVERY", "PARENT_DIRECTORY",
        )
    elif code == "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST":
        category, transition, stage = "ACTION_TARGET_MISMATCH", "ACTION_REPAIR", "ORIGINAL_REQUEST_BINDING"
    elif field or code == "INSPECT_PROJECT_SCOPE_UNSUPPORTED_FIELDS":
        category, transition, stage = "ACTION_FIELD_INVALID", "ACTION_REPAIR", "ACTION_SCHEMA"
    elif code == "WORKSPACE_NOT_FOUND_OR_NOT_DIRECTORY":
        category, transition, stage = (
            "PROJECT_WORKSPACE_UNAVAILABLE_OR_NOT_DIRECTORY", "PREREQUISITE_DISCOVERY",
            "PROJECT_WORKSPACE",
        )
    elif code.startswith("SEMANTIC_CAPABILITY_"):
        category, transition, stage = "CAPABILITY_BINDING_BLOCKED", "ACTION_REPAIR", "CAPABILITY"
    elif code.startswith("REQUEST_WRITE_CEILING_"):
        category, transition, stage = (
            "AUTHORIZATION_OR_SCOPE_BLOCKED", "REVIEW_WRITE_CEILING", "REQUEST_WRITE_CEILING",
        )
    elif code in {
        "PATH_OUTSIDE_AUTHORIZED_ROOTS", "PATH_OUTSIDE_AUTHORIZED_ROOT",
        "RUNTIME_OWNED_SECURITY_STATE", "REPARSE_OR_SYMLINK_FORBIDDEN",
        "DIRECTORY_PARENT_ACCESS_DENIED", "SCOPE_PERMISSION_DENIED",
    }:
        category, transition, stage = (
            "AUTHORIZATION_OR_SCOPE_BLOCKED", "REVIEW_AUTHORIZATION",
            "WRITE_AUTHORIZATION" if tool_name == "create_directory" else "READ_AUTHORIZATION",
        )
    elif code in {
        "RESOLVED_SCOPE_EXPANDS_BEYOND_EXPLICIT_PATH", "WORKSPACE_NOT_AUTHORIZED",
        "PROJECT_WORKING_SET_PATH_OUTSIDE_WORKSPACE",
        "PROJECT_SYNC_TARGET_OUTSIDE_REQUEST_SCOPE",
        "COMMAND_WORKING_DIRECTORY_OUTSIDE_REQUEST_SCOPE",
    }:
        category, transition, stage = (
            "AUTHORIZATION_OR_SCOPE_BLOCKED", "ACTION_REPAIR", "REQUEST_READ_SCOPE",
        )
    elif code in {
        "ABSOLUTE_PATH_REQUIRED", "PARENT_TRAVERSAL_FORBIDDEN",
        "ALTERNATE_DATA_STREAM_FORBIDDEN", "DEVICE_PATH_FORBIDDEN",
        "UNC_PATH_FORBIDDEN", "GLOB_PATH_FORBIDDEN", "NUL_IN_PATH",
        "EMPTY_PATH", "RELATIVE_PATH_WITHOUT_BASE",
    }:
        category, transition, stage = "UNSAFE_OR_INVALID_PATH", "ACTION_REPAIR", "PATH_CANONICALIZATION"
    else:
        category, transition, stage = (
            "RUNTIME_SCOPE_CONTRACT_UNRESOLVED", "RUNTIME_CAUSAL_REVIEW",
            "UNCLASSIFIED_PREFLIGHT" if code == "UNMAPPED_SCOPE_PREFLIGHT_CAUSE"
            else "SCOPE_PREFLIGHT",
        )
    if tool_name == "create_directory" and code == "SCOPE_ALLOWED":
        stage = "EXACT_DIRECTORY_WRITE_SCOPE"
    return {
        "schema": SCHEMA,
        "tool": tool_name if tool_name in ACTION_FIELDS else "OTHER",
        "scope_allowed": bool(allowed),
        "reason_code": code,
        "cause_digest": hashlib.sha256(str(error or "").encode("utf-8")).hexdigest(),
        "cause_classification": (
            "MAPPED_RUNTIME_CAUSE" if code not in {
                "UNMAPPED_SCOPE_PREFLIGHT_CAUSE", "SCOPE_REJECTION_MISSING_CAUSE"}
            else "RUNTIME_CAUSE_UNCLASSIFIED_FAIL_CLOSED"
        ),
        "category": category, "failure_stage": stage,
        "required_field": field,
        "path_role": (
            "PROJECT_WORKSPACE" if tool_name in PROJECT_WORKSPACE_TOOLS
            else "ACTION_PATH" if tool_name in ACTION_FIELDS else "OTHER"
        ),
        "eligible_transition": transition,
        "filesystem_absence_verified": False,
        "can_expand_authorized_scope": False,
        "authorization_granted": False,
        "tool_admission_still_required": True,
    }


def current_action_readiness(
    evidence: Mapping | None, action_ledger: Mapping | None,
) -> list[dict]:
    """Expose only the most recent scoped preflight blockers to Planner."""
    output = []
    for action_id, row in list((evidence or {}).items())[-12:]:
        if not isinstance(row, Mapping):
            continue
        diagnostic = row.get("scope_diagnostic")
        if not isinstance(diagnostic, Mapping):
            continue
        if diagnostic.get("schema") != SCHEMA:
            continue
        if diagnostic.get("scope_allowed") is True:
            continue
        if row.get("evidence_status", "ACTIVE") != "ACTIVE":
            continue
        # Do not copy any paths, exception strings, or untrusted proposal.
        accepted = {
            key: diagnostic.get(key) for key in (
                "schema", "tool", "scope_allowed", "reason_code",
                "cause_digest", "cause_classification", "failure_stage",
                "category", "required_field", "path_role", "eligible_transition",
                "filesystem_absence_verified", "can_expand_authorized_scope",
                "authorization_granted", "tool_admission_still_required",
            )
        }
        accepted["evidence_ref"] = str(action_id)[:160]
        output.append(accepted)
    return output[-8:]
