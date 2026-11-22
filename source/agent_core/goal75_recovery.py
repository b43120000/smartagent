"""Git75 staged recovery, deterministic schema repair and terminal blocker proof.

All decisions are advisory to existing Tool/Security admission. In particular,
retry exhaustion and a Planner BLOCKED statement do not prove Goal infeasibility.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from .goal75_blackboard import digest

SCHEMA = "SMARTAGENT_GOAL75_RECOVERY_DECISION_V1"
OBJECTIVE_TERMINAL_REASONS = frozenset({
    "SCOPE_ACCESS_DENIED", "PERMISSION_DENIED", "EXPLICIT_PATH_INACCESSIBLE",
    "REQUIRED_SOURCE_INACCESSIBLE", "USER_FORBIDDEN_OPERATION",
    "REQUIRED_WEBSITE_UNAVAILABLE", "USER_APPROVAL_DENIED",
})
UNCERTAIN_EFFECT_REASONS = frozenset({
    "TOOL_EXCEPTION_OUTCOME_UNKNOWN", "PROCESS_LIVENESS_UNCONFIRMED",
    "POSSIBLE_PARTIAL_EXECUTION", "EXECUTION_OUTCOME_UNKNOWN",
    "RUNTIME_EXECUTION_NOT_CONFIRMED",
})
SCHEMA_REASONS = frozenset({
    "decision_empty", "json_syntax_invalid", "json_invalid_escape",
    "decision_fields_invalid", "decision_required_field_missing",
    "finish_contract_version_invalid", "tool_call_fields_invalid",
    "finish_v2_legacy_claims_forbidden",
})


def _reason(value: object) -> str:
    text = str(value or "").strip().upper()
    return text[:120] if text and all(c.isalnum() or c in "_.:-" for c in text) else "UNKNOWN"


def repair_single_decision(response: str) -> tuple[str, dict] | None:
    """Only lossless, unambiguous, single-JSON-object canonicalization.

    No command rewriting, path repairs, permission expansion, claim removal,
    evidence invention, multiple-object extraction or guessed values.
    """
    source = str(response or "").strip()
    if not source.startswith("{") or not source.endswith("}") or len(source) > 32768:
        return None

    def unique(pairs):
        row = {}
        for key, value in pairs:
            if key in row:
                raise ValueError("duplicate_key")
            row[key] = value
        return row

    try:
        data = json.loads(source, object_pairs_hook=unique)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    original = digest(data)
    revised = dict(data)
    changed = []
    if revised.get("type") == "tool_call" and "args" in revised:
        if "arguments" in revised or set(revised) - {"type", "name", "tool", "args"}:
            return None
        revised["arguments"] = revised.pop("args")
        changed.append("args_to_arguments")
    if revised.get("type") == "finish":
        if "content" in revised and not any(
            key in revised for key in ("summary", "answer", "text")
        ):
            if set(revised) - {
                "type", "content", "contract_version", "goal_checks", "expected_runtime"
            }:
                return None
            revised["summary"] = revised.pop("content")
            changed.append("content_to_summary")
        if revised.get("contract_version") == "2":
            revised["contract_version"] = 2
            changed.append("version_numeric_string")
    if not changed:
        return None
    if any(k in revised for k in ("command", "path", "working_directory", "password", "token")):
        return None
    return json.dumps(revised, ensure_ascii=False, separators=(",", ":")), {
        "schema": SCHEMA,
        "repair_kind": "DETERMINISTIC_SCHEMA",
        "changes": changed,
        "original_sha256": original,
        "canonical_sha256": digest(revised),
        "authorization_granted": False,
        "evidence_created": False,
    }


def verify_terminal_blocker(
    reason_code: str, *, evidence: Mapping | None = None,
    authorized_alternatives: int = 0,
    uncertain_mutation: bool = False,
) -> dict:
    """Require a Runtime-owned objective denial before terminal-blocked.

    A model statement, a failed command, a budget, or a missing schema field
    is not objective Goal infeasibility. An uncertain effect requires
    read-only reconciliation and may not be replayed.
    """
    reason = _reason(reason_code)
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if uncertain_mutation or reason in UNCERTAIN_EFFECT_REASONS:
        status = "PAUSED_UNRESOLVED"
        route = "RECONCILE_WITHOUT_REPLAY"
    elif (reason in OBJECTIVE_TERMINAL_REASONS
          and evidence.get("runtime_verified") is True
          and evidence.get("scope_bound") is True
          and evidence.get("goal_required") is True
          and evidence.get("alternative_exhausted") is True
          and authorized_alternatives == 0):
        status = "TERMINAL_BLOCKED_VERIFIED"
        route = "NO_AUTHORIZED_GOAL_ROUTE"
    else:
        status = "RECOVERING"
        route = (
            "DETERMINISTIC_SCHEMA_REPAIR" if reason.lower() in SCHEMA_REASONS
            or reason in {x.upper() for x in SCHEMA_REASONS}
            else "STAGE_LOCAL_STRATEGY_REPLAN"
        )
    return {
        "schema": SCHEMA, "status": status, "route": route,
        "reason_code": reason, "terminal_authorized": status == "TERMINAL_BLOCKED_VERIFIED",
        "mutation_replay_authorized": False,
        "scope_expansion_authorized": False,
        "evidence_verified": evidence.get("runtime_verified") is True,
    }


def choose_stage_recovery(
    *, reason_code: str, stage_id: str,
    attempt: int, evidence: Mapping | None = None,
    uncertain_mutation: bool = False,
) -> dict:
    """Select a *new strategy class*, not just retry the same Action."""
    decision = verify_terminal_blocker(
        reason_code, evidence=evidence, uncertain_mutation=uncertain_mutation,
    )
    if decision["status"] == "RECOVERING":
        if decision["route"] == "DETERMINISTIC_SCHEMA_REPAIR":
            next_strategy = "SCHEMA_CANONICALIZE"
        elif attempt <= 1:
            next_strategy = "REOBSERVE_OR_FIX_PREREQUISITE"
        else:
            next_strategy = "ALTERNATIVE_TOOL_OR_STAGE_REPLAN"
    else:
        next_strategy = decision["route"]
    return {
        **decision,
        "stage_id": str(stage_id)[:64],
        "strategy_id": next_strategy,
        "attempt": int(attempt),
        "reset_committed_stages": False,
        "incremental_goal_progress_required": True,
    }


def goal_finish_readiness(
    stage_state: Mapping | None, *, independent_goal_verified: bool,
    unsafe_effects_pending: bool = False,
) -> dict:
    """Never infer Goal PASS solely from Planner progress or Stage count."""
    record = dict(stage_state or {})
    stages = list(record.get("stages") or [])
    all_committed = bool(stages) and all(
        isinstance(s, Mapping)
        and s.get("status") == "VERIFIED_COMMITTED"
        and isinstance(s.get("commit"), Mapping)
        and s["commit"].get("commit_sha256")
        for s in stages
    )
    approved = all_committed and independent_goal_verified and not unsafe_effects_pending
    return {
        "schema": "SMARTAGENT_GOAL75_FINISH_READINESS_V1",
        "status": "PASS" if approved else "NOT_READY",
        "stage_commits_verified": all_committed,
        "independent_goal_verified": bool(independent_goal_verified),
        "unsafe_effects_pending": bool(unsafe_effects_pending),
        "goal_success_accepted": approved,
    }
