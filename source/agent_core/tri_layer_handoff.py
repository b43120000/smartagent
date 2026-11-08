"""Distinct-status Blackboard handoff for Goal, Prerequisite and Executor.

This module owns NO tools, authorizations, mutations or stage completion.
A child Executor terminal is *evidence*; parent agents must independently
decide readiness and goal-stage verification. Status keys are deliberately
different to avoid interpreting one layer's state as another's.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_TRI_LAYER_HANDOFF_V1"
PREREQUISITE_HANDOFF_STATES = frozenset({
    "REPLAN_REQUIRED", "READINESS_RECHECK_REQUIRED", "AWAITING_AUTHORIZATION",
    "REVIEW_REQUIRED",
})
GOAL_STAGE_HANDOFF_STATES = frozenset({
    "WAITING_FOR_PREREQUISITE", "AWAITING_STAGE_VERIFICATION",
    "WAITING_FOR_AUTHORIZATION", "WAITING_FOR_REVIEW",
})
_EXECUTOR_TERMINAL = frozenset({"VERIFIED", "BLOCKED", "NEEDS_PREREQUISITE"})
_SCOPE_REASON = ("SCOPE", "PATH_NOT_AUTHORIZED", "EXPLICIT_PATH",
                 "PERMISSION", "ACCESS_DENIED", "WRITE_CEILING")
_MISSING_REASON = ("PARENT_MISSING", "PREREQUISITE", "DEPENDENCY",
                   "ENVIRONMENT_MISSING", "SOURCE_LOCATION", "SOURCE_REQUIRED")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_:.\-]{0,155}$")


def _reason(value: object) -> str:
    original = str(value or "").upper().split(":", 1)[0]
    return original if _SAFE_CODE.fullmatch(original) else "BLOCKER_REQUIRES_REVIEW"


def classify_parent_route(executor_terminal: str, reason_code: str) -> dict:
    """Routing recommendations, NOT a verified parent stage transition."""
    if executor_terminal not in _EXECUTOR_TERMINAL:
        raise ValueError("tri_layer_executor_terminal_invalid")
    code = _reason(reason_code)
    if executor_terminal == "VERIFIED":
        return {
            "prerequisite_handoff_state": "READINESS_RECHECK_REQUIRED",
            "prerequisite_issue_code": "EXECUTOR_POSTCONDITION_VERIFIED",
            "goal_stage_handoff_state": "AWAITING_STAGE_VERIFICATION",
            "goal_stage_reason_code": "CHILD_EXECUTOR_VERIFIED",
            "next_owner": "GOAL_DRIVER_AND_PREREQUISITE",
        }
    if any(term in str(reason_code).upper() for term in _SCOPE_REASON):
        return {
            "prerequisite_handoff_state": "AWAITING_AUTHORIZATION"
            if any(term in str(reason_code).upper() for term in ("PERMISSION_DENIED", "NOT_AUTHORIZED", "WRITE_CEILING"))
            else "REPLAN_REQUIRED",
            "prerequisite_issue_code": "SCOPE_ADMISSION_REVIEW",
            "goal_stage_handoff_state": "WAITING_FOR_PREREQUISITE",
            "goal_stage_reason_code": "EXECUTOR_SCOPE_BLOCKED",
            "next_owner": "PREREQUISITE",
        }
    if executor_terminal == "NEEDS_PREREQUISITE" or any(
            term in str(reason_code).upper() for term in _MISSING_REASON):
        return {
            "prerequisite_handoff_state": "REPLAN_REQUIRED",
            "prerequisite_issue_code": "OBJECTIVE_PREREQUISITE_MISSING",
            "goal_stage_handoff_state": "WAITING_FOR_PREREQUISITE",
            "goal_stage_reason_code": "EXECUTOR_PREREQUISITE_PENDING",
            "next_owner": "PREREQUISITE",
        }
    return {
        "prerequisite_handoff_state": "REVIEW_REQUIRED",
        "prerequisite_issue_code": "EXECUTOR_BLOCKER_REQUIRES_REVIEW",
        "goal_stage_handoff_state": "WAITING_FOR_REVIEW",
        "goal_stage_reason_code": "EXECUTOR_BLOCKED_NOT_STAGE_COMPLETE",
        "next_owner": "GOAL_DRIVER_AND_PREREQUISITE",
    }


def build_handoff(record: Mapping) -> dict:
    """Three independently namespaced states, bound to one Executor receipt."""
    terminal = str(record.get("terminal_status") or "")
    route = classify_parent_route(terminal, str(record.get("reason_code") or ""))
    parent_id = str(record.get("parent_task_id") or record["task_id"])
    parent_epoch = str(record.get("parent_task_epoch") or record["task_epoch"])
    origin = "|".join((
        str(record["task_id"]), str(record["task_epoch"]),
        str(record["request_id"]), str(record.get("reason_code", "")),
        terminal, parent_id, parent_epoch, str(record.get("stage_id", "")),
    ))
    handoff_id = "HOF-" + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:24].upper()
    return {
        "schema": SCHEMA, "handoff_id": handoff_id,
        "handoff_delivery_state": "PENDING_PARENT_CONSUMPTION",
        "executor_handoff": {
            "executor_terminal_status": terminal,
            "executor_task_id": record["task_id"],
            "executor_task_epoch": record["task_epoch"],
            "executor_request_id": record["request_id"],
            "executor_reason_code": record["reason_code"],
            "executor_evidence_refs": list(record.get("evidence_refs") or []),
            "executor_goal_verified": bool(record.get("goal_verified")),
            "executor_result_not_parent_completion": True,
        },
        "prerequisite_handoff": {
            "prerequisite_handoff_state": route["prerequisite_handoff_state"],
            "prerequisite_issue_code": route["prerequisite_issue_code"],
            "prerequisite_owner_task_id": parent_id,
            "prerequisite_owner_task_epoch": parent_epoch,
            "prerequisite_source_executor_task_id": record["task_id"],
            "prerequisite_readiness_verdict": "NOT_EVALUATED",
            "prerequisite_readiness_evidence_refs": [],
            "prerequisite_action_authorized": False,
        },
        "goal_stage_handoff": {
            "goal_stage_handoff_state": route["goal_stage_handoff_state"],
            "goal_stage_reason_code": route["goal_stage_reason_code"],
            "goal_owner_task_id": parent_id,
            "goal_owner_task_epoch": parent_epoch,
            "goal_stage_id": str(record.get("stage_id") or ""),
            "goal_stage_verification_verdict": "NOT_EVALUATED",
            "goal_stage_evidence_refs": [],
            "goal_stage_completion_inferred": False,
        },
        "next_parent_owner": route["next_owner"],
        "authorization_granted": False,
        "stage_transition_committed": False,
    }


def project_handoff(record: Mapping | None) -> dict:
    if not isinstance(record, Mapping):
        return {}
    handoff = record.get("tri_layer_handoff")
    if not isinstance(handoff, Mapping) or handoff.get("schema") != SCHEMA:
        return {}
    return {
        "schema": SCHEMA,
        "handoff_id": handoff["handoff_id"],
        "handoff_delivery_state": handoff["handoff_delivery_state"],
        "executor_handoff": dict(handoff["executor_handoff"]),
        "prerequisite_handoff": dict(handoff["prerequisite_handoff"]),
        "goal_stage_handoff": dict(handoff["goal_stage_handoff"]),
        "next_parent_owner": handoff["next_parent_owner"],
        "authorization_granted": False,
        "stage_transition_committed": False,
    }
