"""Request-scoped, Runtime-owned environment evidence, never a model-provided fact.

The Planner chooses which state supports its completion condition via an
explicit assertion. Runtime checks that assertion against a pinned PR-B fact.
No action, installation authorization or fabricated tool receipt is created.
"""
from __future__ import annotations
import hashlib
import json
from collections.abc import Mapping


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def build_fact_refs(*, request_id: str, task_id: str, task_epoch: str,
                    facts: list[dict]) -> dict[str, dict]:
    """Only confirmed Runtime probes are eligible for completion evidence."""
    result = {}
    for fact in facts[:80]:
        if not isinstance(fact, dict):
            continue
        if fact.get("kind") not in {"PACKAGE", "VERSION", "ENVIRONMENT"}:
            continue
        if fact.get("state") not in {"MISSING", "SATISFIED"}:
            continue
        if fact.get("source") not in {
            "BUNDLED_PYTHON_METADATA_PROBE",
            "PROJECT_ENVIRONMENT_REPROBED",
            "RUNTIME_VERSION_PROBE", "RUNTIME_PROBE",
        }:
            continue
        if fact.get("kind") == "PACKAGE" and fact.get("source") == "BUNDLED_PYTHON_METADATA_PROBE":
            if fact.get("probe_status") not in {"MISSING", "AVAILABLE"}:
                continue
        identity = {"request_id": request_id, "task_id": task_id,
                    "task_epoch": task_epoch, "fact": fact}
        ref = "ENVFACT-" + _digest(identity)[:24].upper()
        result[ref] = {
            "ref": ref, "request_id": request_id, "task_id": task_id,
            "task_epoch": task_epoch, "fact": dict(fact),
            "fact_digest": _digest(identity),
        }
    return result


def verify_fact_assertion(*, assertion: object, evidence_refs: list[str],
                          matched_condition_id: str, matched_condition: str = "",
                          request_id: str, task_id: str, task_epoch: str,
                          fact_refs: Mapping[str, dict]) -> tuple[str, str]:
    """Verify the Planner's explicit condition/state binding, not goal semantics.

    An absent binding is UNVERIFIED. A mismatch is rejected; it must never
    transform a false user assertion into a positive Runtime probe.
    """
    cited = [ref for ref in evidence_refs if ref in fact_refs or ref.startswith("ENVFACT-")]
    if not cited:
        return "NONE", "NO_ENVIRONMENT_FACT_REFERENCED"
    if len(cited) != 1 or len(evidence_refs) != 1:
        return "UNVERIFIED", "ENVIRONMENT_FACT_MIXED_EVIDENCE"
    if not isinstance(assertion, Mapping) or set(assertion) not in (
        {"evidence_ref", "expected_state", "condition_id"},
        {"evidence_ref", "expected_state", "condition_text"},
    ):
        return "UNVERIFIED", "ENVIRONMENT_FACT_ASSERTION_REQUIRED"
    ref = assertion.get("evidence_ref")
    state = assertion.get("expected_state")
    condition = assertion.get("condition_id")
    if ref != cited[0] or state not in {"MISSING", "SATISFIED"}:
        return "UNVERIFIED", "ENVIRONMENT_FACT_ASSERTION_INVALID"
    if not matched_condition_id or condition != matched_condition_id:
        return "UNVERIFIED", "ENVIRONMENT_FACT_CONDITION_MISMATCH"
    row = fact_refs.get(ref)
    if (not isinstance(row, Mapping)
            or any(row.get(field) != expected for field, expected in (
                ("request_id", request_id), ("task_id", task_id),
                ("task_epoch", task_epoch)))):
        return "UNVERIFIED", "ENVIRONMENT_FACT_IDENTITY_MISMATCH"
    if row["fact_digest"] != _digest({
        "request_id": request_id, "task_id": task_id,
        "task_epoch": task_epoch, "fact": row["fact"],
    }):
        return "UNVERIFIED", "ENVIRONMENT_FACT_INTEGRITY_MISMATCH"
    if row["fact"].get("state") != state:
        return "UNVERIFIED", "ENVIRONMENT_FACT_STATE_MISMATCH"
    return "PASS", "ENVIRONMENT_FACT_MATCHED"
