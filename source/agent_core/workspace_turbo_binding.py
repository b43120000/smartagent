"""Operator-owned Turbo setting for the Telegram WebGPT/Workspace update form.

One optional form field updates exactly the bound workspace's turbo roots.
The global Workspace policy remains the sole authority for Telegram, Manager,
WebCopilot and the Executor; the binding JSON itself does not grant Turbo rights.
"""
from __future__ import annotations

from pathlib import Path

from .path_security import is_within
from .workspace_access import (
    is_valid_turbo_root,
    load_workspace_access_policy,
    save_turbo_roots,
)


def _under(child: str | Path, workspace: str | Path) -> bool:
    """Strict child: workspace itself is never a Turbo root."""
    parent = Path(workspace).expanduser().absolute()
    target = Path(child).expanduser().absolute()
    return target != parent and is_within(target, parent)


def turbo_mode_for_workspace(workspace: str | Path) -> str:
    """Current setting for this binding, not a stale per-transport copy."""
    policy = load_workspace_access_policy()
    roots = [
        str(Path(path).expanduser().absolute())
        for path in policy.get("turbo_roots") or ()
        if _under(path, workspace)
    ]
    return ";".join(roots) if roots and policy.get("turbo_requested") is True else "off"


def prepare_turbo_mode_update(mode: str | None, workspace: str | Path) -> dict | None:
    """Validate before a binding mutation; produce NO authorization or write."""
    if mode is None:
        return None  # Backward-compatible pre-Git44 manager form: no Turbo change.
    parent = Path(workspace).expanduser().resolve()
    policy = load_workspace_access_policy()
    writable = policy.get("writable_workspaces") or []
    if str(parent).casefold() not in {str(Path(p).resolve()).casefold() for p in writable}:
        raise ValueError("turbo_workspace_not_registered_writable")
    raw = str(mode).strip().strip('"')
    if not raw:
        raise ValueError("turbomode_value_required")
    disabled = raw.casefold() == "off"
    existing = [str(p) for p in policy.get("turbo_roots") or ()]
    local = [p for p in existing if _under(p, parent)]
    other = [p for p in existing if not _under(p, parent)]
    chosen = "off"
    if disabled:
        proposed = other
    else:
        # One path means ADD; a semicolon-delimited form is a complete
        # replacement of THIS Workspace's list. '-<absolute path>' removes
        # just one root. Registrations never install NTFS grants by themselves.
        from .windows_security import acl_mode
        try:
            current_acl = acl_mode()
        except (OSError, ValueError, RuntimeError):
            current_acl = "unknown"
        if current_acl != "on":
            raise ValueError("TURBO_REQUIRES_ACL_ON")
        is_removal = raw.startswith("-")
        exact_list = ";" in raw and not is_removal
        items = (
            [item.strip() for item in raw.split(";") if item.strip()]
            if exact_list else [raw[1:].strip() if is_removal else raw]
        )
        if not items:
            raise ValueError("turbomode_value_required")
        target_paths: list[str] = []
        for item in items:
            target = Path(item).expanduser()
            if not target.is_absolute():
                raise ValueError("turbo_absolute_path_required")
            if not is_removal and not is_valid_turbo_root(
                target, parent, writable,
                policy.get("read_only_roots") or (),
                policy.get("denied_write_roots") or (),
            ):
                raise ValueError("turbo_root_not_strict_writable_workspace_child")
            target_paths.append(str(target.absolute()))
        if is_removal:
            if len(target_paths) != 1:
                raise ValueError("turbomode_single_removal_required")
            local = [p for p in local
                     if p.casefold() != target_paths[0].casefold()]
        elif exact_list:
            local = target_paths
        else:
            if all(p.casefold() != target_paths[0].casefold() for p in local):
                local.append(target_paths[0])
        chosen = ";".join(local) if local else "off"
        proposed = other + local
    return {
        "mode": chosen,
        "workspace": str(parent),
        "roots": proposed,
        "expected_revision": int(policy.get("revision", 0) or 0),
        "status": "DISABLED" if disabled else "ENABLED",
    }


def commit_turbo_mode_update(prepared: dict | None) -> dict | None:
    """Commit only after the host's idle/scope/security binding preflight."""
    if prepared is None:
        return None
    # Also recheck against a concurrent Workspace Manager edit inside the
    # shared policy's file lock (save_turbo_roots implements revision CAS).
    return save_turbo_roots(
        prepared["roots"], expected_revision=int(prepared["expected_revision"]),
    )


__all__ = [
    "turbo_mode_for_workspace", "prepare_turbo_mode_update",
    "commit_turbo_mode_update",
]
