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
        if mode not in {"on", "off"}:
            # An unknown ACL mode must never be treated as an explicit OFF.
            state["sandbox_status"] = "FAILED"
            state["status"] = "SANDBOX_ATTESTATION_FAILED"
            state["reason_code"] = "ACL_MODE_UNVERIFIED"
            return state
        if mode == "off":
            # Git48: explicit ACL OFF always routes through the ordinary
            # software-guard execution path, including after a stale ON
            # preference survives a crash or concurrent policy update.
            # Keep requested=ON for diagnostics, but never enable Turbo.
            state["status"] = "TURBO_DISABLED"
            state["reason_code"] = (
                "TURBO_SUPPRESSED_ACL_OFF" if requested else "TURBO_DISABLED"
            )
            return state
        if not requested:
            state["status"] = "TURBO_DISABLED"
            state["reason_code"] = "TURBO_DISABLED"
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
    # A corrupt or unreadable policy MUST block even if the last known
    # requested flag was OFF. Otherwise policy errors become a fail-open path.
    if state.get("sandbox_status") == "FAILED":
        raise RuntimeError(str(state.get("reason_code") or "SANDBOX_ATTESTATION_FAILED"))
    # A validated ACL OFF is a global *disable*, not a Turbo fallback.
    # The ordinary executor still enforces its software guards and approvals.
    if state.get("acl_mode") == "off":
        if (state.get("turbo_effective") != "OFF"
                or state.get("authorization_granted") is not False):
            raise RuntimeError("ACL_OFF_TURBO_STATE_INCONSISTENT")
        return
    if state.get("turbo_requested") != "ON":
        return
    # There is no independently implemented and validated OS broker yet.
    # Do NOT trust a state dict or an environment variable that says READY.
    raise RuntimeError(str(state.get("reason_code") or "SANDBOX_UNAVAILABLE"))


def main(argv=None) -> int:
    """Shared read-only status endpoint for Windows CLI and UI diagnostics."""
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="")
    args = parser.parse_args(argv)
    workspace = args.workspace or None
    if not workspace:
        try:
            from .remote_binding import active
            live = active() or {}
            workspace = str(live.get("workspace") or "").strip() or None
        except (ImportError, RuntimeError, ValueError, OSError):
            workspace = None
    if not workspace:
        try:
            snapshot = access_snapshot()
            workspace = next(iter(snapshot.get("writable_workspaces") or ()), None)
        except (OSError, RuntimeError, ValueError):
            workspace = None
    state = resolve_turbo_state(workspace)
    # Advisory enrollment status only. This must never set global READY/ON:
    # native Windows process/token + outside-root denial are re-attested on
    # EVERY action. A protected journal alone is not a Full-Access grant.
    state["broker_enrolled"] = False
    if (workspace is not None and state["acl_mode"] == "on" and (
            state["turbo_requested"] == "ON" and
            state["scope_verdict"] == "TURBO_SCOPE_ALLOWED")):
        try:
            from .turbo_os_broker_client import active_broker_available
            if active_broker_available(workspace):
                state["broker_enrolled"] = True
                state["status"] = "BROKER_ENROLLED"
                state["reason_code"] = "TURBO_OS_BROKER_PER_ACTION_ATTESTATION_REQUIRED"
                state["sandbox_backend"] = "WIN32_APPCONTAINER_ONESHOT"
        except (OSError, RuntimeError, ValueError):
            pass
    print(json.dumps(state, ensure_ascii=False, sort_keys=True))
    return 0 if (state["sandbox_status"] != "FAILED" and
                 (state["acl_mode"] == "off" or
                  state["turbo_requested"] == "OFF")) else 2


__all__ = ["SCHEMA", "resolve_turbo_state", "sandbox_evidence",
           "require_turbo_backend"]


if __name__ == "__main__":
    raise SystemExit(main())
