"""Deterministic execution policy for a pre-admitted v6 stage."""
from __future__ import annotations
from typing import Callable, Any
from .stage_protocol import validate_stage_manifest
from .stage_result import classify_tool_result

V6_READ_ONLY_TOOLS={"read_file","list_directory","inspect_directory","inspect_project_scope","compare_project_snapshot","extract_project_dependencies","find_file"}
V6_ALLOWED_TOOLS=V6_READ_ONLY_TOOLS|{"project_sync","apply_edit_plan","aggregate_verification"}

def preflight_stage(manifest: dict, actions: list[dict], *, workspace: str = "") -> dict:
    """Fail closed before action one; mutation resumption is not yet durable."""
    manifest=validate_stage_manifest(manifest,actions)
    if manifest["execution"] != "SEQUENTIAL": raise ValueError("stage_parallel_unsupported_v1")
    bad=[str(a.get("tool", "")) for a in actions if str(a.get("tool", "")) not in V6_ALLOWED_TOOLS]
    if bad: raise ValueError("stage_unsupported_or_unpreflightable_tool:"+",".join(bad))
    for action in actions:
        tool=str(action.get("tool", ""))
        if tool=="project_sync" and manifest["kind"]!="CONTEXT_SYNC": raise ValueError("stage_project_sync_requires_context_sync")
        if tool=="aggregate_verification":
            if manifest["kind"] not in {"EXECUTE_VERIFY","REPAIR"}: raise ValueError("stage_aggregate_requires_execute_or_repair")
            commands=action.get("commands"); timeout=action.get("timeout",120)
            if not workspace: raise ValueError("stage_aggregate_requires_authorized_workspace")
            if not isinstance(commands,list) or not commands or len(commands)>16 or not all(isinstance(x,str) and 0<len(x)<=1200 for x in commands): raise ValueError("stage_aggregate_commands_invalid")
            if type(timeout) is not int or not 1<=timeout<=600: raise ValueError("stage_aggregate_timeout_invalid")
        if tool=="apply_edit_plan":
            plan=action.get("plan")
            if not isinstance(plan,dict) or not str(plan.get("base_snapshot_id", "")): raise ValueError("stage_apply_requires_snapshot_bound_plan")
            if not workspace: raise ValueError("stage_apply_requires_authorized_workspace")
            from .edit_plan_contract import validate_edit_plan
            check=validate_edit_plan(workspace,plan)
            if str(check.get("status", "")) not in {"READY","VALID","PASS"}: raise ValueError("stage_apply_plan_preflight_failed")
    return manifest

def execute_stage(manifest: dict, actions: list[dict], execute: Callable[[dict], str]) -> dict:
    manifest = preflight_stage(manifest, actions)
    deps = {x["action_id"]: set(x["depends_on"]) for x in manifest["actions"]}
    outcome: dict[str, dict[str, Any]] = {}
    by_id = {str(x["action_id"]): x for x in actions}
    for action in actions:  # v1 intentionally preserves planner order.
        action_id = str(action["action_id"])
        blocked = [x for x in deps[action_id] if outcome.get(x, {}).get("status") != "COMMITTED"]
        if blocked:
            outcome[action_id] = {"status": "SKIPPED_DEPENDENCY", "depends_on": blocked}
            continue
        if manifest["stop_on_error"] and any(x.get("status") == "FAILED" for x in outcome.values()):
            outcome[action_id] = {"status": "SKIPPED_DEPENDENCY", "depends_on": ["stage_fail_stop"]}
            continue
        try:
            value=str(execute(action)); classified=classify_tool_result(str(action.get("tool", "")),value)
            outcome[action_id] = {"tool":str(action.get("tool", "")),"status": classified["status"], "result": value, "reason": classified["reason"], "evidence": classified["evidence"]}
        except Exception as exc:
            outcome[action_id] = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
            if not manifest["stop_on_error"]:
                continue
    return {"schema": "SMARTAGENT_STAGE_RESULT_V1", "stage_id": manifest["stage_id"], "seq": manifest["seq"], "status": "PASS" if all(x["status"] == "COMMITTED" for x in outcome.values()) else "FAIL", "actions": outcome}
