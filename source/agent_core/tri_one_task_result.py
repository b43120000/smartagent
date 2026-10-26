"""Tri-One Phase 1 / 2A: trusted, opt-in completion metadata.

This record describes an already-admitted Runtime terminal. It cannot create a
terminal receipt, grant execution authority, or prove delivery. It is additive
metadata behind a default-OFF feature flag; legacy task/event fields remain
authoritative throughout 2A.

ANSWER_ONLY is deliberately rejected until 5A introduces and verifies its
separate answer-only terminal admission path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
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
ACTION_COUNT_BASES = frozenset({
    "CALLER_ATTESTED", "RESULT_LEDGER", "TASK_LEDGER", "NO_LEDGER_OBSERVED",
})
_HEX_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")


class TaskResultContractError(ValueError):
    """Malformed, foreign, or overstated completion metadata."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Only an explicit process-level administrator flag opts in."""
    values = os.environ if env is None else env
    return str(values.get(FEATURE_FLAG, "")).strip().lower() in {"1", "true", "yes"}


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _utc_timestamp(value: object = None) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        stamp = time.time()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _normalize_source_interface(value: object) -> str:
    token = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "WEBGPT_COPILOT": "WEB_COPILOT",
        "WEBCOPILOT": "WEB_COPILOT",
        "WEB_COPILOT": "WEB_COPILOT",
        "WEBGPT": "WEB_DIRECT",
        "WEB_DIRECT": "WEB_DIRECT",
        "LOCALAGENT": "LOCAL_AGENT",
        "LOCAL_AGENT": "LOCAL_AGENT",
        "TELEGRAM": "TELEGRAM",
        "LOCAL_TEST": "LOCAL_TEST",
    }
    return aliases.get(token, token)


def _route_is_owned_by_source(route: Mapping[str, object], source_interface: str) -> bool:
    transport = str(route.get("transport") or "").strip().upper().replace("-", "_")
    allowed = {
        "TELEGRAM": {"TELEGRAM"},
        "LOCAL_TEST": {"LOCAL_TEST"},
        "WEB_COPILOT": {"WEBGPT_COPILOT", "WEB_COPILOT", "WEBCOPILOT"},
        "WEB_DIRECT": {"WEB_DIRECT", "WEBGPT", "WEB_DIRECT_AGENT"},
        "LOCAL_AGENT": {"LOCAL_AGENT", "LOCALAGENT"},
    }
    return bool(transport and transport in allowed.get(source_interface, set()))


def _result_id(identity: Mapping[str, str], receipt_id: str, state: str,
               outcome: str, completion_kind: str) -> str:
    return "TRES-" + _digest({
        **dict(identity),
        "terminal_receipt_id": receipt_id,
        "state": state,
        "outcome": outcome,
        "completion_kind": completion_kind,
    })[:24].upper()


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
    operational_action_count_basis: str = "CALLER_ATTESTED",
    artifacts: list[dict] | None = None,
    source_commit: str = "NOT_ATTESTED",
    completion_kind: str | None = None,
    created_at: object = None,
) -> dict[str, Any]:
    """Map trusted Runtime-owned fields into a versioned, non-authorizing record.

    A caller must pass the actual accepted terminal receipt, if one exists.
    The result never infers loaded source identity from a Git HEAD. Unless a
    separately trusted attestation is supplied, source_commit remains
    NOT_ATTESTED. Route details are never copied into the result.
    """
    identity = {
        "request_id": str(request_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "task_epoch": str(task_epoch or "").strip(),
        "intent_digest": str(intent_digest or "").strip(),
    }
    if not all(identity.values()):
        raise TaskResultContractError("missing_task_identity")

    state = str(state or "").strip().upper()
    outcome = str(outcome or "").strip().upper()
    source_interface = _normalize_source_interface(source_interface)
    if state not in STATES or outcome not in OUTCOMES:
        raise TaskResultContractError("invalid_state_or_outcome")
    if source_interface not in SOURCES:
        raise TaskResultContractError("unknown_origin_interface")
    if type(operational_action_count) is not int or operational_action_count < 0:
        raise TaskResultContractError("invalid_operational_action_count")
    action_count_basis = str(operational_action_count_basis or "").strip().upper()
    if action_count_basis not in ACTION_COUNT_BASES:
        raise TaskResultContractError("invalid_operational_action_count_basis")
    if not isinstance(answer_text, str):
        raise TaskResultContractError("non_text_answer")
    if not isinstance(trusted_reply_route, Mapping):
        raise TaskResultContractError("missing_trusted_reply_route")
    route = dict(trusted_reply_route)
    if not _route_is_owned_by_source(route, source_interface):
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

    source_commit = str(source_commit or "NOT_ATTESTED").strip()
    if source_commit != "NOT_ATTESTED" and not _HEX_SHA1.fullmatch(source_commit):
        raise TaskResultContractError("source_commit_not_attested_sha")

    receipt = dict(terminal_acceptance or {})
    accepted = (
        receipt.get("schema") == "SMARTAGENT_TERMINAL_ACCEPTANCE_V1"
        and receipt.get("admission_status") == "ACCEPTED"
        and bool(str(receipt.get("receipt_id") or "").strip())
    )
    if receipt:
        if not accepted:
            raise TaskResultContractError("unaccepted_terminal_receipt")
        for key, value in (
            ("request_id", identity["request_id"]),
            ("task_id", identity["task_id"]),
            ("task_epoch", identity["task_epoch"]),
            ("task_outcome", outcome),
            ("runtime_state", state),
        ):
            if receipt.get(key) != value:
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

    verdict = str(receipt.get("evidence_verdict") or "").strip().upper() if accepted else ""
    if completion_kind is None:
        kind = (
            "EVIDENCE_BACKED"
            if accepted and refs and verdict in {"PASS", "FAIL"}
            else "LEGACY_UNVERIFIED"
        )
    else:
        kind = str(completion_kind).strip().upper()
    if kind not in KINDS:
        raise TaskResultContractError("invalid_completion_kind")
    if kind == "ANSWER_ONLY":
        # Phase 2A cannot authorize the 5A answer-only terminal lane.
        raise TaskResultContractError("answer_only_not_implemented_in_2a")
    if kind == "EVIDENCE_BACKED" and (
        not accepted or not refs or verdict not in {"PASS", "FAIL"}
    ):
        raise TaskResultContractError("unverified_evidence_backed_claim")
    if outcome == "SUCCESS" and (
        kind != "EVIDENCE_BACKED" or verdict != "PASS" or not refs
    ):
        raise TaskResultContractError("success_without_passing_evidence")

    route_transport = str(route.get("transport") or "").strip().upper()
    route_id = "ROUTE-" + _digest({
        "task_id": identity["task_id"],
        "source_interface": source_interface,
        "transport": route_transport,
    })[:20].upper()
    result_id = _result_id(
        identity, str(receipt.get("receipt_id") or "") if accepted else "",
        state, outcome, kind,
    )
    result = {
        "schema": SCHEMA,
        **identity,
        "source_interface": source_interface,
        "reply_route_id": route_id,
        "source_commit": source_commit,
        "completion_kind": kind,
        "task_state": state,
        "outcome": outcome,
        "terminal_acceptance": {
            "status": "ACCEPTED" if accepted else "MISSING",
            "receipt_id": receipt.get("receipt_id") if accepted else None,
            "evidence_verdict": verdict or None,
        },
        "operational_action_count": operational_action_count,
        "operational_action_count_basis": action_count_basis,
        "evidence_refs": refs,
        "answer_text": answer_text,
        "artifacts": [dict(row) for row in (artifacts or [])],
        "delivery": {
            "status": "PENDING",
            "attempt_count": 0,
            "delivery_receipt_id": None,
        },
        "result_id": result_id,
        "created_at": _utc_timestamp(created_at),
    }
    return validate_task_result(result)


def validate_task_result(result: Mapping[str, object]) -> dict[str, object]:
    """Validate a stored record; this does not itself attest terminal or delivery."""
    if not isinstance(result, Mapping) or result.get("schema") != SCHEMA:
        raise TaskResultContractError("wrong_schema")
    identity = {
        key: str(result.get(key) or "").strip()
        for key in ("request_id", "task_id", "task_epoch", "intent_digest")
    }
    if not all(identity.values()):
        raise TaskResultContractError("missing_task_identity")
    source_interface = _normalize_source_interface(result.get("source_interface"))
    if source_interface not in SOURCES:
        raise TaskResultContractError("unknown_origin_interface")
    state = str(result.get("task_state") or "").strip().upper()
    outcome = str(result.get("outcome") or "").strip().upper()
    kind = str(result.get("completion_kind") or "").strip().upper()
    if state not in STATES or outcome not in OUTCOMES or kind not in KINDS:
        raise TaskResultContractError("invalid_state_outcome_or_kind")
    if kind == "ANSWER_ONLY":
        raise TaskResultContractError("answer_only_not_implemented_in_2a")
    count = result.get("operational_action_count")
    if type(count) is not int or count < 0:
        raise TaskResultContractError("invalid_operational_action_count")
    if str(result.get("operational_action_count_basis") or "").upper() not in ACTION_COUNT_BASES:
        raise TaskResultContractError("invalid_operational_action_count_basis")
    if not isinstance(result.get("answer_text"), str):
        raise TaskResultContractError("non_text_answer")
    refs = result.get("evidence_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise TaskResultContractError("invalid_evidence_ref")
    if len(set(refs)) != len(refs):
        raise TaskResultContractError("duplicate_evidence_ref")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or any(not isinstance(row, Mapping) for row in artifacts):
        raise TaskResultContractError("invalid_artifacts")
    source_commit = str(result.get("source_commit") or "")
    if source_commit != "NOT_ATTESTED" and not _HEX_SHA1.fullmatch(source_commit):
        raise TaskResultContractError("source_commit_not_attested_sha")
    if not str(result.get("reply_route_id") or "").startswith("ROUTE-"):
        raise TaskResultContractError("invalid_reply_route_id")
    if not str(result.get("result_id") or "").startswith("TRES-"):
        raise TaskResultContractError("invalid_result_id")
    if not str(result.get("created_at") or "").strip():
        raise TaskResultContractError("missing_created_at")

    terminal = result.get("terminal_acceptance")
    if not isinstance(terminal, Mapping) or terminal.get("status") not in {"ACCEPTED", "MISSING"}:
        raise TaskResultContractError("missing_terminal_acceptance")
    receipt_id = str(terminal.get("receipt_id") or "").strip()
    verdict = str(terminal.get("evidence_verdict") or "").strip().upper()
    if terminal.get("status") == "ACCEPTED":
        if not receipt_id or verdict not in {"PASS", "FAIL", "UNVERIFIED"}:
            raise TaskResultContractError("invalid_terminal_acceptance_summary")
    elif receipt_id or verdict:
        raise TaskResultContractError("missing_receipt_has_claims")

    delivery = result.get("delivery")
    if not isinstance(delivery, Mapping) or delivery.get("status") not in DELIVERY_STATES:
        raise TaskResultContractError("invalid_delivery_state")
    attempts = delivery.get("attempt_count")
    if type(attempts) is not int or attempts < 0:
        raise TaskResultContractError("invalid_delivery_attempt_count")
    delivery_receipt = str(delivery.get("delivery_receipt_id") or "").strip()
    if delivery.get("status") == "DELIVERED":
        if not delivery_receipt:
            raise TaskResultContractError("delivery_receipt_missing")
    elif delivery_receipt:
        raise TaskResultContractError("unconfirmed_delivery_receipt")

    if state == "PAUSED" and terminal.get("status") == "ACCEPTED":
        raise TaskResultContractError("paused_with_terminal_receipt")
    if state != "COMPLETED" and outcome == "SUCCESS":
        raise TaskResultContractError("noncompleted_success")
    if outcome == "SUCCESS" and (
        state != "COMPLETED" or terminal.get("status") != "ACCEPTED"
        or kind != "EVIDENCE_BACKED" or verdict != "PASS" or not refs
    ):
        raise TaskResultContractError("unverified_success")
    if kind == "EVIDENCE_BACKED" and (
        terminal.get("status") != "ACCEPTED" or not refs or verdict not in {"PASS", "FAIL"}
    ):
        raise TaskResultContractError("missing_action_evidence")
    expected_id = _result_id(
        identity, receipt_id if terminal.get("status") == "ACCEPTED" else "",
        state, outcome, kind,
    )
    if result.get("result_id") != expected_id:
        raise TaskResultContractError("result_id_mismatch")
    return dict(result)


def make_task_result_from_remote_event(task: object, event_type: str,
                                       payload: Mapping[str, object] | None) -> dict[str, Any]:
    """Create an additive TaskResult from an existing trusted terminal event."""
    event_type = str(event_type or "").strip().upper()
    state_by_event = {
        "TASK_COMPLETED": "COMPLETED",
        "TASK_FAILED": "FAILED",
        "TASK_INTERRUPTED": "INTERRUPTED",
        "TASK_PAUSED": "PAUSED",
    }
    state = state_by_event.get(event_type)
    if not state:
        raise TaskResultContractError("not_a_terminal_event")

    metadata = getattr(task, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    result_ledger = getattr(task, "result_ledger", {})
    result_ledger = result_ledger if isinstance(result_ledger, Mapping) else {}
    source_key = "paused" if state == "PAUSED" else ("interrupted" if state == "INTERRUPTED" else "final")
    final = result_ledger.get(source_key, {})
    final = final if isinstance(final, Mapping) else {}
    event_payload = payload if isinstance(payload, Mapping) else {}
    route = getattr(task, "reply_route", None)
    if not isinstance(route, Mapping) or not route:
        route = metadata.get("reply_route", {})
    if not isinstance(route, Mapping):
        raise TaskResultContractError("missing_trusted_reply_route")
    transport = route.get("transport") or metadata.get("transport") or ""
    source_interface = _normalize_source_interface(transport)
    # A PAUSED checkpoint is not a terminal completion. Disregard any
    # model-supplied SUCCESS outcome when publishing its status event.
    outcome = (
        "UNKNOWN" if state == "PAUSED" else str(
            final.get("task_outcome") or event_payload.get("task_outcome")
            or ("FAILED" if event_type == "TASK_FAILED" else "UNKNOWN")
        ).strip().upper()
    )
    if outcome not in OUTCOMES:
        outcome = "UNKNOWN"
    task_state = str(getattr(task, "state", "") or "").upper()
    if task_state and task_state != state and not (task_state == "CANCELLED" and state == "INTERRUPTED"):
        raise TaskResultContractError("event_task_state_mismatch")
    source_digest = str(final.get("intent_digest") or "").strip()
    meta_digest = str(metadata.get("intent_digest") or "").strip()
    if source_digest and meta_digest and source_digest != meta_digest:
        raise TaskResultContractError("intent_digest_mismatch")
    receipt = final.get("terminal_acceptance") or event_payload.get("terminal_acceptance") or {}
    if not isinstance(receipt, Mapping):
        raise TaskResultContractError("invalid_terminal_receipt")
    refs = receipt.get("evidence_refs", [])
    if not isinstance(refs, list):
        raise TaskResultContractError("invalid_evidence_ref")

    action_ledger = final.get("action_result_ledger")
    if isinstance(action_ledger, Mapping):
        action_count = len(action_ledger)
        action_basis = "RESULT_LEDGER"
    else:
        task_action_ledger = getattr(task, "action_ledger", {})
        if isinstance(task_action_ledger, Mapping):
            action_count = len(task_action_ledger)
            action_basis = "TASK_LEDGER"
        else:
            action_count = 0
            action_basis = "NO_LEDGER_OBSERVED"

    artifacts = final.get("outbound_artifacts")
    if not isinstance(artifacts, list):
        artifacts = event_payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise TaskResultContractError("invalid_artifacts")
    answer_text = event_payload.get("summary") or final.get("response") or final.get("error") or ""
    task_metadata = dict(metadata)
    attested_sha = str(task_metadata.get("loaded_source_commit") or "").strip()
    source_commit = (
        attested_sha
        if task_metadata.get("loaded_source_commit_attested") is True
        and _HEX_SHA1.fullmatch(attested_sha)
        else "NOT_ATTESTED"
    )
    completed_at = getattr(task, "completed_at", None)
    result = make_task_result(
        request_id=str(getattr(task, "request_id", "") or ""),
        task_id=str(getattr(task, "task_id", "") or ""),
        task_epoch=str(getattr(task, "task_epoch", "") or ""),
        intent_digest=str(final.get("intent_digest") or metadata.get("intent_digest") or ""),
        source_interface=source_interface,
        trusted_reply_route=route,
        state=state,
        outcome=outcome,
        terminal_acceptance=receipt,
        answer_text=str(answer_text),
        evidence_refs=refs,
        operational_action_count=action_count,
        operational_action_count_basis=action_basis,
        artifacts=artifacts,
        source_commit=source_commit,
        created_at=completed_at,
    )
    plan_id = str(final.get("plan_id") or event_payload.get("plan_id") or "").strip()
    source_plan_id = str(metadata.get("source_plan_id") or final.get("source_plan_id") or "").strip()
    if plan_id:
        result["plan_id"] = plan_id
    if source_plan_id:
        result["source_plan_id"] = source_plan_id
    return validate_task_result(result)


def make_task_result_from_webagent_loop(loop: object, answer_text: str, *,
                                        source_interface: str = "WEB_DIRECT") -> dict[str, Any]:
    """Create the same record for the standalone WebAgent controller path."""
    source_interface = _normalize_source_interface(source_interface)
    route = {"transport": source_interface}
    receipt = getattr(loop, "terminal_acceptance", {}) or {}
    if not isinstance(receipt, Mapping):
        raise TaskResultContractError("invalid_terminal_receipt")
    refs = receipt.get("evidence_refs", [])
    if not isinstance(refs, list):
        raise TaskResultContractError("invalid_evidence_ref")
    ledger = getattr(loop, "action_result_ledger", {})
    if isinstance(ledger, Mapping):
        action_count = len(ledger)
        action_basis = "RESULT_LEDGER"
    else:
        action_count = 0
        action_basis = "NO_LEDGER_OBSERVED"
    outcome = str(getattr(loop, "terminal_outcome", "") or "UNKNOWN").strip().upper()
    if outcome not in OUTCOMES:
        outcome = "UNKNOWN"
    return make_task_result(
        request_id=str(getattr(loop, "run_id", "") or ""),
        task_id=str(getattr(loop, "task_id", "") or ""),
        task_epoch=str(getattr(loop, "task_epoch", "") or ""),
        intent_digest=str(getattr(loop, "intent_digest", "") or ""),
        source_interface=source_interface,
        trusted_reply_route=route,
        state="COMPLETED",
        outcome=outcome,
        terminal_acceptance=receipt,
        answer_text=str(answer_text),
        evidence_refs=refs,
        operational_action_count=action_count,
        operational_action_count_basis=action_basis,
        artifacts=[],
        source_commit="NOT_ATTESTED",
        created_at=time.time(),
    )


__all__ = [
    "ACTION_COUNT_BASES", "DELIVERY_STATES", "FEATURE_FLAG", "KINDS", "OUTCOMES",
    "SCHEMA", "SOURCES", "STATES", "TaskResultContractError", "enabled",
    "make_task_result", "make_task_result_from_remote_event",
    "make_task_result_from_webagent_loop", "validate_task_result",
]
