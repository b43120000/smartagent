"""Reconstruct persistent Goal prerequisites from current, trusted Runtime evidence.

This ledger is a deterministic materialized view over the durable Action Result
Ledger. It is not the latest Action Loop status, does not create permissions,
and cannot infer absence from rejected or UNKNOWN operations. Rebuilding the
view after each turn preserves unresolved gaps while allowing a newer, verified
observation to resolve them. It intentionally never reads the local filesystem.
"""
from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_GOAL_GAP_LEDGER_V1"
GAP_SCHEMA = "SMARTAGENT_GOAL_GAP_V1"
from .conditional_goal_policy import conditional_goal_policy


def _path_key(value: object) -> str:
    path = str(value or "").strip()
    if not path or len(path) > 2048:
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def _goal_link(goal: str, path: str) -> str:
    """An intent hint only; it never grants write permission."""
    if _path_key(path) not in _path_key(goal) and path.casefold() not in goal.casefold():
        return "UNBOUND"
    policy = conditional_goal_policy(goal)
    if policy["intent"] == "READ_ONLY_ONLY":
        return "READ_ONLY_OR_RESTRICTED"
    if policy["intent"] == "CONDITIONAL_REMEDIATION":
        return "CONDITIONAL_REMEDIATION_CANDIDATE"
    if policy["intent"] == "REMEDIATION_CANDIDATE":
        return "REMEDIATION_INTENT_CANDIDATE"
    return "UNBOUND"


def reconstruct_goal_gaps(runtime) -> dict:
    """Replay current-epoch directory observations; latest verified fact wins.

    Uses exact Runtime-produced receipts, not model statements or error strings.
    A rejected project inventory cannot resolve or create an absence fact.
    """
    from .goal_strategy_transition import classify_runtime_evidence
    ledger = dict(getattr(runtime, "action_result_ledger", {}) or {})
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    request_id = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    progress = getattr(runtime, "progress_ledger", None)
    goal = str(getattr(progress, "goal", "") or getattr(runtime, "task_request", "") or "")
    policy = conditional_goal_policy(goal)
    ordered = {}
    for action_id, row in ledger.items():
        if not isinstance(row, Mapping):
            continue
        if row.get("evidence_status", "ACTIVE") != "ACTIVE":
            continue
        if row.get("request_id") != request_id or row.get("task_epoch") != epoch:
            continue
        historical = actions.get(action_id) or {}
        observation = classify_runtime_evidence(row, historical)
        if observation.get("environment_observation_kind") != "directory.exists":
            continue
        exists = observation.get("environment_observed_exists")
        path = observation.get("environment_observed_path", "")
        key = _path_key(path)
        if not key or type(exists) is not bool:
            continue
        gap_id = "GAP-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper()
        previous = ordered.get(key)
        goal_link = _goal_link(goal, path)
        linked = goal_link in {"REMEDIATION_INTENT_CANDIDATE",
                               "CONDITIONAL_REMEDIATION_CANDIDATE"}
        status = ("RESOLVED" if linked and exists else
                  "UNRESOLVED" if linked else
                  "OBSERVED_PRESENT" if exists else "OBSERVED_ABSENCE")
        ordered[key] = {
            "schema": GAP_SCHEMA,
            "gap_id": gap_id,
            "kind": "DIRECTORY_EXISTENCE",
            "path": path,
            "predicate": "directory.exists",
            "observed_value": exists,
            "desired_value": True if linked else None,
            "status": status,
            "observed_fact_ref": str(action_id),
            "observation_status": "CONFIRMED",
            "goal_link": goal_link,
            "goal_condition_id": str(row.get("condition_id", "") or ""),
            "revision": (previous or {}).get("revision", 0) + 1,
            "resolved_by_evidence_ref": str(action_id) if exists and linked else "",
            "repair_candidates": (
                ["DISCOVER_SOURCE_IF_SCOPED", "INSPECT_PARENT_IF_SCOPED",
                 "VERIFY_WRITE_AUTHORIZATION", "CHECK_TARGET_CONFLICTS",
                 "CREATE_DIRECTORY_IF_AUTHORIZED", "VERIFY_RESULT"]
                if not exists and linked else
                ["INSPECT_PARENT_IF_SCOPED"] if not exists else []
            ),
            "prerequisites": ({
                "source_integrity": "UNKNOWN",
                "write_authorization": "UNKNOWN",
                "target_conflicts": "UNKNOWN",
                "parent_scope": "UNKNOWN",
                "independent_verification": "NOT_STARTED",
            } if not exists and linked else {}),
            "conditional_mutation": policy["conditional_remediation"],
            "restrictions": policy["restrictions"],
            "authorization_granted": False,
            "goal_completion_verified": False,
        }
    gaps = list(ordered.values())
    return {
        "schema": SCHEMA,
        "goal_policy": policy,
        "request_id": request_id,
        "task_epoch": epoch,
        "gaps": gaps[-32:],
        "active_gaps": [gap for gap in gaps if gap["status"] == "UNRESOLVED"][-32:],
        "known_absences": [gap for gap in gaps if gap["observed_value"] is False][-32:],
        "active_count": sum(gap["status"] == "UNRESOLVED" for gap in gaps),
        "storage": "RECONSTRUCTED_FROM_DURABLE_ACTION_LEDGER",
        "planner_strategy_authority": True,
        "scope_or_write_authorization_granted": False,
    }


def decision_grounding_diagnostics(operational, gap_view: Mapping) -> list[dict]:
    """Reject a *known impossible* project-root read before Tool dispatch.

    Discovery and mutations are not blocked by their mere lack of direct Goal
    progress. All permitted candidates still pass existing Tool Admission.
    """
    from .action_readiness import DIRECTORY_PRECONDITION_FIELDS
    # Tool preconditions apply to all confirmed absences, including read-only
    # tasks where a missing path is not itself an unmet Goal requirement.
    blockers = list(gap_view.get("known_absences") or gap_view.get("active_gaps") or [])
    if not blockers:
        return []
    out = []
    for action in operational:
        tool = str(action.get("tool", "") or "")
        field = DIRECTORY_PRECONDITION_FIELDS.get(tool)
        if not field:
            continue
        root = action.get(field)
        requested = _path_key(root)
        if not requested:
            continue
        for gap in blockers:
            missing = _path_key(gap.get("path"))
            if requested != missing and not requested.startswith(missing + "\\"):
                continue
            out.append({
                "marker": "[GOAL_ALIGNMENT_REJECTED]",
                "reason": "goal_gap_known_missing_workspace",
                "repair_scope": "GOAL_GAP_DECISION_GROUNDING",
                "action_id": str(action.get("action_id", "")),
                "tool": tool,
                "gap_id": gap["gap_id"],
                "evidence_ref": gap["observed_fact_ref"],
                "known_fact": "directory.exists=false",
                "required_precondition": "workspace.exists=true",
                "suggestion": (
                    "The requested workspace was confirmed absent by Runtime. "
                    "Choose an authorized parent/scope discovery or a separately "
                    "admitted create-directory prerequisite when the original "
                    "Goal permits mutation. Do not retry a project-root reader "
                    "until a new independent observation confirms existence."
                ),
            })
            break
    return out
