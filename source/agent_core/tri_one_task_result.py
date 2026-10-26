"""Tri-One Phase 1 / 2A: trusted, opt-in completion metadata.

This contract records already-admitted Runtime state, and neither creates a
terminal receipt nor grants execution/transport authorization.  During 2A the
wire-format and legacy queue/event payloads remain authoritative; this record
is a versioned *additional* audit field, behind a default-OFF feature flag.

In 5A the answer-only admission lane will supply its own separately verified
completion kind; an empty Action Ledger is NEVER sufficient to assert that
an answer-only terminal was legally admitted.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

SCHEMA = "TRI_ONE_TASK_RESULT_V1"
FEATURE_FLAG = "TRI_ONE_TASK_RESULT_V1"
STATES = frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "PAUSED"})
OUTCOMES = frozenset({"SUCCESS", "FAILED", "PARTIAL", "UNKNOWN"})
KINDS = frozenset({"EVIDENCE_BACKED", "ANSWER_ONLY", "LEGACY_UNVERIFIED"})
SOURCES = frozenset({
    "TELEGRAM", "LOCAL_TEST", "WEB_COPILOT", "WEB_DIRECT", "LOCAL_AGENT",
})
DELIVERY_STATES = frozenset({"PENDING", "DELIVERED", "FAILED", "UNKNOWN"})


class TaskResultContractError(ValueError):
    """Malformed, foreign or overstated completion metadata."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Only an explicit administrator-controlled process flag opts in."""
    values = os.environ if env is None else env
    return str(values.get(FEATURE_FLAG, "")).strip().lower() in {"1", "true", "yes"}


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def make_task_result(
    *,
    request_id: str,
    task_id: str,
    task_epoch: str,
    intent_digest: str,
    source_interface: str,
    trusted_reply_route: Mapping[str, object] | None,
    state: str,
    outcome: str,
    terminal_acceptance: Mapping[str, object] | None,
    answer_text: str,
    evidence_refs: list[str] | tuple[str, ...] | None = None,
    operational_action_count: int = 0,
    artifacts: list[dict] | None = None,
    source_commit: str = "NOT_ATTESTED",
    completion_kind: str | None = None,
) -> dict[str, Any]:
    """Map *actual trusted Runtime* result data into read-only v1 metadata.

    Caller, not Planner, owns all identity, terminal and route parameters.
    PENDING means the transport has not supplied an objective delivery receipt.
    Delivery updates remain the responsibility of the existing outbox.
    """
    ident = {
        "request_id": str(request_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "task_epoch": str(task_epoch or "").strip(),
        "intent_digest": str(intent_digest or "").strip(),
    }
    if not all(ident.values()):
        raise TaskResultContractError("missing_task_identity")
    state = str(state or "").upper()
    outcome = str(outcome or "").upper()
    source_interface = str(source_interface or "").upper()
    if state not in STATES or outcome not in OUTCOMES:
        raise TaskResultContractError("invalid_state_or_outcome")
    if source_interface not in SOURCES:
        raise TaskResultContractError("unknown_origin_interface")
    if type(operational_action_count) is not int or operational_action_count < 0:
        raise TaskResultContractError("invalid_operational_action_count")
    if not isinstance(answer_text, str):
        raise TaskResultContractError("non_text_answer")
    if not isinstance(trusted_reply_route, Mapping):
        raise TaskResultContractError("missing_trusted_reply_route")
    route = dict(trusted_reply_route)
    if not route or str(route.get("transport") or "").upper() not in {
        source_interface, "WEBGPT", "WEBGPT_COPILOT",
    }:
        # LocalAgent/Direct may use a process-local route, but this must still
        # be explicit and trusted by the owning adapter.
        raise TaskResultContractError("route_origin_mismatch")
    refs = list(evidence_refs or [])
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise TaskResultContractError("invalid_evidence_ref")
    if len(set(refs)) != len(refs):
        raise TaskResultContractError("duplicate_evidence_ref")
    if not isinstance(artifacts, (list, type(None))) or any(
        not isinstance(row, dict) for row in (artifacts or [])
    ):
        raise TaskResultContractError("invalid_artifacts")

    receipt = dict(terminal_acceptance or {})
    accepted = (
        receipt.get("schema") == "SMARTAGENT_TERMINAL_ACCEPTANCE_V1"
        and receipt.get("admission_status") == "ACCEPTED"
        and bool(str(receipt.get("receipt_id") or ""))
    )
    if receipt:
        if not accepted:
            raise TaskResultContractError("unaccepted_terminal_receipt")
        if any(receipt.get(key) != value for key, value in (
            ("request_id", ident["request_id"]),
            ("task_id", ident["task_id"]),
            ("task_epoch", ident["task_epoch"]),
            ("task_outcome", outcome),
            ("runtime_state", state),
        )):
            raise TaskResultContractError("foreign_or_contradictory_receipt")
        receipt_refs = receipt.get("evidence_refs", [])
        if not isinstance(receipt_refs, list) or receipt_refs != refs:
            raise TaskResultContractError("receipt_evidence_mismatch")
    if state == "COMPLETED" and outcome == "SUCCESS" and not accepted:
        raise TaskResultContractError("success_without_terminal_receipt")
    if state != "COMPLETED" and outcome == "SUCCESS":
        raise TaskResultContractError("noncompleted_success")
    if state == "PAUSED" and accepted:
        raise TaskResultContractError("paused_with_terminal_receipt")

    # In 2A no new answer-only admission exists.  Without a future trusted
    # classifier, no-action completion is legacy/unverified by this schema.
    if completion_kind is None:
        kind = (
            "EVIDENCE_BACKED" if accepted and refs
            and receipt.get("evidence_verdict") == "PASS"
            else "LEGACY_UNVERIFIED"
        )
    else:
        kind = str(completion_kind).upper()
    if kind not in KINDS:
        raise TaskResultContractError("invalid_completion_kind")
    if kind == "EVIDENCE_BACKED" and (
        not accepted or not refs or receipt.get("evidence_verdict") != "PASS"
    ):
        raise TaskResultContractError("unverified_evidence_backed_claim")
    if kind == "ANSWER_ONLY":
        # 5A must introduce an explicit separately accepted reason.  Do not
        # infer authorization from absence of actions in 2A.
        raise TaskResultContractError("answer_only_not_implemented_in_2a")

    # No transport route identifiers or cookies leave this record.
    route_id = "ROUTE-" + _digest(route)[:20].upper()
    result_id = "TRES-" + _digest({
        **ident, "terminal_receipt_id": receipt.get("receipt_id") or "",
        "state": state,
    })[:24].upper()
    return {
        "schema": SCHEMA,
        **ident,
        "source_interface": source_interface,
        "reply_route_id": route_id,
        "source_commit": str(source_commit or "NOT_ATTESTED"),
        "completion_kind": kind,
        "task_state": state,
        "outcome": outcome,
        "terminal_acceptance": {
            "status": "ACCEPTED" if accepted else "MISSING",
            "receipt_id": receipt.get("receipt_id") if accepted else None,
            "evidence_verdict": receipt.get("evidence_verdict") if accepted else None,
        },
        "operational_action_count": operational_action_count,
        "evidence_refs": refs,
        "answer_text": answer_text,
        "artifacts": [dict(row) for row in (artifacts or [])],
        "delivery": {
            "status": "PENDING",
            "attempt_count": 0,
            "delivery_receipt_id": None,
        },
        "result_id": result_id,
    }


def validate_task_result(result: Mapping[str, object]) -> dict[str, object]:
    """Validate stored v1 metadata; no caller may use this to attest delivery."""
    if not isinstance(result, Mapping) or result.get("schema") != SCHEMA:
        raise TaskResultContractError("wrong_schema")
    if str(result.get("completion_kind")) == "ANSWER_ONLY":
        raise TaskResultContractError("answer_only_not_implemented_in_2a")
    delivery = result.get("delivery")
    if not isinstance(delivery, Mapping) or delivery.get("status") not in DELIVERY_STATES:
        raise TaskResultContractError("invalid_delivery_state")
    if delivery.get("status") == "DELIVERED":
        # DeliveryManager's objective receipt is required, not a model claim.
        if not delivery.get("delivery_receipt_id"):
            raise TaskResultContractError("delivery_receipt_missing")
    receipt = result.get("terminal_acceptance")
    if not isinstance(receipt, Mapping):
        raise TaskResultContractError("missing_terminal_acceptance")
    if result.get("outcome") == "SUCCESS" and (
        result.get("task_state") != "COMPLETED"
        or receipt.get("status") != "ACCEPTED"
        or not receipt.get("receipt_id")
    ):
        raise TaskResultContractError("unverified_success")
    if result.get("completion_kind") == "EVIDENCE_BACKED" and (
        receipt.get("evidence_verdict") != "PASS" or not result.get("evidence_refs")
    ):
        raise TaskResultContractError("missing_action_evidence")
    return dict(result)
