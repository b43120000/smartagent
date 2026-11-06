"""Stable, bounded Goal Identity for historical *source hints*, not authority.

Matching is by exact normalized Windows destination and required top-level
entries. Names such as 'Case D' are never used as a project identifier.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "SMARTAGENT_SOURCE_GOAL_IDENTITY_V1"


def absolute_windows_path(path: object) -> bool:
    raw = str(path or "").strip()
    return (3 <= len(raw) <= 1024
            and bool(re.match(r"^[A-Za-z]:[\\/]", raw))
            and "\x00" not in raw
            and not any(part in (".", "..") for part in re.split(r"[\\/]", raw[3:])))


def normalized_path(path: object) -> str:
    if not absolute_windows_path(path):
        return ""
    return ntpath.normcase(ntpath.normpath(str(path).strip()))


def goal_identity(contract: Mapping | None) -> dict:
    value = contract if isinstance(contract, Mapping) else {}
    target = normalized_path(value.get("requested_target"))
    entries = value.get("required_top_level_entries") or []
    if not isinstance(entries, (tuple, list)):
        entries = []
    required = []
    for item in entries[:24]:
        name = str(item or "").strip()
        if (not name or len(name) > 180 or name in {".", ".."}
                or any(ch in name for ch in ("/", "\\", ":", "\x00"))):
            return {"schema": SCHEMA, "status": "INVALID", "digest": ""}
        required.append(name.casefold())
    required = sorted(set(required))
    if not target or not required:
        return {"schema": SCHEMA, "status": "NOT_INDEXABLE", "digest": ""}
    canonical = json.dumps(
        {"target": target, "required": required}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"),
    )
    return {"schema": SCHEMA, "status": "READY",
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target": target, "required_entries": required}


def context_identity(owner: str, instance: str, workspace: str) -> str:
    if not owner or not instance or not workspace:
        return ""
    canonical_workspace = str(Path(workspace).expanduser().resolve()).casefold()
    payload = json.dumps([owner, instance, canonical_workspace],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["SCHEMA", "absolute_windows_path", "normalized_path",
           "goal_identity", "context_identity"]
