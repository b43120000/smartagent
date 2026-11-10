"""Git46 Turbo OS-sandbox authority gate.

This module is deliberately *not* a software emulation of NTFS/AppContainer.
No host subprocess or ordinary restricted executor is a valid Turbo backend.
Only a future independently attested OS broker may report READY; until then
all explicitly requested Turbo work is blocked before any tool dispatch.

The requested roots remain in the existing workspace_access_policy.json.
The OS backend must implement a separate, authenticated attestation channel
before the READY branch can ever be enabled.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .windows_security import acl_mode
from .workspace_access import access_snapshot, is_valid_turbo_root

SCHEMA = "SMARTAGENT_TURBO_SANDBOX_STATE_V1"
BACKEND = "UNAVAILABLE_NO_OS_BROKER"


def resolve_turbo_state(workspace: str | Path | None = None) -> dict[str, Any]:
    """Read shared policy per action; never accept an env or JSON READY assertion.

    A signed lease, package SID, process-token identity, ACL and kernel-level
    negative access probes are not implemented in this version. An ordinary
    workspace-scoped executor cannot replace any of these checks.
    """
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "acl_mode": "unknown",
        "turbo_requested": "OFF",
        "sandbox_status": "NOT_READY",
        "sandbox_attestation_passed": False,
        "turbo_effective": "OFF",
        "sandbox_mode": "TURBO_OS_SCOPED",
        "sandbox_backend": BACKEND,
        "sandbox_identity": "",
        "sandbox_identity_verified": False,
        "turbo_root": "",
        "workspace_root": str(workspace or ""),
        "policy_revision": 0,
        "acl_attestation": "NOT_VERIFIED",
        "process_isolation_attestation": "NOT_VERIFIED",
        "child_process_policy": "NOT_VERIFIED",
        "scope_verdict": "TURBO_SCOPE_DENIED",
        "status": "SANDBOX_UNAVAILABLE",
        "reason_code": "TURBO_DISABLED",
        "authorization_granted": False,
    }
    try:
        access = access_snapshot()
        policy = access["policy"]
        state["policy_revision"] = int(policy.get("revision", 0) or 0)
        requested = policy.get("turbo_requested") is True
        state["turbo_requested"] = "ON" if requested else "OFF"
        mode = acl_mode()
        state["acl_mode"] = mode
        if not requested:
            state["reason_code"] = "TURBO_DISABLED"
            return state
        if mode != "on":
            state["reason_code"] = "TURBO_REQUIRES_ACL_ON"
            return state
        configured = list(access.get("turbo_roots") or ())
        if not configured or workspace is None:
            state["reason_code"] = "TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE"
            return state
        parent = Path(workspace).expanduser().resolve()
        allowed = [
            Path(root).expanduser().resolve()
            for root in configured
            if is_valid_turbo_root(
                root, parent, access["writable_workspaces"],
                access["read_only_roots"],
                policy.get("denied_write_roots") or (),
            )
        ]
        if not allowed:
            state["reason_code"] = "TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE"
            return state
        # A workspace may have several nominated roots. Selection of a single
        # OS-verified execution boundary belongs to the broker, not Planner.
        state["turbo_root"] = str(allowed[0])
        state["scope_verdict"] = "TURBO_SCOPE_ALLOWED"
        state["reason_code"] = "SANDBOX_UNAVAILABLE"
        return state
    except Exception:
        # Missing, invalid or concurrently changed policy can never grant.
        state["sandbox_status"] = "FAILED"
        state["status"] = "SANDBOX_ATTESTATION_FAILED"
        state["reason_code"] = "SANDBOX_ATTESTATION_FAILED"
        return state


def sandbox_evidence(state: dict, *, action_id: str = "",
                     requested_operation: str = "") -> dict[str, Any]:
    """Privacy-safe, non-authorizing blackboard receipt; no raw shell text."""
    keys = (
        "sandbox_mode", "sandbox_backend", "sandbox_identity",
        "sandbox_identity_verified", "turbo_root", "workspace_root",
        "policy_revision", "acl_attestation", "process_isolation_attestation",
        "child_process_policy", "scope_verdict", "sandbox_status",
        "sandbox_attestation_passed", "turbo_requested", "turbo_effective",
        "acl_mode",
    )
    receipt = {key: state.get(key) for key in keys}
    receipt.update({
        "schema": "SMARTAGENT_TURBO_SANDBOX_EVIDENCE_V1",
        "requested_operation": str(requested_operation)[:80],
        "os_execution_result": "NOT_EXECUTED",
        "os_access_denied": False,
        "failure_reason_code": str(state.get("reason_code") or "SANDBOX_UNAVAILABLE"),
        "action_id": str(action_id)[:128],
        "evidence_ref": "",
        "executed": False,
        "authorization_granted": False,
    })
    return receipt


def require_turbo_backend(state: dict) -> None:
    """Never reuse the Git45 ordinary command executor as a Turbo fallback."""
    if state.get("turbo_requested") != "ON":
        return
    # There is no independently implemented and validated OS broker yet.
    # Do NOT trust a state dict or an environment variable that says READY.
    raise RuntimeError(str(state.get("reason_code") or "SANDBOX_UNAVAILABLE"))


__all__ = ["SCHEMA", "resolve_turbo_state", "sandbox_evidence",
           "require_turbo_backend"]
