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
    *,
    protocol_spec_rejection: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Audit action results and a separately verified protocol rejection.

    PROTOCOL_SPEC_REJECTION is a Runtime control-plane reference, not an
    executed action or observation. It is scoped only when the Runtime passes
    its validated, nonexecuted, same-request/epoch rejection record. Model-
    supplied names and unverified/foreign records are still UNKNOWN_REF.
    """
    control_ref = "PROTOCOL_SPEC_REJECTION"
    record = protocol_spec_rejection
    scoped_control = (
        isinstance(record, Mapping)
        and record.get("request_id") == request_id
        and record.get("task_epoch") == task_epoch
        and record.get("executed") is False
        and record.get("reason") == "evidence_file_exists_boolean_required"
        and record.get("verification_status") == "SPEC_INVALID"
        and isinstance(record.get("action_id"), str)
        and bool(record.get("action_id"))
    )
    action_refs = [
        str(ref) for ref in refs
        if str(ref) not in {"REQUEST_ACCEPTED", "RUNTIME_STATUS", control_ref}
    ]
    control_refs = [str(ref) for ref in refs if str(ref) == control_ref]
    unknown = [ref for ref in action_refs if ref not in action_result_ledger]
    if control_refs and not scoped_control:
        unknown.extend(control_refs)
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
        "protocol_ref_count": len(control_refs) if scoped_control else 0,
        "unknown_ref_count": len(unknown),
        "foreign_ref_count": len(foreign),
        "incomplete_ref_count": len(incomplete),
        # Preserve v1 consumers of SCOPED/SUSPECT; expose the specific
        # provenance problem separately without silently treating it as valid.
        "identity_status": "SCOPED" if status == "SCOPED" else "SUSPECT",
        "identity_reason": status,
    }
