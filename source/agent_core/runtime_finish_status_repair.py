"""Bounded, non-authorizing Finish repair for incorrect Runtime status claims.

The Runtime never edits a model Finish, changes a ToolResult, or upgrades FAIL
to PASS. We permit a separate *non-executing* repair turn only when every
comparator mismatch is an actual typed status mismatch (e.g. REJECTED != FAILED).
"""
from __future__ import annotations

from collections.abc import Mapping

_SCHEMA = "SMARTAGENT_FINISH_STATUS_REPAIR_V1"
_STATUSES = frozenset({
    "SUCCEEDED", "FAILED", "REJECTED", "UNKNOWN", "RUNNING", "PARTIAL",
})
_VERIFICATION = frozenset({"PASS", "FAIL", "UNVERIFIED", "UNKNOWN", "SPEC_INVALID"})


def diagnose_finish_status_mismatch(comparison: Mapping) -> dict | None:
    if (not isinstance(comparison, Mapping)
            or comparison.get("reason_code") != "MODEL_RUNTIME_MISMATCH"):
        return None
    rows = comparison.get("mismatches")
    if not isinstance(rows, list) or not (1 <= len(rows) <= 32):
        return None
    corrections = []
    seen = set()
    for item in rows:
        if not isinstance(item, Mapping) or item.get("field") not in {
                "status", "data.verification_status"}:
            return None
        ref = item.get("evidence_ref")
        actual = item.get("actual")
        expected = item.get("expected")
        allowed = _STATUSES if item["field"] == "status" else _VERIFICATION
        if (not isinstance(ref, str) or not ref.startswith("MIN-EV-")
                or len(ref) > 90 or (ref, item["field"]) in seen
                or actual not in allowed or expected not in allowed
                or actual == expected):
            return None
        seen.add((ref, item["field"]))
        corrections.append({
            "evidence_ref": ref,
            "field": item["field"],
            "model_claimed_status": expected,
            "runtime_actual_status": actual,
            "missing_postrun_evidence": (
                item["field"] == "data.verification_status"
                and actual == "UNVERIFIED"
            ),
        })
    return {
        "schema": _SCHEMA,
        "reason_code": "MODEL_CLAIMED_STATUS_DIFFERS_FROM_RUNTIME",
        "repair_class": "FINISH_STATUS_REPAIR",
        "mismatch_count": len(corrections),
        "corrections": corrections,
        "repair_only": True,
        "evidence_unmodified": True,
        "authorization_granted": False,
    }


def finish_runtime_status_catalog(loop, *, max_records: int = 64) -> list[dict]:
    """All recorded ToolResult statuses, not merely last six model-visible rows."""
    from .tool_result_contract import decode_result
    ledger = getattr(loop, "action_result_ledger", {}) or {}
    if not isinstance(ledger, Mapping):
        return []
    rows = []
    for row in sorted(
        (v for v in ledger.values() if isinstance(v, Mapping)),
        key=lambda v: int(v.get("produced_round") or 0),
    )[:max_records]:
        evidence_ref = row.get("evidence_ref")
        receipt = decode_result(row.get("result") or "")
        if (not isinstance(evidence_ref, str)
                or not evidence_ref.startswith("MIN-EV-")
                or not isinstance(receipt, Mapping)
                or receipt.get("status") not in _STATUSES):
            continue
        rows.append({
            "evidence_ref": evidence_ref,
            "tool": str(row.get("tool") or "")[:48],
            "status": receipt["status"],
            "verification_status": (receipt.get("data") or {}).get(
                "verification_status", "NOT_RECORDED"),
            "executed": receipt.get("executed") is True,
            "reason_code": str(receipt.get("reason") or "")[:100],
        })
    return rows


__all__ = [
    "diagnose_finish_status_mismatch", "finish_runtime_status_catalog",
]
