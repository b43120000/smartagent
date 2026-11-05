"""Goal-aligned, evidence-guided Strategy Transition contract (GAST V1).

Planner chooses strategy. Runtime classifies its OWN committed evidence, validates
explicit Planner transitions against the immutable Task Progress goal and
Completion Contract, and retains normal Tool Admission as the final authority.

A ToolResult error is not evidence that a filesystem object or dependency is
absent. This module never chooses an installer, writes files, or grants rights.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

SCHEMA = "SMARTAGENT_GOAL_STRATEGY_TRANSITION_V1"
READINESS_SCHEMA = "SMARTAGENT_GOAL_TRANSITION_READINESS_V1"
KINDS = frozenset({
    "ACTION_REPAIR", "EVIDENCE_ACQUISITION",
    "PREREQUISITE_RESOLUTION", "STRATEGY_REPLAN",
})
READ_TOOLS = frozenset({
    "list_directory", "inspect_directory", "inspect_project_scope",
    "inspect_project_working_set", "find_file", "query_project",
    "read_file", "observe_fact",
})
SCOPED_REPAIR_FIELDS = {
    "explicit_path_scope_requires_workspace": "workspace",
    "explicit_path_scope_requires_path": "path",
    "explicit_path_scope_requires_paths": "paths",
    "query_project_structured_queries_required": "queries",
}
CODE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,119}$")


class GoalTransitionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _code(value: object) -> str:
    value = str(value or "").strip()
    return value if CODE.fullmatch(value) else ""


def goal_digest(goal: object) -> str:
    return hashlib.sha256(str(goal or "").encode("utf-8")).hexdigest()


def condition_choices(contract: Mapping | None) -> dict[str, str]:
    from .task_progress import condition_id_for
    out = {}
    for kind in ("success", "failure", "in_progress", "interrupted"):
        for value in list((contract or {}).get(kind) or [])[:64]:
            if type(value) is str and value.strip():
                out[condition_id_for(kind, value)] = kind.upper()
    return out


def _scope_error(*values: object) -> str:
    # Inspect only allowlisted codes; never propagate arbitrary ToolResult text.
    for value in values:
        data = str(value or "")
        for code in SCOPED_REPAIR_FIELDS:
            if code in data:
                return code
    return ""


def classify_runtime_evidence(record: Mapping, original: Mapping | None = None) -> dict:
    """Classify execution/observation without interpreting untrusted prose."""
    original = original or {}
    action = original.get("action") or {}
    if not isinstance(action, Mapping):
        action = {}
    tool = _code(record.get("tool") or action.get("tool"))
    result = str(original.get("result", "") or "")
    reason = _code(record.get("result_reason") or original.get("result_reason"))
    scope_error = _scope_error(reason, result, original.get("scope_error_code"))
    status = _code(record.get("result_status")).upper() or "UNKNOWN"
    verification = _code(record.get("effective_verification_status")
                         or record.get("verification_status")).upper() or "UNKNOWN"
    executed = record.get("executed") is True
    evidence_status = _code(record.get("evidence_status")).upper() or "ACTIVE"
    base = {
        "tool": tool, "result_status": status,
        "verification_status": verification,
        "executed": executed, "reason": reason,
        "scope_error_code": scope_error,
        "required_field": SCOPED_REPAIR_FIELDS.get(scope_error, ""),
        "evidence_status": evidence_status,
        "proves_missing_prerequisite": False,
        "authorizes_execution": False,
        "goal_completion_verified": False,
    }
    if evidence_status != "ACTIVE":
        base.update(classification="STALE_EVIDENCE",
                    eligible_transition="EVIDENCE_ACQUISITION")
    elif not executed and status in {"REJECTED", "FAILED"}:
        base.update(classification="PREEXECUTION_ADMISSION_BLOCKER",
                    eligible_transition="ACTION_REPAIR")
    elif not executed or status in {"UNKNOWN", "PARTIAL", "QUEUED", "RUNNING"}:
        base.update(classification="INDETERMINATE",
                    eligible_transition="EVIDENCE_ACQUISITION")
    elif status == "SUCCEEDED" and verification == "PASS":
        base.update(classification="VERIFIED_ACTION_SUCCESS",
                    eligible_transition="CONTINUE")
    elif (status in {"FAILED", "REJECTED"} and verification == "FAIL"):
        base.update(classification="VERIFIED_STRATEGY_FAILURE",
                    eligible_transition="STRATEGY_REPLAN")
    elif (reason in {"DIRECTORY_NOT_FOUND", "TARGET_NOT_FOUND", "FILE_NOT_FOUND"}):
        # A failed read, even when executed, does not by itself prove a
        # particular original-Goal prerequisite is absent or writable.
        base.update(classification="NEGATIVE_RESULT_UNCONFIRMED",
                    eligible_transition="EVIDENCE_ACQUISITION")
    else:
        base.update(classification="OUTCOME_UNVERIFIED",
                    eligible_transition="EVIDENCE_ACQUISITION")
    return base


def build_transition_readiness(runtime) -> dict:
    progress = getattr(runtime, "progress_ledger", None)
    goal = getattr(progress, "goal", "") or getattr(runtime, "task_request", "")
    contract = getattr(progress, "completion_contract", {}) or {}
    choices = condition_choices(contract)
    evidence = dict(getattr(runtime, "action_result_ledger", {}) or {})
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    conditions = dict(getattr(runtime, "condition_result_ledger", {}) or {})
    unresolved = [
        {"condition_id": cid, "condition_class": kind,
         "verification_status": _code((conditions.get(cid) or {}).get("status")).upper() or "UNKNOWN"}
        for cid, kind in choices.items()
        if kind == "SUCCESS" and str((conditions.get(cid) or {}).get("status", "")).upper() != "PASS"
    ][:16]
    candidates = []
    for aid in list(evidence)[-16:]:
        record = evidence.get(aid)
        if not isinstance(record, Mapping):
            continue
        action = actions.get(aid) or {}
        item = classify_runtime_evidence(record, action)
        if item["classification"] == "VERIFIED_ACTION_SUCCESS":
            continue
        candidates.append({"evidence_ref": str(aid)[:160],
                           "blocking_action_id": str(aid)[:160],
                           **item})
    selected = candidates[-8:]
    latest = selected[-1] if selected else {}
    return {
        "schema": READINESS_SCHEMA,
        "goal_anchor_sha256": goal_digest(goal),
        "goal_immutable": True,
        "planner_decides_strategy": True,
        "runtime_owns_evidence_and_admission": True,
        "unresolved_success_conditions": unresolved,
        "latest_blocker": latest,
        "recent_blockers": selected,
        "transition_options": sorted(KINDS),
        "proposal_required_for_normal_progress": False,
        "transition_proposal_schema": {
            "schema": SCHEMA,
            "required_fields": [
                "schema", "kind", "goal_anchor_sha256", "goal_condition_id",
                "evidence_ref", "blocked_action_id", "next_tool", "rationale",
            ],
        },
        "negative_result_is_not_missing_proof": True,
        "scope_repair_is_not_scope_expansion": True,
        "unchanged_goal_and_completion_contract_required": True,
    }


def validate_planner_transition(
    proposal: object, *, goal: str, contract: Mapping,
    evidence: Mapping, action_ledger: Mapping,
    operational: Sequence[Mapping],
    request_id: str, task_epoch: str,
) -> dict | None:
    """Verify optional Planner proposal; never select a strategy for Planner."""
    if proposal is None:
        return None
    fields = {
        "schema", "kind", "goal_anchor_sha256", "goal_condition_id",
        "evidence_ref", "blocked_action_id", "next_tool", "rationale",
    }
    if not isinstance(proposal, Mapping) or set(proposal) != fields:
        raise GoalTransitionError("goal_transition_schema_invalid")
    if proposal.get("schema") != SCHEMA or proposal.get("kind") not in KINDS:
        raise GoalTransitionError("goal_transition_kind_invalid")
    if proposal.get("goal_anchor_sha256") != goal_digest(goal):
        raise GoalTransitionError("goal_transition_goal_anchor_mismatch")
    cid = proposal.get("goal_condition_id")
    if type(cid) is not str or cid not in condition_choices(contract):
        raise GoalTransitionError("goal_transition_condition_not_declared")
    ref = proposal.get("evidence_ref")
    blocked = proposal.get("blocked_action_id")
    if type(ref) is not str or ref != blocked or not ref:
        raise GoalTransitionError("goal_transition_evidence_binding_invalid")
    row = evidence.get(ref)
    if not isinstance(row, Mapping) or row.get("evidence_status", "ACTIVE") != "ACTIVE":
        raise GoalTransitionError("goal_transition_evidence_not_active")
    if row.get("request_id", request_id) != request_id or row.get("task_epoch", task_epoch) != task_epoch:
        raise GoalTransitionError("goal_transition_evidence_identity_mismatch")
    if ref not in action_ledger:
        raise GoalTransitionError("goal_transition_action_not_found")
    if (type(proposal.get("rationale")) is not str
            or not 8 <= len(proposal["rationale"].strip()) <= 768):
        raise GoalTransitionError("goal_transition_rationale_invalid")
    next_tool = proposal.get("next_tool")
    if type(next_tool) is not str or not any(
            item.get("tool") == next_tool for item in operational):
        raise GoalTransitionError("goal_transition_next_action_not_submitted")
    fact = classify_runtime_evidence(row, action_ledger[ref])
    kind = proposal["kind"]
    # A failed tool execution/denied scope is not proof that the missing
    # prerequisite is real. For these cases Planner can repair or acquire.
    if kind == "PREREQUISITE_RESOLUTION":
        if not fact["proves_missing_prerequisite"]:
            raise GoalTransitionError("goal_transition_prerequisite_not_verified")
    elif kind == "STRATEGY_REPLAN":
        if fact["eligible_transition"] != "STRATEGY_REPLAN":
            raise GoalTransitionError("goal_transition_strategy_not_evidence_supported")
    elif kind == "ACTION_REPAIR":
        if fact["eligible_transition"] != "ACTION_REPAIR":
            raise GoalTransitionError("goal_transition_not_action_repair")
    # EVIDENCE_ACQUISITION can be selected after an uncertain read/rejection,
    # but it cannot assert a final result.
    return {
        "schema": SCHEMA, "kind": kind,
        "goal_anchor_sha256": goal_digest(goal),
        "goal_condition_id": cid,
        "evidence_ref": ref,
        "blocked_action_id": blocked,
        "next_tool": next_tool,
        "rationale_sha256": hashlib.sha256(proposal["rationale"].encode("utf-8")).hexdigest(),
        "evidence_classification": fact["classification"],
        "authorization_granted": False,
        "completion_verified": False,
    }


def repeat_denied_read_diagnostic(
    operational: Sequence[Mapping], *,
    evidence: Mapping, action_ledger: Mapping,
) -> dict | None:
    """Prevent only *identical* denied reads with no subsequent new evidence.

    Do not block a different tool, field-repaired action, or a repeat after new
    independently committed evidence. Never apply this to mutating actions.
    """
    if not operational or not evidence:
        return None
    from .action_loop_state import semantic_action_signature
    ids = list(evidence)
    for action in operational:
        if action.get("tool") not in READ_TOOLS:
            continue
        proposed = semantic_action_signature(action)
        for index in range(len(ids) - 1, -1, -1):
            aid = ids[index]
            row = evidence.get(aid) or {}
            old = action_ledger.get(aid) or {}
            old_action = old.get("action") or {}
            if old_action.get("tool") != action.get("tool"):
                continue
            if semantic_action_signature(old_action) != proposed:
                continue
            if row.get("evidence_status", "ACTIVE") != "ACTIVE":
                continue
            if (row.get("executed") is not False
                    or row.get("result_status") != "REJECTED"):
                continue
            # Any newer committed evidence may change the environment. Do
            # not infer it from a new Planner turn or Action ID alone.
            if index < len(ids) - 1:
                break
            return {
                "marker": "[GOAL_ALIGNMENT_REJECTED]",
                "reason": "goal_alignment_identical_denied_read_without_new_evidence",
                "blocked_action_id": str(aid)[:160],
                "tool": action.get("tool", ""),
                "suggestion": (
                    "The identical read was rejected before execution. Repair "
                    "the required Tool field or select a materially different "
                    "authorized evidence acquisition strategy. No absence or "
                    "permission can be inferred from the denial."
                ),
            }
    return None
