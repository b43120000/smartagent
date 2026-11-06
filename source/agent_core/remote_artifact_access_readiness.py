"""Evidence-typed remote artifact access readiness for Goal-Driven planning.

An indexed GitHub reference is E1 discovery, never a network permission.
UNKNOWN triggers a bounded capability/admission probe; DENIED and
NEEDS_USER_APPROVAL require a *Runtime-owned* scoped policy receipt.
Git executable availability proves only the tool, not repository access.
"""
from __future__ import annotations

from collections.abc import Mapping

SCHEMA = "SMARTAGENT_REMOTE_ARTIFACT_ACCESS_READINESS_V1"
RECEIPT_SCHEMA = "SMARTAGENT_REMOTE_ACCESS_ADMISSION_RECEIPT_V1"
STATES = frozenset({"UNKNOWN", "AUTHORIZED", "DENIED", "NEEDS_USER_APPROVAL"})


def resolve_remote_artifact_access(
    artifact_view: Mapping | None, *,
    environment: Mapping | None = None,
    runtime_receipt: Mapping | None = None,
    request_id: str = "", task_epoch: str = "",
) -> dict:
    """Project verified preflight evidence, never issue Git/network actions.

    runtime_receipt is reserved for the Runtime's own security/admission
    pipeline; neither the model response nor an artifact manifest may supply it.
    No receipt is currently inferred from missing command attempts, CLI exit
    codes, source metadata, or read-only workspace paths.
    """
    artifact_view = artifact_view if isinstance(artifact_view, Mapping) else {}
    refs = [r for r in list(artifact_view.get("artifact_references") or [])[:8]
            if isinstance(r, Mapping) and r.get("kind") == "REMOTE_ARTIFACT_REFERENCE"]
    git = ((environment or {}).get("tools") or {}).get("git") or {}
    if not isinstance(git, Mapping):
        git = {}
    git_status = str(git.get("status") or "UNKNOWN")
    if git_status not in {"AVAILABLE", "MISSING", "INVALID"}:
        git_status = "UNKNOWN"
    access = "UNKNOWN"
    evidence = ""
    reason = "ACCESS_NOT_YET_EVALUATED"
    receipt = runtime_receipt if isinstance(runtime_receipt, Mapping) else {}
    matched = (
        bool(refs and request_id and task_epoch)
        and receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("authority") == "RUNTIME_SECURITY_PREFLIGHT"
        and receipt.get("request_id") == request_id
        and receipt.get("task_epoch") == task_epoch
        and receipt.get("candidate_id") == refs[0].get("candidate_id")
        and receipt.get("repository") == refs[0].get("repository")
        and isinstance(receipt.get("evidence_ref"), str)
        and bool(receipt.get("evidence_ref"))
    )
    if matched:
        decision = receipt.get("decision")
        if (decision == "AUTHORIZED"
                and receipt.get("network_authorized") is True
                and receipt.get("repository_read_authorized") is True):
            access = "AUTHORIZED"
            reason = "RUNTIME_VERIFIED_NETWORK_AND_REPOSITORY_READ"
        elif decision == "DENIED" and receipt.get("policy_denial_confirmed") is True:
            access = "DENIED"
            reason = "RUNTIME_CONFIRMED_POLICY_DENIAL"
        elif (decision == "NEEDS_USER_APPROVAL"
              and receipt.get("approval_required_confirmed") is True):
            access = "NEEDS_USER_APPROVAL"
            reason = "RUNTIME_CONFIRMED_ADDITIONAL_APPROVAL_REQUIRED"
        if access != "UNKNOWN":
            evidence = str(receipt["evidence_ref"])[:160]
    if access == "UNKNOWN":
        if not refs:
            next_check = "DISCOVER_EXACT_ARTIFACT_REFERENCE"
        elif git_status == "AVAILABLE":
            next_check = "CHECK_GIT_REPOSITORY_NETWORK_AND_SCOPE_ADMISSION"
        else:
            next_check = "VERIFY_LOCAL_GIT_CAPABILITY"
    elif access == "AUTHORIZED":
        next_check = "PROPOSE_PINNED_GIT_ACQUISITION_FOR_NORMAL_TOOL_ADMISSION"
    elif access == "DENIED":
        next_check = "REPORT_CONFIRMED_DENIAL"
    else:
        next_check = "REQUEST_PRECISE_MISSING_APPROVAL"
    return {
        "schema": SCHEMA,
        "authorization_state": access,
        "reason_code": reason,
        "authorization_evidence_ref": evidence,
        "repository_access_status": (
            "NOT_CHECKED" if access == "UNKNOWN" else access
        ),
        "registered_artifact_count": len(refs),
        "git_executable_status": git_status,
        "git_capability_known": git_status == "AVAILABLE",
        "git_tool_route": {"tool": "run_command", "operation": "GIT_INSPECT"},
        "next_verification": next_check,
        "tool_action_proposed": False,
        "network_request_attempted": False,
        "artifact_integrity_verified": False,
        "normal_tool_scope_and_security_admission_required": True,
        "authorization_inferred_from_git_installation": False,
        "authorization_inferred_from_artifact_reference": False,
    }


__all__ = ["SCHEMA", "RECEIPT_SCHEMA", "STATES", "resolve_remote_artifact_access"]
