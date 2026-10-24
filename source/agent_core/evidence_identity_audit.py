"""Request-bound terminal evidence reference audit, before any privacy aliasing.

The audit does not decide success or rewrite historical evidence. The existing
Runtime terminal admission rules remain authoritative; raw IDs are private.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping


def audit_terminal_references(
    refs: Iterable[str], action_result_ledger: Mapping[str, Mapping],
    request_id: str, task_epoch: str,
) -> dict[str, object]:
    action_refs = [
        str(ref) for ref in refs
        if str(ref) not in {"REQUEST_ACCEPTED", "RUNTIME_STATUS"}
    ]
    unknown_refs = [ref for ref in action_refs if ref not in action_result_ledger]
    foreign_refs = [
        ref for ref in action_refs if ref in action_result_ledger
        and (
            action_result_ledger[ref].get("request_id", request_id) != request_id
            or action_result_ledger[ref].get("task_epoch", task_epoch) != task_epoch
        )
    ]
    return {
        "schema": "SMARTAGENT_TERMINAL_REFERENCE_AUDIT_V1",
        "action_ref_count": len(action_refs),
        "unknown_ref_count": len(unknown_refs),
        "foreign_ref_count": len(foreign_refs),
        "identity_status": "SUSPECT" if unknown_refs or foreign_refs else "SCOPED",
    }
