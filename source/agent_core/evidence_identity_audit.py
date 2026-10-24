"""Request-bound terminal evidence provenance audit, before privacy aliasing.

Audit is observational; it does not synthesize identities, grant admission
or rewrite historical receipts. Incomplete identity is not SCOPED.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def audit_terminal_references(
    refs: Iterable[str],
    action_result_ledger: Mapping[str, Mapping],
    request_id: str,
    task_epoch: str,
) -> dict[str, object]:
    action_refs = [
        str(ref) for ref in refs
        if str(ref) not in {"REQUEST_ACCEPTED", "RUNTIME_STATUS"}
    ]
    unknown = [ref for ref in action_refs if ref not in action_result_ledger]
    foreign = []
    incomplete = []
    for ref in action_refs:
        if ref not in action_result_ledger:
            continue
        entry = action_result_ledger[ref]
        if not isinstance(entry, Mapping):
            incomplete.append(ref)
            continue
        owner_id = entry.get("request_id")
        owner_epoch = entry.get("task_epoch")
        if not isinstance(owner_id, str) or not owner_id or not isinstance(owner_epoch, str) or not owner_epoch:
            incomplete.append(ref)
        elif owner_id != request_id or owner_epoch != task_epoch:
            foreign.append(ref)
    status = (
        "FOREIGN" if foreign else "UNKNOWN_REF" if unknown
        else "INCOMPLETE" if incomplete else "SCOPED"
    )
    return {
        "schema": "SMARTAGENT_TERMINAL_REFERENCE_AUDIT_V2",
        "action_ref_count": len(action_refs),
        "unknown_ref_count": len(unknown),
        "foreign_ref_count": len(foreign),
        "incomplete_ref_count": len(incomplete),
        # Preserve v1 consumers of SCOPED/SUSPECT; expose the specific
        # provenance problem separately without silently treating it as valid.
        "identity_status": "SCOPED" if status == "SCOPED" else "SUSPECT",
        "identity_reason": status,
    }
