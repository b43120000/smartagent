"""Deterministic, bounded first-FULL report_progress normalization.

No plan, success condition, Tool Action, permission or Action Evidence is invented.
Initial CONTINUE may cite REQUEST_ACCEPTED only as a status marker, *never*
as proof of execution or terminal success.
"""
from __future__ import annotations

from collections.abc import Mapping

SCHEMA = "SMARTAGENT_INITIAL_PROGRESS_NORMALIZATION_V1"


def normalize_initial_progress(payload: Mapping, *, has_plan: bool,
                               action_result_count: int = 0) -> tuple[dict, dict]:
    out = dict(payload or {})
    changes: list[str] = []
    report = {
        "schema": SCHEMA, "applied": False, "normalized_fields": changes,
        "authority_granted": False, "terminal_evidence_created": False,
        "status_marker_only": False,
    }
    if (has_plan or action_result_count != 0
            or out.get("tool") != "report_progress"
            or str(out.get("decision", "")).strip().upper() != "CONTINUE"
            or str(out.get("outcome", "PENDING")).strip().upper() != "PENDING"):
        return out, report

    # An initial CONTINUE is PENDING regardless of the eventual goal. This
    # does not select or modify the Planner's success/failure conditions.
    if "outcome" not in out:
        out["outcome"] = "PENDING"
        changes.append("outcome")

    # No completed Action Results can exist at bootstrap; status references
    # are non-action markers, NOT a verification PASS.
    if "evidence_refs" not in out or out.get("evidence_refs") == []:
        out["evidence_refs"] = ["REQUEST_ACCEPTED"]
        changes.append("evidence_refs")
        report["status_marker_only"] = True

    # Use Planner-authored text rather than constructing an environment fact.
    if (out.get("base_evaluation") in (None, "")
            and isinstance(out.get("current_focus"), str)
            and out["current_focus"].strip()):
        out["base_evaluation"] = out["current_focus"].strip()
        changes.append("base_evaluation")

    steps = out.get("steps")
    if isinstance(steps, list) and 1 <= len(steps) <= 32:
        canonical = []
        for row in steps:
            if not isinstance(row, dict):
                break
            value = row.get("step")
            if isinstance(value, bool):
                break
            if isinstance(value, str):
                if not value.isdecimal() or value.startswith("0"):
                    break
                value = int(value)
            if type(value) is not int or value != len(canonical) + 1:
                break
            status = str(row.get("status", "")).upper().strip()
            if status not in {"PENDING", "IN_PROGRESS", "COMPLETED", "COMPLETE"}:
                break
            canonical.append(status)
        if len(canonical) == len(steps):
            if "total_steps" not in out:
                out["total_steps"] = len(canonical)
                changes.append("total_steps")
            if "current_step" not in out:
                completed = 0
                for status in canonical:
                    if status not in {"COMPLETED", "COMPLETE"}:
                        break
                    completed += 1
                # All-completed CONTINUE is inconsistent: do not fabricate a
                # valid step position or suppress TaskProgress's rejection.
                if completed < len(canonical):
                    out["current_step"] = completed
                    changes.append("current_step")
    report["applied"] = bool(changes)
    return out, report



# The first FULL plan is not an accepted TaskProgressLedger until the entire
# Progress + Action batch passes admission.  However, its Planner-authored
# fields may be reused during a strictly format-only repair.  Never reuse a
# decision, evidence_ref, scope, completion verdict, or actual action here.
FIRST_FULL_PLAN_FIELDS = (
    "base_evaluation", "total_steps", "current_step", "steps",
    "completion_contract",
)


def _reusable_plan_field(name: str, value: object) -> bool:
    import math
    if name in {"total_steps", "current_step"}:
        return (type(value) in {int, float} and math.isfinite(value)
                and (value > 0 if name == "total_steps" else value >= 0))
    if name == "base_evaluation":
        return isinstance(value, str) and bool(value.strip())
    if name == "steps":
        return (isinstance(value, list) and 1 <= len(value) <= 32
                and all(isinstance(row, dict) for row in value))
    if name == "completion_contract":
        return isinstance(value, dict) and bool(value)
    return False


def remember_unadmitted_first_full(
    previous: Mapping | None, payload: Mapping, *,
    request_id: str, task_epoch: str, round_id: int,
) -> dict:
    """Preserve only Planner-authored valid-shaped plan fields, without acceptance.

    This cache is request/epoch bound and explicitly carries no executable
    authority. Even after field merging, the entire proposal must pass the
    normal Progress, commit, scope, Tool and evidence gates.
    """
    from copy import deepcopy
    old = dict(previous or {})
    if (old.get("request_id") != request_id
            or old.get("task_epoch") != task_epoch):
        old = {}
    fields = deepcopy(dict(old.get("fields") or {}))
    for name in FIRST_FULL_PLAN_FIELDS:
        if name in payload and _reusable_plan_field(name, payload[name]):
            fields[name] = deepcopy(payload[name])
    if not fields:
        return {}
    return {
        "schema": "SMARTAGENT_UNADMITTED_FIRST_FULL_DRAFT_V1",
        "request_id": request_id,
        "task_epoch": task_epoch,
        "source_round": int(round_id),
        "fields": fields,
        "admitted": False, "executed": False,
        "authorization_granted": False, "evidence_created": False,
    }


def materialize_unadmitted_first_full(
    payload: Mapping, draft: Mapping | None, *,
    request_id: str, task_epoch: str, field_repair_active: bool,
) -> tuple[dict, list[str]]:
    """Fill omitted bootstrap plan fields only in a genuine field-repair turn.

    An explicit PATCH is never secretly promoted into FULL.  The Planner must
    choose FULL while no accepted ledger exists.  A conflicting explicit value
    is preserved for normal validation; never silently overwrite it.
    """
    from copy import deepcopy
    result = deepcopy(dict(payload))
    old = dict(draft or {})
    if (not field_repair_active
            or old.get("request_id") != request_id
            or old.get("task_epoch") != task_epoch
            or result.get("tool") != "report_progress"
            or str(result.get("decision", "")).upper() != "CONTINUE"
            or str(result.get("outcome", "PENDING")).upper() != "PENDING"
            or result.get("progress_update", "FULL") != "FULL"):
        return result, []
    filled = []
    for name in FIRST_FULL_PLAN_FIELDS:
        if name not in result and name in (old.get("fields") or {}):
            result[name] = deepcopy(old["fields"][name])
            filled.append(name)
    return result, filled


__all__ = [
    "SCHEMA", "normalize_initial_progress", "FIRST_FULL_PLAN_FIELDS",
    "remember_unadmitted_first_full", "materialize_unadmitted_first_full",
]

