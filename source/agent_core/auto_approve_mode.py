"""Explicit ACL-OFF secondary confirmation opt-in. Never an execution permit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .workspace_access import (
    access_snapshot, load_workspace_access_policy, workspace_access_policy_path,
)
from .windows_security import acl_mode

_CONTROL = re.compile(r"(?is)^\s*auto[_ -]?approve\s*=\s*(on|off|status)\s*$")


def parse_auto_approve_control(text: str) -> str | None:
    match = _CONTROL.fullmatch(str(text or ""))
    return match.group(1).upper() if match else None


def auto_approve_snapshot(workspace=None, working_directory=None) -> dict:
    state = {
        "schema": "SMARTAGENT_AUTO_APPROVE_STATE_V1",
        "acl": "UNKNOWN", "turbo_requested": "OFF", "turbo_effective": "OFF",
        "auto_approve_requested": "OFF", "auto_approve_effective": "OFF",
        "executor_backend": "UNVERIFIED", "workspace_root": str(workspace or ""),
        "policy_revision": 0, "reason_code": "POLICY_UNVERIFIED",
    }
    try:
        from .path_security import is_within, reject_reparse_components, workspace_path_mode
        access = access_snapshot()
        policy = access["policy"]
        state["policy_revision"] = int(policy.get("revision", 0) or 0)
        requested = policy.get("auto_approve_requested") is True
        state["auto_approve_requested"] = "ON" if requested else "OFF"
        state["turbo_requested"] = "ON" if policy.get("turbo_requested") is True else "OFF"
        mode = acl_mode()
        state["acl"] = str(mode).upper()
        if mode not in {"off", "on"}:
            state["reason_code"] = "ACL_UNVERIFIED"
            return state
        if mode == "on":
            from .turbo_sandbox import resolve_turbo_state
            turbo = resolve_turbo_state(workspace)
            state["turbo_effective"] = turbo["turbo_effective"]
            state["executor_backend"] = (
                "NATIVE_APPCONTAINER_BROKER" if state["turbo_requested"] == "ON"
                else "RESTRICTED_EXECUTOR"
            )
            state["reason_code"] = "ACL_ON_AUTO_APPROVE_INEFFECTIVE"
            return state
        state["executor_backend"] = "WINDOWS_EXECUTOR"
        if not requested:
            state["reason_code"] = "AUTO_APPROVE_DISABLED"
            return state
        if workspace is None:
            # A global setting is not a bound execution context. Never
            # advertise Effective ON without a real writable Workspace.
            state["reason_code"] = "WORKSPACE_NOT_BOUND"
            return state
        if workspace is not None:
            target = Path(workspace).expanduser().absolute()
            cwd = Path(working_directory or target).expanduser().absolute()
            writable = tuple(Path(p).expanduser().resolve(strict=True)
                             for p in access["writable_workspaces"])
            read_only = tuple(Path(p).expanduser().resolve(strict=False)
                              for p in access["read_only_roots"])
            denied = tuple(Path(p).expanduser().resolve(strict=False)
                           for p in policy.get("denied_write_roots") or ())
            def authorized_writable(directory: Path) -> bool:
                # ACL OFF can delegate work between independently authorized
                # writable roots (for example E:\\AIspace -> C:\\HDRViewer).
                # Each endpoint must qualify by itself. Auto Approve never
                # makes a new root writable or bypasses path/command guards.
                if (not directory.is_dir()
                        or workspace_path_mode(directory, writable, read_only) != "writable"
                        or any(is_within(directory, p) for p in denied)):
                    return False
                for root in writable:
                    if root.is_dir() and is_within(directory, root):
                        reject_reparse_components(directory, root)
                        return True
                return False

            if not (authorized_writable(target) and authorized_writable(cwd)):
                state["reason_code"] = "WORKSPACE_NOT_AUTHORIZED_WRITABLE"
                return state
        state["auto_approve_effective"] = "ON"
        state["reason_code"] = "ACL_OFF_WORKSPACE_POLICY_OPT_IN"
        return state
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        state["reason_code"] = "POLICY_OR_SCOPE_UNVERIFIED"
        return state


def evaluate_auto_approve(workspace, command: str, *, working_directory=None) -> dict:
    """Decision receipt, not permission. Call after original action admission."""
    state = auto_approve_snapshot(workspace, working_directory)
    result = {
        **state, "approved": False, "approval_kind": "SECONDARY_CONFIRMATION",
        "command_sha256": hashlib.sha256(str(command or "").encode("utf-8")).hexdigest(),
        "authorization_scope": str(workspace or ""), "executed": False,
    }
    if state["auto_approve_effective"] != "ON":
        return result
    try:
        from .command_security import inspect_command, build_command_approval_manifest
        cwd = str(working_directory or workspace)
        admission = inspect_command(command, workspace=cwd)
        if not admission.allowed and not admission.approval_eligible:
            result["reason_code"] = "COMMAND_SECURITY_HARD_DENIED:" + admission.code
            return result
        if admission.approval_eligible:
            build_command_approval_manifest(command, workspace=cwd)
        result["approved"] = True
        result["reason_code"] = "AUTO_APPROVE_SECONDARY_CONFIRMATION"
        return result
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        result["reason_code"] = "COMMAND_POLICY_UNVERIFIED:" + type(exc).__name__
        return result


def set_auto_approve_requested(enabled: bool, *, expected_revision=None) -> dict:
    if type(enabled) is not bool:
        raise ValueError("auto_approve_boolean_required")
    if enabled:
        # Only an explicit ACL OFF operator may arm the preference. ACL ON
        # and transitional/unknown states can never stage a latent ON.
        try:
            if acl_mode() != "off":
                raise ValueError("auto_approve_requires_acl_off")
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("auto_approve_acl_mode_unverified") from exc
    from .process_file_lock import exclusive_process_lock
    path = workspace_access_policy_path()
    lock = path.with_name("workspace_access_policy.lock")
    with exclusive_process_lock(lock, label="auto-approve-workspace-policy"):
        current = load_workspace_access_policy()
        if expected_revision is not None and current["revision"] != int(expected_revision):
            raise RuntimeError("auto_approve_policy_concurrent_update_detected")
        if (current.get("auto_approve_requested") is True) == enabled:
            return current
        payload = dict(current)
        payload["auto_approve_requested"] = enabled
        payload["revision"] = int(current.get("revision", 0) or 0) + 1
        payload["updated_at"] = int(time.time())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return payload


def apply_auto_approve_control(text: str, *, workspace) -> dict | None:
    choice = parse_auto_approve_control(text)
    if choice is None:
        return None
    if choice != "STATUS":
        from .workspace_access import require_writable_workspace
        require_writable_workspace(workspace)
        set_auto_approve_requested(choice == "ON")
    return auto_approve_snapshot(workspace)


def format_auto_approve_status(state: dict) -> str:
    return "\n".join((
        "Auto Approve Runtime",
        "ACL: " + state["acl"],
        "Turbo Requested: " + state["turbo_requested"],
        "Turbo Effective: " + state["turbo_effective"],
        "Auto Approve Requested: " + state["auto_approve_requested"],
        "Auto Approve Effective: " + state["auto_approve_effective"],
        "Executor Backend: " + state["executor_backend"],
        "Reason: " + state["reason_code"],
        "Policy revision: " + str(state["policy_revision"]),
    ))
