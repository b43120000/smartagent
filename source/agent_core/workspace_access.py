"""Persist and enforce workspace access independently of Windows ACL mode."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .windows_security import acl_mode, default_profile_path, load_security_profile


ACCESS_POLICY_SCHEMA = "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1"


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve())).casefold()


def _local_security_profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "localdata"
        / "secure"
        / "windows_security"
        / "security_profile.json"
    ).resolve()


def workspace_access_policy_path() -> Path:
    return _local_security_profile_path().with_name("workspace_access_policy.json")


def software_only_mode_enabled() -> bool:
    local_profile = _local_security_profile_path()
    local_mode = local_profile.with_name("acl_mode.json")
    if local_mode.is_file():
        return acl_mode(local_profile) == "off"
    if local_profile.is_file():
        return False
    options_path = local_profile.parents[2] / "metadata" / "provisioning_options.json"
    if not options_path.is_file():
        return False
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return (
        isinstance(options, dict)
        and options.get("schema") == "SMARTAGENT_PROVISIONING_OPTIONS_V1"
        and options.get("configure_security") is False
    )


def _normalized_roots(values) -> tuple[str, ...]:
    roots: dict[str, str] = {}
    for value in values or ():
        if not str(value or "").strip():
            continue
        resolved = str(Path(value).expanduser().resolve())
        roots.setdefault(_key(resolved), resolved)
    return tuple(roots.values())


def _load_local_profile() -> dict:
    path = _local_security_profile_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"security_profile_invalid:{path}") from exc
    return value if isinstance(value, dict) else {}


def _editable_profile_read_only(profile: dict) -> tuple[str, ...]:
    protected = {
        _key(value)
        for value in (
            *tuple(profile.get("skill_roots") or ()),
            profile.get("executor_code_root", ""),
        )
        if str(value or "").strip()
    }
    return tuple(
        value
        for value in _normalized_roots(profile.get("read_only_roots") or ())
        if _key(value) not in protected
    )


def _policy_from_profile(profile: dict) -> dict:
    return {
        "schema": ACCESS_POLICY_SCHEMA,
        "revision": 0,
        "updated_at": 0,
        "writable_workspaces": list(
            _normalized_roots(profile.get("authorized_workspace_roots") or ())
        ),
        "read_only_roots": list(_editable_profile_read_only(profile)),
        "denied_write_roots": list(
            _normalized_roots(profile.get("denied_write_roots") or ())
        ),
    }


def load_workspace_access_policy(*, profile: dict | None = None) -> dict:
    """Load the user policy; derive a non-persistent legacy view when absent."""
    path = workspace_access_policy_path()
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"workspace_access_policy_invalid:{path}") from exc
        if not isinstance(value, dict) or value.get("schema") != ACCESS_POLICY_SCHEMA:
            raise RuntimeError(f"workspace_access_policy_invalid:{path}")
        return {
            "schema": ACCESS_POLICY_SCHEMA,
            "revision": int(value.get("revision", 0) or 0),
            "updated_at": int(value.get("updated_at", 0) or 0),
            "writable_workspaces": list(
                _normalized_roots(value.get("writable_workspaces") or ())
            ),
            "read_only_roots": list(
                _normalized_roots(value.get("read_only_roots") or ())
            ),
            "denied_write_roots": list(
                _normalized_roots(value.get("denied_write_roots") or ())
            ),
        }
    return _policy_from_profile(profile if profile is not None else _load_local_profile())


def save_workspace_access_policy(
    writable_workspaces, read_only_roots, denied_write_roots=()
) -> dict:
    """Atomically publish the shared ON/OFF software authorization policy."""
    writable = _normalized_roots(writable_workspaces)
    read_only = _normalized_roots(read_only_roots)
    denied = _normalized_roots(denied_write_roots)
    if not writable:
        raise ValueError("at_least_one_writable_workspace_required")
    overlap = {_key(value) for value in writable} & {_key(value) for value in read_only}
    if overlap:
        raise ValueError(f"workspace_access_policy_mode_overlap:{sorted(overlap)[0]}")
    previous = load_workspace_access_policy()
    payload = {
        "schema": ACCESS_POLICY_SCHEMA,
        "revision": int(previous.get("revision", 0) or 0) + 1,
        "updated_at": int(time.time()),
        "writable_workspaces": list(writable),
        "read_only_roots": list(read_only),
        "denied_write_roots": list(denied),
    }
    path = workspace_access_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return payload


def access_snapshot() -> dict:
    software_only = software_only_mode_enabled()
    try:
        profile = _load_local_profile()
    except RuntimeError:
        if not software_only or not workspace_access_policy_path().is_file():
            raise
        profile = {}
    if not software_only:
        profile = load_security_profile(
            _local_security_profile_path()
            if _local_security_profile_path().is_file()
            else default_profile_path()
        )
    policy = load_workspace_access_policy(profile=profile)
    writable = tuple(policy["writable_workspaces"])
    user_read_only = tuple(policy["read_only_roots"])
    protected_read_only = _normalized_roots(
        (
            *tuple(profile.get("skill_roots") or ()),
            profile.get("executor_code_root", ""),
        )
    )
    read_only = tuple(dict.fromkeys((*user_read_only, *protected_read_only)))
    return {
        "profile": profile,
        "policy": policy,
        "writable_workspaces": writable,
        "read_only_roots": read_only,
    }


def require_writable_workspace(path: str | Path) -> str:
    candidate = str(Path(path).expanduser().resolve())
    if not Path(candidate).is_dir():
        raise ValueError(f"workspace_unavailable:{candidate}")
    allowed = {_key(value) for value in access_snapshot()["writable_workspaces"]}
    if _key(candidate) not in allowed:
        raise ValueError(f"workspace_not_in_authorized_registry:{candidate}")
    return candidate


__all__ = [
    "ACCESS_POLICY_SCHEMA",
    "access_snapshot",
    "load_workspace_access_policy",
    "require_writable_workspace",
    "save_workspace_access_policy",
    "software_only_mode_enabled",
    "workspace_access_policy_path",
]
