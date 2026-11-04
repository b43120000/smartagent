"""Goal-driven readiness decisions for rejected TASK_PLAN_V1 plans.

This module does not execute, authorize, or invent file modifications. A plan
with missing source-grounded fields enters an observation lane rather than the
strict, byte-identical canonical schema repair latch.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_PLAN_READINESS_V1"
REPAIR = "SCHEMA_REPAIR"
DETERMINISTIC = "DETERMINISTIC_REPAIR"
DISCOVER = "PREREQUISITE_DISCOVERY"
_READY = "EVIDENCE_READY"


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def classify_plan_readiness(
    submitted: object, normalized: object,
    validator: Mapping[str, object], canonical_repairs: list[dict],
) -> dict:
    """Separate lossless schema repair from source-evidence discovery.

    Only validator-reported missing required fields count. Do not turn an
    arbitrary plan-format defect into permission to run host observations.
    """
    plan = dict(normalized) if isinstance(normalized, Mapping) else {}
    raw = dict(submitted) if isinstance(submitted, Mapping) else {}
    missing_outer = _strings(
        validator.get("missing_fields")
        if validator.get("reason") == "MISSING_FIELDS" else []
    )
    nested = validator.get("edit_plan_validation")
    nested = nested if isinstance(nested, Mapping) else {}
    missing_edit = _strings(
        nested.get("missing_fields") if nested.get("reason") == "MISSING_FIELDS" else []
    )
    unresolved = []
    for name in missing_outer:
        if name not in plan:
            unresolved.append("plan." + name)
    edit = plan.get("edit_plan")
    edit = edit if isinstance(edit, Mapping) else {}
    for name in missing_edit:
        if name not in edit:
            unresolved.append("edit_plan." + name)
    if unresolved:
        kind = DISCOVER
    elif (missing_outer or missing_edit) and canonical_repairs:
        kind = DETERMINISTIC
    else:
        kind = REPAIR
    return {
        "schema": SCHEMA,
        "readiness": kind,
        "validator_reason": str(validator.get("reason", "")),
        "missing_fields": sorted(set(unresolved)),
        "canonical_repairs": list(canonical_repairs),
        "has_source_targets": bool(_strings(plan.get("files_to_read"))),
        "authorization_granted": False,
        "plan_mutation_executed": False,
    }


def discovery_observation(workspace: str) -> dict | None:
    """Inventory only; source-content retrieval remains a separate Tool decision."""
    if not workspace or "<" in workspace or ">" in workspace:
        return None
    return {"tool": "inspect_project_scope", "workspace": workspace}


def verified_inventory(result: Mapping[str, object]) -> str:
    """A real snapshot receipt, not just action exit=0 or model narrative."""
    value = result.get("data")
    snapshot = value if isinstance(value, Mapping) else result
    if (snapshot.get("schema") != "PROJECT_SNAPSHOT_V1"
            or not re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("snapshot_id", "")))
            or not isinstance(snapshot.get("files"), list)
            or not isinstance(snapshot.get("workspace_root"), str)):
        return ""
    fingerprint = {
        "snapshot_id": snapshot["snapshot_id"],
        "workspace_root": snapshot["workspace_root"],
        "file_count": len(snapshot["files"]),
    }
    return hashlib.sha256(json.dumps(
        fingerprint, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def transition_discovery_from_observation(
    action: Mapping[str, object], result: Mapping[str, object],
    prior: Mapping[str, object],
) -> dict:
    """Release the exact repair latch only on matching new Runtime evidence."""
    context = dict(prior)
    planned = next_actions = list(context.get("next_actions") or [])
    expected = next_actions[0] if next_actions else {}
    if (str(action.get("tool", "")) != "inspect_project_scope"
            or not expected or str(action.get("workspace", "")) != str(expected.get("workspace", ""))):
        return context
    proof = verified_inventory(result)
    if not proof:
        return {
            **context,
            "reason": "task_plan_discovery_unverified",
            "blocking_reason": "PREREQUISITE_OBSERVATION_UNVERIFIED",
            "next_actions": [], "required_action": "",
            "pause_immediately": True,
            "terminal_if_no_action": "PLAN_READINESS: project inventory produced no verifiable snapshot",
        }
    if proof == context.get("last_inventory_digest"):
        return {
            **context,
            "reason": "task_plan_discovery_no_new_evidence",
            "next_actions": [], "required_action": "",
            "pause_immediately": True,
            "terminal_if_no_action": "PLAN_READINESS: repeated identical inventory without new evidence",
        }
    return {
        **context,
        "reason": "task_plan_evidence_ready",
        "readiness": _READY,
        "runtime_state": "REPLAN_REQUIRED",
        "prerequisite_satisfied": True,
        "last_inventory_digest": proof,
        "required_action": "",
        "forbidden_actions": ["repair_task_plan"],
        "next_actions": [],
        "pause_immediately": False,
        "rules": [
            "Inventory evidence is available. Use new source-grounded observations if needed, then submit a NEW complete propose_task_plan.",
            "Do not fabricate files_to_modify, expected_observable_result, file content, or successful compilation.",
            "The new plan must pass the existing TASK_PLAN_V1 validator, scope and verification gates.",
        ],
    }
