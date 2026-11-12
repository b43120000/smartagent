#!/usr/bin/env python3
"""Admission rules for free-form commands before any process is created."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .path_security import is_within


@dataclass(frozen=True)
class CommandAdmission:
    allowed: bool
    code: str = ""
    detail: str = ""
    approval_eligible: bool = False
    # Safe classifier-only metadata. Never persist an executable's raw path.
    target_kind: str = ""


class CommandSecurityError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


_DISK_FORMAT_COMMAND = re.compile(
    r'''(?ix)
    (?:
        ^
        |[;&|]\s*
        |\bcmd(?:\.exe)?\s+/[ck]\s+
        |\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n;&|]{0,160}?\s-(?:command|c)\s+
        |\bstart-process\b\s+(?:-filepath\s+)?
        |\bstart(?:\.exe)?\b\s+(?:""\s+)?
    )
    \s*["']?
    (?:(?:[a-z]:[\\/]|\\\\)[^"'\r\n]*[\\/])?
    format(?:\.com)?["']?
    (?=\s|$)
    ''',
)


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Match the actual command/executable token, not an argument such as
    # ``git log --format=%s`` or a filename containing the word "format".
    ("disk_format_forbidden", _DISK_FORMAT_COMMAND),
    ("diskpart_forbidden", re.compile(r"(?i)\bdiskpart(?:\.exe)?\b")),
    ("disk_management_forbidden", re.compile(r"(?i)\b(?:clear-disk|remove-partition|initialize-disk|set-disk)\b")),
    ("raw_device_forbidden", re.compile(r"(?i)(?:\\\\[.?]\\(?:physicaldrive|harddisk|globalroot)|\\device\\harddisk)")),
    ("acl_change_forbidden", re.compile(r"(?i)\b(?:takeown|icacls|cacls)(?:\.exe)?\b|\b(?:set-acl|set-owner)\b")),
    ("recursive_rmdir_requires_typed_delete", re.compile(r"(?i)\b(?:rmdir|rd)(?:\.exe)?\b(?=[^\r\n]*?(?:/s\b|-s\b|--recursive\b))")),
    ("recursive_del_requires_typed_delete", re.compile(r"(?i)\b(?:del|erase)(?:\.exe)?\b(?=[^\r\n]*?/s\b)")),
    ("recursive_remove_item_requires_typed_delete", re.compile(r"(?i)\b(?:remove-item|rm|ri)\b(?=[^\r\n]*?(?:-recurse\b|-r\b))")),
    ("mirror_delete_requires_typed_operation", re.compile(r"(?i)\brobocopy(?:\.exe)?\b(?=[^\r\n]*?(?:/mir\b|/purge\b))")),
    ("git_clean_force_forbidden", re.compile(r"(?i)\bgit(?:\.exe)?\s+clean\b(?=[^\r\n]*(?:-[a-z]*f[a-z]*\b|--force\b))")),
    ("dynamic_code_execution_forbidden", re.compile(r"(?i)\b(?:invoke-expression|iex|mshta|regsvr32|rundll32)(?:\.exe)?\b|(?:^|[;&|]\s*)&\s*[$(]")),
    ("dynamic_location_restore_forbidden", re.compile(r"(?i)(?:^|[;&|]\s*)popd(?:\.exe)?\b")),
    ("encoded_powershell_forbidden", re.compile(r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n]*\s-(?:e|en|enc|enco|encodedcommand)\b")),
)

_EXECUTABLE_PATH = re.compile(
    r'''(?ix)
    (?P<quoted>["'](?P<qpath>(?:[a-z]:[\\/]|\\\\|\.\.?[\\/])[^"'\r\n]+?\.(?:exe|com|bat|cmd|ps1|py|pyw|js|mjs|vbs|wsf|hta|jar))["'])
    |
    (?P<bare>(?:[a-z]:[\\/]|\\\\|\.\.?[\\/])[^\s;&|]+?\.(?:exe|com|bat|cmd|ps1|py|pyw|js|mjs|vbs|wsf|hta|jar))
    ''',
)

_INVOCATION_PREFIX = re.compile(
    r'''(?ix)(?:
        ^\s*|
        [;&|]\s*|
        &\s*|
        \.\s+|
        \bstart-process\b[^\r\n;&|]*|
        \b-file(?:path)?\s+|
        \b(?:python|py|node|java|cmd|powershell|pwsh|wscript|cscript|mshta)(?:\.exe)?\b[^\r\n;&|]*
    )$'''
)

_LOCATION_CHANGE = re.compile(
    r'''(?ix)(?:^|[;&|]\s*)
    (?:set-location(?:\s+-(?:literal)?path)?|push-location(?:\s+-(?:literal)?path)?|cd(?:\s+/d)?|chdir|pushd)
    \s+(?P<target>"[^"]+"|'[^']+'|[^\s;&|]+)
    ''',
)


def _effective_directory(command: str, end: int, workspace: Path) -> Path:
    current = workspace
    for match in _LOCATION_CHANGE.finditer(command, 0, end):
        raw = match.group("target").strip().strip("\"'")
        target = Path(raw).expanduser()
        current = (target if target.is_absolute() else current / target).resolve()
    return current


def _outside_execution_targets(command: str, workspace: str | Path) -> tuple[Path, ...]:
    """Find every explicit external executable/script target in command order.

    Checking only the first target allows a trusted invocation followed by an
    untrusted one in the same compound command. Neither a hash nor a prior
    accepted command may authorize a different target.
    """
    root = Path(workspace).expanduser().resolve()
    result: list[Path] = []
    seen: set[str] = set()
    for match in _EXECUTABLE_PATH.finditer(command):
        prefix = command[max(0, match.start() - 240):match.start()]
        if not _INVOCATION_PREFIX.search(prefix):
            continue
        raw = match.group("qpath") or match.group("bare") or ""
        current = _effective_directory(command, match.start(), root)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = current / candidate
        candidate = candidate.resolve()
        if not is_within(candidate, root):
            key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                result.append(candidate)
    return tuple(result)


def _outside_execution_target(command: str, workspace: str | Path) -> Path | None:
    """Legacy single-target helper retained for callers using this name."""
    found = _outside_execution_targets(command, workspace)
    return found[0] if found else None


def _execution_target_kind(path: Path) -> str:
    return ("SCRIPT" if path.suffix.casefold() in
            {".ps1", ".py", ".pyw", ".js", ".mjs", ".vbs", ".wsf", ".hta"}
            else "EXECUTABLE")


def inspect_command(command: str, *, workspace: str | Path | None = None) -> CommandAdmission:
    value = str(command or "")
    if not value.strip():
        return CommandAdmission(False, "empty_command", "command is empty")
    if "\x00" in value:
        return CommandAdmission(False, "nul_in_command", "command contains NUL")
    for code, pattern in _RULES:
        if pattern.search(value):
            return CommandAdmission(False, code, "use a typed, runtime-authorized operation")
    if workspace is not None:
        # All invoked external targets must be authorized independently.
        # Retain the legacy ACL OFF/workspace behavior for ordinary commands.
        accepted_code = ""
        for outside in _outside_execution_targets(value, workspace):
            authorized_workspace = False
            try:
                from .workspace_access import access_snapshot
                authorized_workspace = any(
                    is_within(outside, root)
                    for root in access_snapshot().get("writable_workspaces", ())
                )
            except Exception:
                authorized_workspace = False
            if authorized_workspace:
                accepted_code = "authorized_workspace_executable"
                continue
            trusted = False
            try:
                from .windows_security import (
                    authorized_workspace_roots,
                    is_trusted_executable,
                    load_security_profile,
                )
                profile = load_security_profile()
                authorized_workspace = any(
                    is_within(outside, root)
                    for root in authorized_workspace_roots(profile)
                )
                trusted = is_trusted_executable(outside, profile)
            except Exception:
                trusted = False
                authorized_workspace = False
            if authorized_workspace:
                accepted_code = "authorized_workspace_executable"
                continue
            if trusted:
                accepted_code = "trusted_external_executable"
                continue
            return CommandAdmission(
                False, "execution_target_outside_workspace", str(outside),
                True, _execution_target_kind(outside),
            )
        if accepted_code:
            return CommandAdmission(True, accepted_code)
    return CommandAdmission(True)


def inspect_native_command(command: str, *, workspace: str | Path) -> CommandAdmission:
    """Native Turbo executable admission: explicit outside targets need a pin.

    The existing generic Command Security hard bans run first. Unlike normal
    ACL OFF/workspace authorization, a path outside the Turbo Root must be
    explicitly pinned in operator-controlled security_profile.json and must
    match its SHA256. This check never authorizes filesystem writes; every
    actual command still runs through the separately attested OS broker.
    Bare PATH commands are not attested by this function; Planner must never
    interpret a software finding as an OS execution receipt.
    """
    first = inspect_command(command, workspace=workspace)
    if not first.allowed:
        return first
    targets = _outside_execution_targets(command, workspace)
    if not targets:
        return first
    try:
        from .windows_security import (
            load_security_profile, trusted_executable_records, is_trusted_executable,
        )
        profile = load_security_profile()
        rows = trusted_executable_records(profile)
        pinned = {str(Path(row["path"]).resolve()).casefold() for row in rows}
        for target in targets:
            if (str(target).casefold() not in pinned
                    or not is_trusted_executable(target, profile)):
                return CommandAdmission(
                    False, "execution_target_outside_workspace",
                    str(target), True, _execution_target_kind(target),
                )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return CommandAdmission(
            False, "execution_target_trust_unverified",
            "", False,
        )
    return CommandAdmission(True, "native_pinned_external_target")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_command_approval_manifest(command: str, *, workspace: str | Path) -> dict:
    root = Path(workspace).expanduser().resolve()
    admission = inspect_command(command, workspace=root)
    if admission.allowed or not admission.approval_eligible:
        raise CommandSecurityError(admission.code or "command_not_approval_eligible", admission.detail)
    target = Path(admission.detail).expanduser().resolve()
    if not target.is_file():
        raise CommandSecurityError("approval_target_not_file", str(target))
    payload = {
        "schema": "COMMAND_ALLOW_ONCE_V1",
        "approval_kind": "EXECUTION",
        "command": str(command),
        "command_sha256": hashlib.sha256(str(command).encode("utf-8")).hexdigest(),
        "cwd": str(root),
        "target": str(target),
        "executable_sha256": _file_sha256(target),
    }
    payload["manifest_digest"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def validate_command_approval_manifest(command: str, *, workspace: str | Path, manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise CommandSecurityError("approval_manifest_invalid", "not_object")
    expected = build_command_approval_manifest(command, workspace=workspace)
    for key, value in expected.items():
        if str(manifest.get(key, "")) != str(value):
            raise CommandSecurityError("approval_manifest_mismatch", key)
    return expected


def require_command_allowed(command: str, *, workspace: str | Path | None = None, approval_manifest: dict | None = None) -> None:
    admission = inspect_command(command, workspace=workspace)
    if admission.allowed:
        return
    if approval_manifest is not None and admission.approval_eligible and workspace is not None:
        validate_command_approval_manifest(command, workspace=workspace, manifest=approval_manifest)
        return
    raise CommandSecurityError(admission.code, admission.detail)


__all__ = [
    "CommandAdmission", "CommandSecurityError", "build_command_approval_manifest",
    "inspect_command", "inspect_native_command",
    "require_command_allowed", "validate_command_approval_manifest",
]
