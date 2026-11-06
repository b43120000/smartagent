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


__all__ = ["SCHEMA", "normalize_initial_progress"]
