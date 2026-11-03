"""SCR V1: model-selected read capabilities, not model-issued authorization.

The Planner translates user semantics into this finite Runtime-owned registry.
Scope, ACL, write ceilings and execution evidence are checked separately.
"""
from __future__ import annotations
from collections.abc import Mapping
from types import MappingProxyType

SCHEMA = "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1"
CAPABILITY_TO_TOOL = MappingProxyType({
    "directory.list": "list_directory",
    "directory.inspect_metadata": "inspect_directory",
    "directory.find": "find_file",
    "project.inspect_scope": "inspect_project_scope",
    "project.inspect_working_set": "inspect_project_working_set",
})
TOOL_TO_CAPABILITY = MappingProxyType({
    tool: cap for cap, tool in CAPABILITY_TO_TOOL.items()
})


class SemanticCapabilityError(ValueError):
    pass


def normalize_resolution(proposal: object) -> dict:
    """Reject unknown entries, duplicates, oversized and authority-like keys."""
    if not isinstance(proposal, Mapping) or set(proposal) != {"schema", "capabilities"}:
        raise SemanticCapabilityError("semantic_capability_schema_invalid")
    if proposal.get("schema") != SCHEMA:
        raise SemanticCapabilityError("semantic_capability_schema_invalid")
    caps = proposal.get("capabilities")
    if (not isinstance(caps, list) or not 1 <= len(caps) <= 12
            or any(type(x) is not str or x not in CAPABILITY_TO_TOOL for x in caps)
            or len(set(caps)) != len(caps)):
        raise SemanticCapabilityError("semantic_capability_list_invalid")
    return {"schema": SCHEMA, "capabilities": list(caps)}


def verify_resolution_for_actions(proposal, previous, actions):
    """Validate semantic plan before admission; do not alter existing guards."""
    old = normalize_resolution(previous) if previous is not None else None
    new = normalize_resolution(proposal) if proposal is not None else old
    if new is None:
        return None  # Compatibility: existing canonical V9 tool admission.
    selected = set(new["capabilities"])
    if old is not None and not set(old["capabilities"]).issubset(selected):
        raise SemanticCapabilityError("semantic_capability_removal_forbidden")
    for action in actions:
        tool = str(action.get("tool", "") or "")
        cap = TOOL_TO_CAPABILITY.get(tool)
        if cap is not None and cap not in selected:
            raise SemanticCapabilityError("semantic_capability_not_selected:" + tool)
    return new


def render_planner_contract():
    registry = "; ".join(k + " => " + v for k, v in CAPABILITY_TO_TOOL.items())
    return (
        "SCR V1: For read-only directory/project inspection requests, "
        "translate the user's intent into report_progress.capability_resolution "
        "on the first Progress, with schema " + SCHEMA
        + " and capabilities selected from the finite registry. "
        "Use the relevant subset of these finite canonical capabilities: "
        + registry + ". Mixed goals need all required capabilities. "
        "Choose by meaning, never by matching literal user words. "
        "Preserve earlier selections in any subsequent cumulative expansion. "
        "This proposal is NOT authorization: never grant write, broaden paths, "
        "override ACL or claim success/evidence from this field."
    )
