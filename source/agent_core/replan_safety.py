"""Bounded, evidence-gated Planner revisions; no new execution authority.

This module consumes ONLY Runtime-owned route catalogs and trusted Action
receipts. Do not accept model-authored receipt dictionaries or route catalogs.
It never executes tools, changes scope, claims terminal goal SUCCESS, or resets
the existing bounded-repair/GAR budgets.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

SCHEMA = "SMARTAGENT_EVIDENCE_GATED_REPLAN_V1"
_ROUTE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_RESULTS = frozenset({"FAILED", "BLOCKED", "REACHABLE", "UNKNOWN"})
_MAX_ROUTES = 24
_MAX_REVISIONS = 12


class ReplanSafetyError(ValueError):
    pass


def _routes(values: Sequence[str]) -> list[str]:
    if (not isinstance(values, (list, tuple)) or not values
            or len(values) > _MAX_ROUTES
            or any(type(item) is not str or not _ROUTE_ID.fullmatch(item)
                   for item in values)
            or len(set(values)) != len(values)):
        raise ReplanSafetyError("replan_route_catalog_invalid")
    return list(values)


def assess_recovery_routes(
    *, original_goal_digest: str, proposal_goal_digest: str,
    authorized_routes: Sequence[str], requested_routes: Sequence[str],
    runtime_receipts: Mapping[str, object],
    pending_effects: bool = False,
) -> dict:
    """Classify scoped access attempts, without inventing URL availability.

    The caller supplies an immutable original goal digest, finite authorized
    route IDs and a Runtime-owned receipt ledger keyed by action ID. A receipt
    must be independently verified and correlated with one authorized route.
    A blocked route is NOT a network attempt. REACHABLE is NOT goal SUCCESS.
    """
    if (not isinstance(original_goal_digest, str)
            or not _DIGEST.fullmatch(original_goal_digest)
            or proposal_goal_digest != original_goal_digest):
        raise ReplanSafetyError("replan_original_goal_changed")
    routes = _routes(authorized_routes)
    selected = _routes(requested_routes)
    if not set(selected).issubset(routes):
        raise ReplanSafetyError("replan_route_scope_expansion")
    if not isinstance(runtime_receipts, Mapping):
        raise ReplanSafetyError("replan_runtime_receipts_invalid")
    if pending_effects:
        return {
            "schema": SCHEMA, "decision": "PAUSE", "reason": "PENDING_EFFECTS",
            "remaining_routes": [], "attempts": [],
            "evidence_refs": [], "goal_satisfied": False,
        }
    seen: dict[str, dict] = {}
    unverified = []
    for action_id, item in runtime_receipts.items():
        if not isinstance(action_id, str) or not action_id or not isinstance(item, Mapping):
            raise ReplanSafetyError("replan_runtime_receipt_shape_invalid")
        route = item.get("route_id")
        if route not in routes:
            raise ReplanSafetyError("replan_receipt_route_untrusted")
        result = str(item.get("result", "") or "").upper()
        if result not in _ALLOWED_RESULTS:
            raise ReplanSafetyError("replan_receipt_result_invalid")
        # Executed/verified facts come from Runtime's receipt ledger, not
        # narrative Progress or a model's claimed network status.
        verified = item.get("verified") is True
        receipt_ref = str(item.get("evidence_ref", "") or "")
        if not verified or not receipt_ref:
            unverified.append(route)
            continue
        executed = item.get("executed")
        if (result in {"FAILED", "REACHABLE"} and executed is not True) or (
            result == "BLOCKED" and executed is not False
        ):
            unverified.append(route)
            continue
        if result == "UNKNOWN":
            unverified.append(route)
            continue
        if route in seen:
            # Multiple independent attempts of one strategy may disagree.
            # Never erase older results or choose a convenient terminal fact.
            prev = seen[route]
            if prev["result"] != result:
                unverified.append(route)
            prev["action_ids"].append(action_id)
            prev["evidence_refs"].append(receipt_ref)
            continue
        seen[route] = {
            "route_id": route, "result": result,
            "reason_code": (str(item.get("reason_code", "") or "UNKNOWN")
                            if _ROUTE_ID.fullmatch(str(item.get("reason_code", "") or "UNKNOWN"))
                            else "REDACTED_CODE"),
            "action_ids": [action_id], "evidence_refs": [receipt_ref],
            "executed": executed is True,
        }
    attempts = [seen[r] for r in routes if r in seen]
    evidence_refs = list(dict.fromkeys(
        ref for item in attempts for ref in item["evidence_refs"]
    ))
    if unverified:
        status, reason = "PAUSE", "UNVERIFIED_OR_CONFLICTING_ROUTE_RESULT"
    elif any(item["result"] == "REACHABLE" for item in attempts):
        status, reason = "CONTINUE", "ACCESS_ROUTE_REACHABLE_GOAL_NOT_VERIFIED"
    else:
        remaining = [r for r in routes if r not in seen]
        if remaining:
            status, reason = "REPLAN", "AUTHORIZED_UNTRIED_ROUTE_AVAILABLE"
        elif attempts and all(x["result"] in {"FAILED", "BLOCKED"} for x in attempts):
            status = "EXHAUSTED"
            reason = ("AUTHORIZED_ROUTES_BLOCKED_NO_NETWORK_ATTEMPT"
                      if all(x["result"] == "BLOCKED" for x in attempts)
                      else "AUTHORIZED_ROUTES_EXHAUSTED")
        else:
            status, reason = "PAUSE", "NO_VERIFIED_ROUTE_EVIDENCE"
    return {
        "schema": SCHEMA, "decision": status, "reason": reason,
        "remaining_routes": ([r for r in routes if r not in seen]
                             if status == "REPLAN" else []),
        "attempts": attempts, "evidence_refs": evidence_refs,
        "goal_satisfied": False,
    }


def admit_strategy_revision(
    *, original_goal_digest: str, proposal_goal_digest: str,
    authorized_routes: Sequence[str], requested_route: str,
    runtime_receipts: Mapping[str, object], previous_revision: int,
    proposed_revision: int, prior_revision_routes: Sequence[str] = (),
    previous_evidence_refs: Sequence[str] = (),
    pending_effects: bool = False, max_revisions: int = _MAX_REVISIONS,
) -> dict:
    """A revision cannot reset counters, expand scope or replay tried routes.

    A new Action ID, modified prose or inflated Progress step count is never
    new evidence. Retries need an independent, bounded route slot in the
    original Runtime-authorized catalog. Existing GAR budgets remain binding.
    """
    if (type(previous_revision) is not int or type(proposed_revision) is not int
            or previous_revision < 1 or proposed_revision != previous_revision + 1):
        raise ReplanSafetyError("replan_revision_sequence_invalid")
    if (type(max_revisions) is not int or not 1 <= max_revisions <= _MAX_REVISIONS
            or proposed_revision > max_revisions):
        return {"decision": "PAUSE", "reason": "REPLAN_BUDGET_EXHAUSTED"}
    catalog = _routes(authorized_routes)
    if requested_route not in catalog:
        raise ReplanSafetyError("replan_route_scope_expansion")
    if set(prior_revision_routes).difference(catalog):
        raise ReplanSafetyError("replan_historical_scope_invalid")
    result = assess_recovery_routes(
        original_goal_digest=original_goal_digest,
        proposal_goal_digest=proposal_goal_digest,
        authorized_routes=catalog, requested_routes=[requested_route],
        runtime_receipts=runtime_receipts, pending_effects=pending_effects,
    )
    if result["decision"] in {"PAUSE", "EXHAUSTED"}:
        return {"decision": result["decision"], "reason": result["reason"]}
    used = {row["route_id"] for row in result["attempts"]}
    if requested_route in used:
        return {"decision": "PAUSE", "reason": "REPEATED_STRATEGY_NO_NEW_EVIDENCE"}
    if requested_route in prior_revision_routes:
        return {"decision": "PAUSE", "reason": "REPEATED_REVISION_ROUTE"}
    # Prior evidence is diagnostic; it never authorizes an extra route or
    # overrides the immutable source/provenance contract.
    if not isinstance(previous_evidence_refs, (tuple, list)):
        raise ReplanSafetyError("replan_evidence_history_invalid")
    return {
        "decision": "REPLAN", "reason": "AUTHORIZED_NEW_STRATEGY",
        "revision": proposed_revision, "route_id": requested_route,
        "goal_satisfied": False,
    }
