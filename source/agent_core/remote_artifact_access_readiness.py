"""Evidence-typed remote artifact access readiness for Goal-Driven planning.

An indexed GitHub reference is E1 discovery, never a network permission.
UNKNOWN triggers a bounded capability/admission probe; DENIED and
NEEDS_USER_APPROVAL require a *Runtime-owned* scoped policy receipt.
Git executable availability proves only the tool, not repository access.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

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
        and receipt.get("authority") in {
            "RUNTIME_SECURITY_PREFLIGHT", "RUNTIME_ADMITTED_GIT_READ_PROBE"
        }
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


def derive_admitted_git_read_receipt(
    artifact_view: Mapping | None, *,
    action_ledger: Mapping | None, result_ledger: Mapping | None,
    request_id: str, task_epoch: str,
) -> dict | None:
    """Correlate only ACTUAL accepted Git remote reads to the exact artifact.

    This is a projection of the canonical Runtime execution ledger, not
    model narrative or output metadata. It recognizes the single-purpose
    `git ls-remote https://github.com/owner/repo.git HEAD` probe; it cannot
    approve other network destinations, cloning, files or target writes.
    Commands other than this exact grammar do not create permission receipts.
    """
    if not request_id or not task_epoch:
        return None
    refs = [
        row for row in list((artifact_view or {}).get("artifact_references") or [])[:8]
        if isinstance(row, Mapping) and row.get("kind") == "REMOTE_ARTIFACT_REFERENCE"
    ]
    if not refs:
        return None
    actions = action_ledger if isinstance(action_ledger, Mapping) else {}
    results = result_ledger if isinstance(result_ledger, Mapping) else {}
    from .tool_result_contract import decode_result

    for action_id, recorded in reversed(list(actions.items())[-64:]):
        if not isinstance(recorded, Mapping):
            continue
        action = recorded.get("action")
        if not isinstance(action, Mapping):
            continue
        if action.get("tool") != "run_command" or action.get("operation") != "GIT_INSPECT":
            continue
        text = str(action.get("command") or "").strip()
        # Only direct HTTPS GitHub references. Never accept URLs with embedded
        # credentials, aliases, options, command chains, or shell expansions.
        match = re.fullmatch(
            r"git(?:\.exe)?\s+ls-remote\s+['\"]?"
            r"https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})(?:\.git)?"
            r"['\"]?\s+HEAD",
            text, re.IGNORECASE,
        )
        if match is None:
            continue
        repo = match.group(1)
        # The optional URL transport suffix is not part of GitHub's
        # canonical owner/repository identity.
        if repo.casefold().endswith(".git"):
            repo = repo[:-4]
        linked = next((r for r in refs if str(r.get("repository", "")).casefold() == repo.casefold()), None)
        if linked is None:
            continue
        evidence = results.get(action_id)
        if (not isinstance(evidence, Mapping)
                or evidence.get("request_id") != request_id
                or evidence.get("task_epoch") != task_epoch
                or evidence.get("evidence_status", "ACTIVE") != "ACTIVE"):
            continue
        tool_result = decode_result(str(recorded.get("result") or "")) or {}
        code = str(tool_result.get("reason") or "")
        status = tool_result.get("status")
        data = tool_result.get("data")
        if not isinstance(data, Mapping):
            data = {}
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "authority": "RUNTIME_ADMITTED_GIT_READ_PROBE",
            "request_id": request_id,
            "task_epoch": task_epoch,
            "candidate_id": linked["candidate_id"],
            "repository": linked["repository"],
            "evidence_ref": str(action_id)[:160],
        }
        if (status == "SUCCEEDED" and tool_result.get("executed") is True
                and data.get("process_state") == "EXITED"
                and type(data.get("exit_code")) is int and data["exit_code"] == 0
                and evidence.get("execution_status") == "SUCCEEDED"):
            transcript = str(recorded.get("result") or "")
            if re.search(r"(?m)^stdout:\r?\n[0-9a-fA-F]{40}\s+HEAD(?:\r?\n|$)", transcript):
                return {
                    **receipt, "decision": "AUTHORIZED",
                    "network_authorized": True,
                    "repository_read_authorized": True,
                }
            # An empty ls-remote output is not proof of repository access.
            return None
        if status == "REJECTED" and tool_result.get("executed") is False:
            if code in {"COMMAND_SCOPE_REJECTED", "WORKING_DIRECTORY_REJECTED",
                        "TOOL_SCOPE_REJECTED"}:
                return {**receipt, "decision": "DENIED",
                        "policy_denial_confirmed": True,
                        "denial_reason_code": code}
            if code == "SECURITY_CONFIRMATION_REQUIRED":
                return {**receipt, "decision": "NEEDS_USER_APPROVAL",
                        "approval_required_confirmed": True}
        # A failed network command, unresolved credential error, or ambiguous
        # result is UNKNOWN: never manufacture a DENIED/APPROVAL classification.
        return None
    return None


__all__ = ["SCHEMA", "RECEIPT_SCHEMA", "STATES", "resolve_remote_artifact_access", "derive_admitted_git_read_receipt"]
