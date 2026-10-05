#!/usr/bin/env python3
"""Canonical filesystem authorization shared by every agent interface."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable


class PathSecurityError(ValueError):
    """Raised when a local path cannot be authorized safely."""

    def __init__(self, code: str, path: str = ""):
        self.code = str(code)
        self.path = str(path)
        super().__init__(f"{self.code}:{self.path}" if self.path else self.code)


_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_GLOB_RE = re.compile(r"[*?\[\]]")


def _text(raw_path: str | os.PathLike[str]) -> str:
    value = os.fspath(raw_path).strip()
    if not value:
        raise PathSecurityError("empty_path")
    if "\x00" in value:
        raise PathSecurityError("nul_in_path", value)
    return value


def _reject_windows_special_path(value: str) -> None:
    normalized = value.replace("/", "\\")
    lowered = normalized.lower()
    if lowered.startswith(_DEVICE_PREFIXES):
        raise PathSecurityError("device_path_forbidden", value)
    if normalized.startswith("\\\\"):
        raise PathSecurityError("unc_path_forbidden", value)
    drive, tail = os.path.splitdrive(normalized)
    if ":" in tail:
        raise PathSecurityError("alternate_data_stream_forbidden", value)


def canonicalize_path(
    raw_path: str | os.PathLike[str],
    *,
    base: str | os.PathLike[str] | None = None,
    require_absolute: bool = False,
    forbid_glob: bool = False,
) -> Path:
    value = _text(raw_path)
    _reject_windows_special_path(value)
    if forbid_glob and _GLOB_RE.search(value):
        raise PathSecurityError("glob_path_forbidden", value)
    candidate = Path(value).expanduser()
    if ".." in candidate.parts:
        raise PathSecurityError("parent_traversal_forbidden", value)
    if require_absolute and not candidate.is_absolute():
        raise PathSecurityError("absolute_path_required", value)
    if not candidate.is_absolute():
        if base is None:
            raise PathSecurityError("relative_path_without_base", value)
        candidate = Path(base).expanduser() / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathSecurityError("path_resolution_failed", value) from exc


def is_within(path: Path, root: Path, *, allow_root: bool = True) -> bool:
    candidate = os.path.normcase(str(path))
    boundary = os.path.normcase(str(root))
    try:
        common = os.path.commonpath([candidate, boundary])
    except ValueError:
        return False
    if common != boundary:
        return False
    return allow_root or candidate != boundary


def is_reparse_path(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attrs = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attrs & reparse_flag)


def reject_reparse_components(path: Path, root: Path) -> None:
    if not is_within(path, root):
        raise PathSecurityError("path_outside_authorized_root", str(path))
    current = root
    if is_reparse_path(current):
        raise PathSecurityError("reparse_or_symlink_forbidden", str(current))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PathSecurityError("path_outside_authorized_root", str(path)) from exc
    for part in relative.parts:
        current = current / part
        if current.exists() and is_reparse_path(current):
            raise PathSecurityError("reparse_or_symlink_forbidden", str(current))


def authorize_path(
    raw_path: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
    *,
    base: str | os.PathLike[str] | None = None,
    require_absolute: bool = False,
    allow_root: bool = True,
    forbid_glob: bool = False,
    reject_reparse: bool = True,
) -> Path:
    candidate = canonicalize_path(
        raw_path,
        base=base,
        require_absolute=require_absolute,
        forbid_glob=forbid_glob,
    )
    normalized_roots = [canonicalize_path(root, require_absolute=True) for root in roots]
    for root in normalized_roots:
        if not is_within(candidate, root, allow_root=allow_root):
            continue
        if reject_reparse:
            reject_reparse_components(candidate, root)
        return candidate
    raise PathSecurityError("path_outside_authorized_roots", str(candidate))


def is_filesystem_root(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved == Path(resolved.anchor)


__all__ = [
    "PathSecurityError", "authorize_path", "canonicalize_path",
    "is_filesystem_root", "is_reparse_path", "is_within",
    "reject_reparse_components",
]
