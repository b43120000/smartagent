"""Request-bound shared language for Planner, Replan, Progress and Tool Admission.

This is a *projection*, not a new authority: it describes exact contract
decisions from existing validators, never executes, invents semantic choices,
modifies Scope/ACL, or treats repair/held actions as Action Evidence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import re

SCHEMA = "SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1"
_ALLOWED_ROUTES = frozenset({
    "PASS_UNCHANGED", "REWRITE_SAFE_EQUIVALENT",
    "NEEDS_INFORMATION", "REJECT_REPLAN", "REQUIRES_APPROVAL",
    "HOLD_FOR_REPAIR", "ADMITTED", "DENIED",
})
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
_AUTH_REASONS = frozenset({
    "USER_APPROVAL_REQUIRED", "execution_confirmation_required",
    "security_approval_required", "COMMAND_ALLOW_ONCE_V1",
})
_SCOPE_REASONS = (
    "scope", "authorization", "access_denied", "permission",
    "write_ceiling", "outside_workspace", "outside_request", "forbidden",
)
_REPLAN_REASONS = frozenset({
    "semantic_path_planner_selection_mismatch",
    "semantic_path_planner_selection_conflict",
    "semantic_capability_selection_conflict",
    "preexecution_identical_strategy_replay",
    "run_command_working_directory_rejected",
    "action_replacement_not_deferred",
})


def _safe(value: object, default: str = "") -> str:
    word = str(value or "")
    return word if _SAFE_NAME.fullmatch(word) else default


def _field_names(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return sorted({_safe(k) for k in value if _safe(k)})[:48]


def _schema_fields(tool: str) -> list[str]:
    """Introspect the authoritative envelope registry, not a second schema."""
    try:
        from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
        spec = TOOL_ENVELOPE_SCHEMAS.get(tool)
        if not isinstance(spec, dict):
            return []
        fields = {"tool", "action_id", *spec["required"], *spec["optional"]}
        if tool not in {"report_progress", "final_response", "turn_commit"}:
            fields.update({"depends_on", "replaces_action_id"})
        return sorted(fields)
    except (ImportError, KeyError, TypeError):
        return []


def _contract(tool: str, reason: str, mode: str = "") -> str:
    r = reason.lower()
    if "approval" in r or reason in _AUTH_REASONS:
        return "AUTHORIZATION"
    if any(s in r for s in _SCOPE_REASONS):
        return "AUTHORIZATION"
    if "evidence" in r or reason.startswith("terminal_success"):
        return "EVIDENCE"
    if "prerequisite" in r or "missing_context" in r:
        return "PREREQUISITE"
    if "replan" in r or mode == "REPLAN_REQUIRED" or reason in _REPLAN_REASONS:
        return "REPLAN"
    if "pending_actions" in r or "hold" in r or "recovery" in r:
        return "ACTION_RECOVERY"
    if tool == "report_progress" or "progress" in r or "condition" in r:
        return "PROGRESS"
    if tool:
        return "ACTION"
    return "REQUEST"


def _route(reason: str, contract: str, mode: str = "") -> str:
    if reason in _AUTH_REASONS or "approval" in reason.lower():
        return "REQUIRES_APPROVAL"
    if contract == "AUTHORIZATION":
        return "DENIED"
    if (mode == "REPLAN_REQUIRED" or contract == "REPLAN"
            or reason in _REPLAN_REASONS):
        return "REJECT_REPLAN"
    if contract == "ACTION_RECOVERY":
        return "HOLD_FOR_REPAIR"
    return "NEEDS_INFORMATION"


def _origin(diagnostic: Mapping, submitted: Sequence[Mapping]) -> tuple[str, str, list[str], list[str]]:
    tool = _safe(diagnostic.get("tool"))
    action_id = _safe(diagnostic.get("action_id"))
    reason = str(diagnostic.get("reason") or "")
    # A reason with no tool is ambiguous; use the *actual* registry to assign
    # only when exactly one proposed envelope has the same defect class.
    if not tool and reason == "unexpected_field":
        candidates = []
        for call in submitted:
            if not isinstance(call, Mapping):
                continue
            name = _safe(call.get("tool"))
            expected = _schema_fields(name)
            if expected and set(call) - set(expected):
                candidates.append((name, _safe(call.get("action_id"))))
        if len(candidates) == 1:
            tool, action_id = candidates[0]
    observed = next((x for x in submitted if isinstance(x, Mapping)
                     and _safe(x.get("tool")) == tool
                     and (not action_id or _safe(x.get("action_id")) == action_id)), {})
    expected = _schema_fields(tool)
    actual = _field_names(observed)
    return tool, action_id, actual, expected


def diagnostic_receipts(diagnostics: Sequence[Mapping], *,
                        submitted: Sequence[Mapping],
                        ccrc: Mapping | None = None) -> list[dict]:
    mode = str((ccrc or {}).get("mode") or "")
    rows = []
    for diagnostic in list(diagnostics or [])[:24]:
        if not isinstance(diagnostic, Mapping):
            continue
        reason = _safe(diagnostic.get("reason"), "UNKNOWN_DIAGNOSTIC")
        tool, action_id, actual, expected = _origin(diagnostic, submitted)
        contract = _contract(tool, reason, mode)
        fields = []
        # Only trusted field names; never serialize raw values/paths/commands.
        for field in list(diagnostic.get("field_issues") or [])[:24]:
            if isinstance(field, Mapping):
                name = _safe(field.get("field") or field.get("path"))
                if name:
                    fields.append(name)
        extras = sorted(set(actual) - set(expected)) if expected else []
        missing = []
        if tool and expected:
            from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
            spec = TOOL_ENVELOPE_SCHEMAS.get(tool) or {}
            required = set(spec.get("required") or {}) | {"tool", "action_id"}
            missing = sorted(required - set(actual))
        rows.append({
            "contract": contract,
            "reason_code": reason,
            "tool": tool,
            "action_id": action_id,
            "decision": _route(reason, contract, mode),
            "field_issues": sorted(set(fields)),
            "unexpected_keys": extras[:24],
            "missing_keys": missing[:24],
            "expected_keys": expected[:64],
            "origin": "PLANNER_SUBMISSION_OR_UNADMITTED_RESTORATION",
            "requires_planner_choice": contract in {"REPLAN", "PREREQUISITE"},
            "authorization_granted": False,
            "executed": False,
        })
    return rows


def build_transaction(*, request_id: str, task_epoch: str, original_goal: str,
                      round_id: int, phase: str,
                      diagnostics: Sequence[Mapping] = (),
                      submitted: Sequence[Mapping] = (),
                      accepted: Sequence[Mapping] = (),
                      pending: Sequence[Mapping] = (),
                      recovery_events: Sequence[Mapping] = (),
                      ccrc: Mapping | None = None,
                      canonical: Sequence[Mapping] = (),
                      evidence_count: int = 0) -> dict:
    """One stable bounded vocabulary projected across all existing validators."""
    phase = phase if phase in {"REJECTED", "ADMITTED", "PROPOSED"} else "PROPOSED"
    issues = diagnostic_receipts(diagnostics, submitted=submitted, ccrc=ccrc)
    held = sorted({
        _safe(row.get("action_id")) for row in list(pending or [])[:32]
        if isinstance(row, Mapping) and _safe(row.get("action_id"))
    })
    acts = sorted({
        _safe(row.get("action_id")) for row in list(accepted or [])[:32]
        if isinstance(row, Mapping) and row.get("tool") not in
        {"report_progress", "turn_commit", "final_response"}
        and _safe(row.get("action_id"))
    })
    mode = _safe((ccrc or {}).get("mode"), "NONE")
    goal_digest = sha256(str(original_goal or "").encode("utf-8")).hexdigest()
    # Never render the original goal; its digest only pins replan consistency.
    replay_events = [
        {"status": _safe(row.get("status"), "UNKNOWN"),
         "action_ids": [_safe(x) for x in list(row.get("action_ids") or [])[:32] if _safe(x)],
         "authorization_granted": False, "executed": False}
        for row in list(recovery_events or [])[-6:]
        if isinstance(row, Mapping)
    ]
    return {
        "schema": SCHEMA,
        "request_id": str(request_id),
        "task_epoch": str(task_epoch),
        "round": int(round_id),
        "original_goal_sha256": goal_digest,
        "phase": phase,
        "progress_contract": "PROGRESS",
        "replan_contract": "REPLAN",
        "request_contract": "REQUEST",
        "prerequisite_contract": "PREREQUISITE",
        "action_contract": "ACTION",
        "recovery_contract": "ACTION_RECOVERY",
        "evidence_contract": "EVIDENCE",
        "authorization_contract": "AUTHORIZATION",
        "ccrc_mode": mode,
        "issues": issues,
        "canonical_decisions": [
            {"contract": _safe(row.get("contract")), "decision": _safe(row.get("decision")),
             "reason_code": _safe(row.get("reason_code"))}
            for row in list(canonical or [])[-6:] if isinstance(row, Mapping)
        ],
        "pending_action_ids": held,
        "admitted_action_ids": acts if phase == "ADMITTED" else [],
        "recovery_events": replay_events,
        "actual_execution_status": "NOT_EXECUTED_BY_THIS_CONTRACT",
        "evidence_count_before_admission": max(0, int(evidence_count)),
        "planner_decision_authority": True,
        "full_admission_required": True,
        "authorization_granted": False,
        "budget_reset": False,
        "result_evidence_created": False,
    }


def render_transaction(record: Mapping) -> str:
    """Include precise bounded schema diffs in the immediate Planner repair."""
    return (
        "[SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1]\n"
        + json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        + "\n[/SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1]\n"
        "This receipt is diagnostic, never Action Evidence or permission. "
        "Safe-equivalent rewrites must pass every ordinary admission gate. "
        "Fix NEEDS_INFORMATION in its owning contract; only Planner may "
        "choose REPLAN. Never guess paths, evidence, authorization, actions, "
        "or terminal success. A pending action remains unexecuted; restore "
        "only an identical never-dispatched action after full admission."
    )


__all__ = [
    "SCHEMA", "diagnostic_receipts", "build_transaction", "render_transaction",
]
