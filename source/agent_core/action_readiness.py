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


def _error_code(raw: object) -> str:
    value = str(raw or "")
    # Longest / most specific matches precede short generic "not_found".
    for code in (
        "workspace_not_found_or_not_directory",
        "explicit_path_scope_requires_workspace",
        "explicit_path_scope_requires_paths",
        "explicit_path_scope_requires_path",
        "explicit_path_scope_requires_root",
        "inspect_project_scope_unsupported_fields",
        "project_working_set_paths_invalid",
        "semantic_capability_binding_identity_mismatch",
        "semantic_capability_not_selected",
        "resolved_scope_expands_beyond_explicit_path",
        "workspace_not_authorized",
        "path_outside_authorized_roots",
        "request_write_ceiling_tool_not_authorized",
        "request_write_ceiling_unresolved",
        "request_write_ceiling_command_unverified",
    ):
        if value.startswith(code) or code in value:
            return code
    return "OTHER_SCOPE_REJECTION" if value else "NONE"


def classify_scope_preflight(tool: object, error: object = "", *, allowed: bool) -> dict:
    """Return bounded status codes, not attacker-controlled error text/paths."""
    tool_name = str(tool or "")
    code = _error_code(error) if not allowed else "SCOPE_ALLOWED"
    field = {
        "explicit_path_scope_requires_workspace": "workspace",
        "explicit_path_scope_requires_paths": "paths",
        "explicit_path_scope_requires_path": "path",
        "explicit_path_scope_requires_root": "root",
        "project_working_set_paths_invalid": "paths",
    }.get(code, "")
    if allowed:
        category, next_transition = "READY_FOR_TOOL_ADMISSION", "CONTINUE"
    elif field or code == "inspect_project_scope_unsupported_fields":
        category, next_transition = "ACTION_FIELD_INVALID", "ACTION_REPAIR"
    elif code == "workspace_not_found_or_not_directory":
        category, next_transition = "PROJECT_WORKSPACE_UNAVAILABLE_OR_NOT_DIRECTORY", "EVIDENCE_ACQUISITION"
    elif code.startswith("semantic_capability_"):
        category, next_transition = "CAPABILITY_BINDING_BLOCKED", "ACTION_REPAIR"
    elif code in {
        "resolved_scope_expands_beyond_explicit_path", "workspace_not_authorized",
        "path_outside_authorized_roots", "request_write_ceiling_tool_not_authorized",
        "request_write_ceiling_unresolved", "request_write_ceiling_command_unverified",
    }:
        category, next_transition = "AUTHORIZATION_OR_SCOPE_BLOCKED", "EVIDENCE_ACQUISITION"
    else:
        category, next_transition = "OTHER_PREFLIGHT_REJECTION", "EVIDENCE_ACQUISITION"
    return {
        "schema": SCHEMA,
        "tool": tool_name if tool_name in ACTION_FIELDS else "OTHER",
        "scope_allowed": bool(allowed),
        "reason_code": code,
        "category": category,
        "required_field": field,
        "path_role": ("PROJECT_WORKSPACE" if tool_name in PROJECT_WORKSPACE_TOOLS
                      else "ACTION_PATH" if tool_name in ACTION_FIELDS else "OTHER"),
        "eligible_transition": next_transition,
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
                "schema", "tool", "scope_allowed", "reason_code", "category",
                "required_field", "path_role", "eligible_transition",
                "filesystem_absence_verified", "can_expand_authorized_scope",
                "authorization_granted", "tool_admission_still_required",
            )
        }
        accepted["evidence_ref"] = str(action_id)[:160]
        output.append(accepted)
    return output[-8:]
