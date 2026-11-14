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
        "turbo_roots": [],
        "turbo_requested": False,
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
            "turbo_roots": list(_normalized_roots(value.get("turbo_roots") or ())),
            # Git45 roots do not silently migrate to an enabled Git46 sandbox.
            "turbo_requested": value.get("turbo_requested") is True,
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
    turbo = [path for path in previous.get("turbo_roots", [])
             if any(is_valid_turbo_root(path, parent, writable,
                                        read_only, denied) for parent in writable)]
    payload = {
        "schema": ACCESS_POLICY_SCHEMA,
        "revision": int(previous.get("revision", 0) or 0) + 1,
        "updated_at": int(time.time()),
        "writable_workspaces": list(writable),
        "read_only_roots": list(read_only),
        "denied_write_roots": list(denied),
        "turbo_roots": list(turbo),
        # Changing workspace authority revokes any previous Turbo request.
        "turbo_requested": False,
    }
    path = workspace_access_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return payload


def builtin_toolchain_read_roots() -> tuple[str, ...]:
    """Runtime-owned read-only toolchain installed with SmartAgent.

    This path is never supplied by the Planner or Edit Workspace.  It does
    not grant execution trust: the native broker independently verifies each
    pinned Git/Python executable and enforces the AppContainer RX ACL.
    """
    try:
        from .path_security import is_reparse_path
        profile_path = default_profile_path()
        if (profile_path.parent.name.casefold() != "windows_security"
                or profile_path.parent.parent.name.casefold() != "secure"
                or profile_path.parent.parent.parent.name.casefold() != "localdata"):
            return ()
        install_root = profile_path.parents[3]
        toolchain = install_root / ".venv"
        if not toolchain.is_dir() or is_reparse_path(toolchain):
            return ()
        return (str(toolchain.resolve(strict=True)),)
    except (OSError, ValueError, RuntimeError, IndexError):
        return ()


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
    builtins = builtin_toolchain_read_roots()
    protected_read_only = _normalized_roots(
        (
            *tuple(profile.get("skill_roots") or ()),
            profile.get("executor_code_root", ""),
            *builtins,
        )
    )
    read_only = tuple(dict.fromkeys((*user_read_only, *protected_read_only)))
    return {
        "profile": profile,
        "policy": policy,
        "writable_workspaces": writable,
        "read_only_roots": read_only,
        "builtin_toolchain_read_roots": builtins,
        "turbo_roots": tuple(policy.get("turbo_roots") or ()),
        "turbo_requested": policy.get("turbo_requested") is True,
    }



def is_valid_turbo_root(path: str | Path, workspace: str | Path,
                        writable_roots, read_only_roots=(),
                        denied_write_roots=()) -> bool:
    """Turbo is an opt-in within an *existing* writable workspace, never ACL."""
    from .path_security import reject_reparse_components, is_within
    try:
        # Check original path components BEFORE resolving symlinks/junctions.
        # Otherwise an in-root symlink can disappear from a canonicalized path.
        requested = Path(path).expanduser().absolute()
        parent = Path(workspace).expanduser().absolute()
        if not is_within(requested, parent):
            return False
        reject_reparse_components(requested, parent)
        # A future child directory may be preconfigured before a deployment
        # creates it. Every existing component must still pass reparse checks.
        target = requested.resolve(strict=False)
        root = parent.resolve(strict=True)
        if (not root.is_dir() or target == root
                or (target.exists() and not target.is_dir())):
            return False
        if not is_within(target, root):
            return False
        # Reject symlink/reparse traversal, including pre-existing junctions.
        reject_reparse_components(target, root)
        if not any(is_within(root, Path(p).expanduser().resolve())
                   for p in writable_roots):
            return False
        from .path_security import workspace_path_mode
        reads = tuple(Path(p).expanduser().resolve() for p in read_only_roots)
        writes = tuple(Path(p).expanduser().resolve() for p in writable_roots)
        if workspace_path_mode(target, writes, reads) != "writable":
            return False
        if any(is_within(p, target) for p in reads):
            return False
        if any(is_within(target, Path(p).expanduser().resolve()) or
               is_within(Path(p).expanduser().resolve(), target)
               for p in denied_write_roots):
            return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_turbo_authorization_workspace(
    task_directory: str | Path | None, *, snapshot: dict | None = None,
) -> Path | None:
    """Find the UNIQUE policy-owned parent of a task working directory.

    This is identity resolution, NOT a filesystem or OS permission grant.
    Task/CWD is never allowed to nominate an additional writable container.
    Ambiguous, missing, reparse and out-of-policy locations fail closed.
    """
    if task_directory is None:
        return None
    from .path_security import is_within, reject_reparse_components
    try:
        access = snapshot if snapshot is not None else access_snapshot()
        directory = Path(task_directory).expanduser().absolute()
        if not directory.is_dir():
            return None
        candidates: dict[str, Path] = {}
        for raw_parent in access.get("writable_workspaces") or ():
            parent = Path(raw_parent).expanduser().resolve(strict=True)
            if not parent.is_dir() or not is_within(directory, parent):
                continue
            reject_reparse_components(directory, parent)
            if not any(
                is_valid_turbo_root(
                    root, parent, access.get("writable_workspaces") or (),
                    access.get("read_only_roots") or (),
                    (access.get("policy") or {}).get("denied_write_roots") or (),
                )
                for root in access.get("turbo_roots") or ()
            ):
                continue
            candidates[str(parent).casefold()] = parent
        return next(iter(candidates.values())) if len(candidates) == 1 else None
    except (OSError, RuntimeError, ValueError):
        return None

def save_turbo_roots(turbo_roots, *, workspace: str | Path | None = None,
                     expected_revision: int | None = None) -> dict:
    """Atomic shared policy; Telegram and Workspace Manager read same source.

    The exact writable workspace is supplied by the authenticated caller.
    Do not infer it from arbitrary user text or from a model's action.
    """
    from .process_file_lock import exclusive_process_lock
    policy_path = workspace_access_policy_path()
    lock = policy_path.with_name("workspace_access_policy.lock")
    with exclusive_process_lock(lock, label="turbo-workspace-policy"):
        current = load_workspace_access_policy()
        if (expected_revision is not None and
                int(current.get("revision", 0) or 0) != expected_revision):
            raise RuntimeError("turbo_policy_concurrent_update_detected")
        writable = current["writable_workspaces"]
        read_only = current["read_only_roots"]
        denied = current["denied_write_roots"]
        parent = str(Path(workspace).expanduser().resolve()) if workspace else ""
        requested = list(_normalized_roots(turbo_roots))
        if requested:
            try:
                mode = acl_mode()
            except (OSError, ValueError, RuntimeError):
                mode = "unknown"
            if mode != "on":
                raise ValueError("TURBO_REQUIRES_ACL_ON")
        for item in requested:
            parents = [parent] if parent else writable
            if not any(is_valid_turbo_root(item, root, writable, read_only, denied)
                       for root in parents):
                raise ValueError("turbo_root_not_strict_writable_workspace_child:" + item)
        payload = dict(current)
        payload["turbo_roots"] = requested
        # ON is only a requested preference, NEVER an OS execution grant.
        payload["turbo_requested"] = bool(requested)
        payload["revision"] = int(current.get("revision", 0) or 0) + 1
        payload["updated_at"] = int(time.time())
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = policy_path.with_name(policy_path.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, policy_path)
        finally:
            temporary.unlink(missing_ok=True)
        return payload


def set_turbo_root(path: str | Path, *, workspace: str | Path,
                   enabled: bool = True) -> dict:
    """User-control entrypoint; never grants filesystem rights by itself."""
    parent = Path(workspace).expanduser().resolve()
    existing = load_workspace_access_policy()
    candidates = [p for p in existing.get("turbo_roots", [])
                  if _key(p) != _key(path)]
    if enabled:
        candidates.append(str(Path(path).expanduser().resolve()))
    # Validates *all* requested turbo roots using registered workspace parents
    # and the new root against the caller's exact active workspace.
    if enabled and not is_valid_turbo_root(
            path, parent, existing["writable_workspaces"],
            existing["read_only_roots"], existing["denied_write_roots"]):
        raise ValueError("turbo_root_not_strict_writable_workspace_child")
    return save_turbo_roots(candidates,
                            expected_revision=int(existing.get("revision", 0) or 0))

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
    "is_valid_turbo_root", "resolve_turbo_authorization_workspace",
    "save_turbo_roots", "set_turbo_root",
]
