"""Strict, version-6 stage-manifest validation.

The manifest travels on the trailing turn_commit so v5 envelopes remain
unchanged.  It is deliberately data-only: semantic planning is still owned by
the planner, while this module owns deterministic admission rules.
"""
from __future__ import annotations

from typing import Any

STAGE_KINDS = {"CONTEXT_SYNC", "EXECUTE_VERIFY", "REPAIR", "FINALIZE"}
TASK_SIZES = {"SMALL", "MEDIUM", "LARGE"}
EXECUTIONS = {"SEQUENTIAL", "PARALLEL"}
RESULT_POLICIES = {"COMPACT", "FULL"}
READ_ONLY_PARALLEL_TOOLS = {
    "read_file", "list_directory", "inspect_directory", "inspect_project_scope",
    "compare_project_snapshot", "extract_project_dependencies", "find_file",
    "inspect_semantic_map",
}

_TOP = {"stage_id", "seq", "kind", "task_size", "execution", "result_policy", "stop_on_error", "actions", "snapshot_id"}
_ACTION = {"action_id", "depends_on"}


class StageManifestError(ValueError):
    pass


def _fail(reason: str) -> None:
    raise StageManifestError(reason)


def validate_stage_manifest(manifest: object, actions: list[dict]) -> dict:
    """Validate the complete stage before any action can be admitted."""
    if not isinstance(manifest, dict): _fail("stage_manifest_not_object")
    unknown = set(manifest) - _TOP
    if unknown: _fail("stage_unknown_field:" + ",".join(sorted(unknown)))
    required = {"stage_id", "seq", "kind", "task_size", "execution", "result_policy", "stop_on_error", "actions"}
    missing = required - set(manifest)
    if missing: _fail("stage_missing_field:" + ",".join(sorted(missing)))
    if not isinstance(manifest["stage_id"], str) or not manifest["stage_id"].strip(): _fail("stage_id_invalid")
    if type(manifest["seq"]) is not int or manifest["seq"] < 1: _fail("stage_seq_invalid")
    for key, allowed in (("kind", STAGE_KINDS), ("task_size", TASK_SIZES), ("execution", EXECUTIONS), ("result_policy", RESULT_POLICIES)):
        if not isinstance(manifest[key], str) or manifest[key] not in allowed: _fail(f"stage_{key}_invalid")
    if type(manifest["stop_on_error"]) is not bool: _fail("stage_stop_on_error_invalid")
    if manifest["result_policy"] != "COMPACT": _fail("stage_result_policy_unsupported_v1")
    if "snapshot_id" in manifest and not isinstance(manifest["snapshot_id"], str): _fail("stage_snapshot_id_invalid")
    declared = manifest["actions"]
    if not isinstance(declared, list): _fail("stage_actions_not_list")
    actual_ids = [str(a.get("action_id", "") or "") for a in actions]
    if len(actual_ids) != len(set(actual_ids)) or any(not x for x in actual_ids): _fail("stage_actual_action_ids_invalid")
    if len(declared) != len(actions): _fail("stage_action_count_mismatch")
    nodes: dict[str, set[str]] = {}
    for item in declared:
        if not isinstance(item, dict): _fail("stage_action_not_object")
        unknown = set(item) - _ACTION
        if unknown: _fail("stage_action_unknown_field:" + ",".join(sorted(unknown)))
        if set(item) != _ACTION or not isinstance(item["action_id"], str) or not item["action_id"]: _fail("stage_action_id_invalid")
        deps = item["depends_on"]
        if not isinstance(deps, list) or not all(isinstance(x, str) and x for x in deps): _fail("stage_depends_on_invalid")
        if len(deps) != len(set(deps)): _fail("stage_duplicate_dependency")
        nodes[item["action_id"]] = set(deps)
    if set(nodes) != set(actual_ids): _fail("stage_action_ids_mismatch")
    declared_order={str(item["action_id"]): index for index,item in enumerate(declared)}
    for action_id, deps in nodes.items():
        if action_id in deps or not deps <= set(nodes): _fail("stage_dependency_unknown_or_self")
    remaining = {k: set(v) for k, v in nodes.items()}
    while remaining:
        ready = {key for key, deps in remaining.items() if not deps}
        if not ready: _fail("stage_dependency_cycle")
        for key in ready: remaining.pop(key)
        for deps in remaining.values(): deps.difference_update(ready)
    for action_id, deps in nodes.items():
        if any(declared_order[dep] >= declared_order[action_id] for dep in deps): _fail("stage_manifest_not_topological")
    if manifest["execution"] == "PARALLEL":
        if any(nodes.values()): _fail("stage_parallel_dependencies_forbidden")
        by_id = {str(a["action_id"]): str(a.get("tool", "")) for a in actions}
        if any(by_id[x] not in READ_ONLY_PARALLEL_TOOLS for x in nodes): _fail("stage_parallel_unsafe_tool")
    return {**manifest, "actions": [{"action_id": x, "depends_on": sorted(nodes[x])} for x in actual_ids]}
