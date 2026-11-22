"""Git49 per-action Turbo OS broker scope contract.

Preparing a request is NOT executing it and cannot authorize Windows tokens.
Only an OS-attested privileged broker may redeem a request. Never trust a
Planner-supplied path, policy revision, PID, SID, or attestation field.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .path_security import (
    PathSecurityError, canonicalize_path, is_within, reject_reparse_components,
)
from .turbo_sandbox import resolve_turbo_state

SCHEMA = "SMARTAGENT_TURBO_BROKER_SCOPE_REQUEST_V1"
_KINDS = frozenset({
    "RUN_COMMAND", "WRITE_FILE", "CREATE_DIRECTORY", "DELETE_PATH",
    "APPLY_EDIT_PLAN", "EXECUTE_SCRIPT", "COMPILE", "GIT_OPERATION",
})


class TurboBrokerScopeError(RuntimeError):
    pass


def _checked_path(path: str | Path, root: Path) -> Path:
    """Reject text traversal/reparse BEFORE canonicalizing to avoid hiding it."""
    requested = Path(str(path)).expanduser()
    if not requested.is_absolute():
        raise TurboBrokerScopeError("TURBO_BROKER_ABSOLUTE_PATH_REQUIRED")
    raw = requested.absolute()
    if ".." in raw.parts:
        raise TurboBrokerScopeError("TURBO_BROKER_PARENT_TRAVERSAL_FORBIDDEN")
    if not is_within(raw, root):
        raise TurboBrokerScopeError("TURBO_BROKER_PATH_OUTSIDE_ROOT")
    try:
        reject_reparse_components(raw, root)
        canonical = canonicalize_path(str(raw), require_absolute=True,
                                      forbid_glob=True)
    except PathSecurityError as exc:
        raise TurboBrokerScopeError("TURBO_BROKER_PATH_UNSAFE") from exc
    if not is_within(canonical, root):
        raise TurboBrokerScopeError("TURBO_BROKER_PATH_OUTSIDE_ROOT")
    return canonical


def prepare_scope_request(*, workspace: str | Path, working_directory: str | Path,
                          expected_revision: int, task_id: str, action_id: str,
                          kind: str, target: str | Path | None = None) -> dict[str, Any]:
    """Prepare a non-authorizing, revision-bound OS Broker input.

    The actual SID, process token, Job Object and NTFS ACL must be attested
    outside this process on every execution. A SHA256 is an accidental-change
    detector, not a signature, lease, authentication or OS grant.
    """
    if kind not in _KINDS:
        raise TurboBrokerScopeError("TURBO_BROKER_OPERATION_UNSUPPORTED")
    if (not task_id or not action_id or len(task_id) > 128 or
            len(action_id) > 128 or any(ord(c) < 33 for c in task_id + action_id)):
        raise TurboBrokerScopeError("TURBO_BROKER_ACTION_ID_INVALID")
    if type(expected_revision) is not int or expected_revision < 1:
        raise TurboBrokerScopeError("TURBO_BROKER_REVISION_INVALID")
    state = resolve_turbo_state(workspace)
    if state["sandbox_status"] == "FAILED":
        raise TurboBrokerScopeError(state["reason_code"])
    if state["acl_mode"] != "on" or state["turbo_requested"] != "ON":
        raise TurboBrokerScopeError("TURBO_BROKER_NOT_REQUESTED")
    if state["policy_revision"] != expected_revision:
        raise TurboBrokerScopeError("TURBO_BROKER_STALE_POLICY_REVISION")
    if state["scope_verdict"] != "TURBO_SCOPE_ALLOWED" or not state["turbo_root"]:
        raise TurboBrokerScopeError("TURBO_BROKER_ROOT_UNVERIFIED")
    root = Path(state["turbo_root"]).expanduser().absolute()
    # Never treat a non-existent or reparse directory as an OS-granted root.
    if not root.is_dir() or root.is_symlink():
        raise TurboBrokerScopeError("TURBO_BROKER_ROOT_NOT_DIRECTORY")
    cwd = _checked_path(working_directory, root)
    if not cwd.is_dir():
        raise TurboBrokerScopeError("TURBO_BROKER_CWD_NOT_DIRECTORY")
    resolved_target = str(_checked_path(target, root)) if target is not None else ""
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "workspace_root": str(Path(workspace).expanduser().resolve(strict=True)),
        "turbo_root": str(root.resolve(strict=True)),
        "working_directory": str(cwd),
        "target": resolved_target,
        "policy_revision": state["policy_revision"],
        "task_id": task_id,
        "action_id": action_id,
        "kind": kind,
        "broker_lease": "",
        "os_sid": "",
        "os_token_verified": False,
        "os_acl_attested": False,
        "authorization_granted": False,
        "execution_status": "PREPARED_NOT_EXECUTED",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["content_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return payload


__all__ = ["SCHEMA", "TurboBrokerScopeError", "prepare_scope_request"]
