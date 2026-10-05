#!/usr/bin/env python3
"""Windows identity, NTFS, and restricted-executor attestation."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from .json_state_io import read_json_retry
from .protocol_manifest import PROTOCOL_FAMILY, PROTOCOL_VERSION
from .protocol_v8 import sha256_digest

SECURITY_SCHEMAS = {
    "SMARTAGENT_WINDOWS_SECURITY_V1",
    "SMARTAGENT_WINDOWS_SECURITY_V2",
}
MACHINE_AUTHORIZATION_SCHEMA = "SMARTAGENT_MACHINE_AUTHORIZATION_V1"


def _local_security_profile_path() -> Path:
    return (Path(__file__).resolve().parents[2] / "localdata" / "secure" / "windows_security" / "security_profile.json").resolve()


def machine_authorization_path() -> Path:
    """Return the machine-wide pointer to the authoritative security profile."""
    explicit = str(
        os.environ.get("SMARTAGENT_MACHINE_AUTHORIZATION", "") or ""
    ).strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    program_data = str(
        os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or ""
    ).strip()
    if not program_data:
        program_data = str(Path.home() / "AppData" / "Local")
    return (Path(program_data) / "SmartAgent" / "machine_authorization.json").resolve()


def load_machine_authorization() -> dict | None:
    """Load and validate the protected machine authorization pointer."""
    path = machine_authorization_path()
    if not path.is_file():
        return None
    try:
        value = read_json_retry(path)
    except Exception as exc:
        raise RuntimeError(f"machine_authorization_unreadable:{path}") from exc
    if not isinstance(value, dict) or value.get("schema") != MACHINE_AUTHORIZATION_SCHEMA:
        raise RuntimeError("machine_authorization_invalid")
    if value.get("protocol_family") != PROTOCOL_FAMILY or int(
        value.get("protocol_version", 0) or 0
    ) != PROTOCOL_VERSION:
        raise RuntimeError("machine_authorization_protocol_mismatch")
    expected_manifest = str(value.get("protocol_manifest_sha256", "") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest):
        raise RuntimeError("machine_authorization_manifest_digest_missing")
    current_manifest_path = Path(__file__).resolve().parents[2] / "config" / "protocol_manifest.json"
    if not current_manifest_path.is_file():
        raise RuntimeError("machine_authorization_local_manifest_missing")
    current_manifest = hashlib.sha256(current_manifest_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(current_manifest, expected_manifest):
        raise RuntimeError("machine_authorization_manifest_mismatch")
    raw_profile = str(value.get("security_profile", "") or "").strip()
    if not raw_profile:
        raise RuntimeError("machine_authorization_profile_missing")
    profile = Path(raw_profile).expanduser()
    if not profile.is_absolute():
        raise RuntimeError("machine_authorization_profile_not_absolute")
    profile = profile.resolve()
    if not profile.is_file():
        raise RuntimeError(f"machine_authorization_profile_unavailable:{profile}")
    value["security_profile"] = str(profile)
    value["authorization_file"] = str(path)
    return value


def default_profile_path() -> Path:
    explicit = str(os.environ.get("SMARTAGENT_SECURITY_PROFILE", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    local_profile = _local_security_profile_path()
    if local_profile.with_name("acl_mode.json").is_file() and acl_mode(local_profile) == "off":
        return local_profile
    module_path = Path(__file__).resolve()
    # The scheduled executor runs from the protected executor_code copy.  Its
    # adjacent profile is authoritative even if the machine pointer is being
    # replaced during a repair transaction.
    for parent in module_path.parents:
        if parent.name.casefold() == "windows_security" and parent.parent.name.casefold() == "secure":
            return parent / "security_profile.json"
    machine = load_machine_authorization()
    if machine is not None:
        return Path(machine["security_profile"])
    return module_path.parents[2] / "localdata" / "secure" / "windows_security" / "security_profile.json"


def acl_mode(profile_path: str | Path | None = None) -> str:
    profile = Path(profile_path or default_profile_path()).expanduser().resolve()
    mode_path = profile.with_name("acl_mode.json")
    if not mode_path.is_file():
        return "on" if profile.is_file() else "off"
    try:
        value = read_json_retry(mode_path)
    except Exception as exc:
        raise RuntimeError(f"acl_mode_unreadable:{mode_path}") from exc
    if not isinstance(value, dict) or value.get("schema") != "SMARTAGENT_ACL_MODE_V1":
        raise RuntimeError("acl_mode_invalid")
    mode = str(value.get("mode", "") or "").strip().lower()
    if mode not in {"on", "off"}:
        raise RuntimeError("acl_mode_invalid")
    expected_root = profile.parent.parent.parent.parent.resolve()
    recorded_root = Path(str(value.get("install_root", "") or "")).expanduser()
    if not recorded_root.is_absolute() or recorded_root.resolve() != expected_root:
        raise RuntimeError("acl_mode_install_root_mismatch")
    return mode


def restricted_executor_required(
    profile_path: str | Path | None = None,
) -> bool:
    """Return whether this process must use the OS restricted executor.

    ``ACLstatus.bat off`` is an explicit transition to software-guard-only
    mode.  The profile intentionally remains on disk so ``on`` can restore the
    protection without reinstalling; profile existence therefore cannot be
    used as the mode switch.  The persisted ACL mode takes precedence even
    over the launcher's legacy ``SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED``
    environment variable.

    Invalid or unreadable mode state remains fail-closed when a profile is
    present.  Only a valid, installation-bound ``off`` record disables the
    restricted executor.
    """
    if profile_path is not None:
        profile = Path(profile_path).expanduser().resolve()
    else:
        module_root = Path(__file__).resolve().parents[2]
        local_profile = (
            module_root
            / "localdata"
            / "secure"
            / "windows_security"
            / "security_profile.json"
        )
        # Read the local mode before consulting the machine authorization.
        # This keeps an explicit software-only installation usable while its
        # protected executor/profile is stale or under repair.
        if local_profile.with_name("acl_mode.json").is_file():
            try:
                if acl_mode(local_profile) == "off":
                    return False
            except Exception:
                if local_profile.is_file():
                    return True
                raise
        profile = default_profile_path()

    if profile.with_name("acl_mode.json").is_file():
        try:
            if acl_mode(profile) == "off":
                return False
        except Exception:
            if profile.is_file():
                return True
            raise

    explicit = os.environ.get(
        "SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    return explicit or profile.is_file()


def filesystem_type(path: str | Path) -> str:
    if os.name != "nt":
        return "non-windows"
    root = Path(path).resolve().anchor
    volume = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    maxlen = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        root, volume, len(volume), ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fs, len(fs)
    )
    return fs.value.upper() if ok else "UNKNOWN"


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def load_security_profile(path: str | Path | None = None) -> dict:
    source = Path(path or default_profile_path())
    if not source.is_file():
        raise RuntimeError(f"security_profile_missing:{source}")
    value = read_json_retry(source)
    if not isinstance(value, dict) or value.get("schema") not in SECURITY_SCHEMAS:
        raise RuntimeError("security_profile_invalid")
    if value.get("schema") == "SMARTAGENT_WINDOWS_SECURITY_V2":
        workspace_roots = list(value.get("authorized_workspace_roots") or [])
        if not workspace_roots:
            raise RuntimeError("security_profile_workspace_roots_missing")
        if not list(value.get("skill_roots") or []):
            raise RuntimeError("security_profile_skill_roots_missing")
    return value


def authorized_workspace_roots(profile: dict) -> list[Path]:
    values = list(profile.get("authorized_workspace_roots") or [])
    if not values and profile.get("workspace_container"):
        values = [profile["workspace_container"]]
    return [Path(value).resolve() for value in values if str(value or "").strip()]


def workspace_traverse_roots(profile: dict) -> list[Path]:
    """Return root-only RX ancestors required to reach configured workspaces."""
    return [
        Path(value).resolve()
        for value in profile.get("workspace_traverse_roots") or []
        if str(value or "").strip()
    ]


def expected_workspace_traverse_roots(profile: dict) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    workspaces = authorized_workspace_roots(profile)
    for workspace in workspaces:
        anchor = Path(workspace.anchor).resolve()
        for parent in workspace.parents:
            resolved = parent.resolve()
            if _same_path(resolved, anchor):
                break
            if any(_is_within(resolved, root) for root in workspaces):
                continue
            key = os.path.normcase(str(resolved)).casefold()
            if key not in seen:
                seen.add(key)
                result.append(resolved)
    return result


def trusted_executable_records(profile: dict) -> tuple[dict, ...]:
    records = []
    for row in profile.get("trusted_executables") or ():
        if not isinstance(row, dict):
            continue
        raw_path = str(row.get("path", "") or "").strip()
        digest = str(row.get("sha256", "") or "").strip().lower()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        records.append({
            "path": str(Path(raw_path).expanduser().resolve()),
            "sha256": digest,
            "kind": str(row.get("kind", "") or ""),
        })
    return tuple(records)


def is_trusted_executable(path: str | Path, profile: dict | None = None) -> bool:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        return False
    local_profile = _local_security_profile_path()
    if local_profile.with_name("acl_mode.json").is_file() and acl_mode(local_profile) == "off":
        return True
    active = profile if isinstance(profile, dict) else load_security_profile()
    expected = ""
    candidate_key = os.path.normcase(str(candidate)).casefold()
    for row in trusted_executable_records(active):
        if os.path.normcase(str(Path(row["path"]).resolve())).casefold() == candidate_key:
            expected = row["sha256"]
            break
    if not expected:
        return False
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hmac.compare_digest(digest, expected)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())).casefold() == os.path.normcase(
        str(right.resolve())
    ).casefold()


def attest(
    workspace: str | Path, *, profile_path: str | Path | None = None,
    require_executor: bool = True,
) -> dict:
    root = Path(workspace).resolve()
    result = {
        "schema": "SMARTAGENT_SECURITY_ATTESTATION_V1",
        "workspace": str(root),
        "filesystem": filesystem_type(root),
        "user": getpass.getuser(),
        "is_admin": is_admin(),
        "profile_path": str(profile_path or default_profile_path()),
        "passed": False,
        "reasons": [],
    }
    try:
        profile = load_security_profile(profile_path)
    except Exception as exc:
        result["reasons"].append(str(exc))
        return result

    containers = authorized_workspace_roots(profile)
    endpoint = Path(profile.get("executor_endpoint", "")).resolve()
    controller_state = (
        Path(profile["controller_state_root"]).resolve()
        if profile.get("controller_state_root") else None
    )
    executor_python = (
        Path(profile["executor_python"]).resolve()
        if profile.get("executor_python") else None
    )
    executor_code = (
        Path(profile["executor_code_root"]).resolve()
        if profile.get("executor_code_root") else None
    )
    skill_roots = [Path(value).resolve() for value in profile.get("skill_roots") or []]
    traverse_roots = workspace_traverse_roots(profile)
    expected_traverse = expected_workspace_traverse_roots(profile)

    if not containers or not any(_same_path(root, value) for value in containers):
        result["reasons"].append("workspace_outside_configured_container")
    if result["filesystem"] != "NTFS":
        result["reasons"].append("workspace_not_ntfs")
    if not list(profile.get("denied_write_roots") or []):
        result["reasons"].append("denied_write_roots_missing")
    if not list(profile.get("root_create_denied") or []):
        result["reasons"].append("root_create_denied_missing")
    if controller_state is None or not controller_state.is_dir():
        result["reasons"].append("controller_state_root_unavailable")
    if executor_python is None or not executor_python.is_file():
        result["reasons"].append("restricted_executor_python_unavailable")
    if executor_code is None or not executor_code.is_dir():
        result["reasons"].append("restricted_executor_code_unavailable")
    if profile.get("schema") == "SMARTAGENT_WINDOWS_SECURITY_V2" and not skill_roots:
        result["reasons"].append("restricted_executor_skill_roots_missing")
    if any(not path.is_dir() for path in skill_roots):
        result["reasons"].append("restricted_executor_skill_root_unavailable")
    if any(not path.is_dir() for path in traverse_roots):
        result["reasons"].append("workspace_traverse_root_unavailable")
    if any(not any(_same_path(path, value) for value in traverse_roots) for path in expected_traverse):
        result["reasons"].append("workspace_traverse_roots_incomplete")
    if require_executor and not endpoint.is_dir():
        result["reasons"].append("restricted_executor_endpoint_unavailable")
    if require_executor and endpoint.is_dir():
        try:
            state = read_json_retry(endpoint / "service_state.json")
        except Exception:
            state = {}
        if time.time() - float(state.get("heartbeat_at", 0) or 0) > 10:
            result["reasons"].append("restricted_executor_service_not_ready")
        if str(state.get("user", "")).casefold() != str(
            profile.get("executor_user", "")
        ).casefold():
            result["reasons"].append("restricted_executor_service_identity_mismatch")
        if bool(state.get("is_admin", True)):
            result["reasons"].append("restricted_executor_service_is_admin")
        if str(state.get("profile_digest", "")) != sha256_digest(profile):
            result["reasons"].append("restricted_executor_profile_mismatch")
        if not bool((state.get("self_test") or {}).get("passed", False)):
            result["reasons"].append("restricted_executor_acl_self_test_failed")

    result["executor_endpoint"] = str(endpoint)
    result["executor_user"] = str(profile.get("executor_user", ""))
    result["passed"] = not result["reasons"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--allow-software-only", action="store_true")
    parser.add_argument("--resolve-profile", action="store_true")
    args = parser.parse_args(argv)
    if args.resolve_profile:
        print(default_profile_path())
        return 0
    if not args.workspace:
        parser.error("--workspace is required unless --resolve-profile is used")
    report = attest(
        args.workspace, profile_path=args.profile or None,
        require_executor=not args.allow_software_only,
    )
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
