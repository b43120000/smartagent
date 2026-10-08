#!/usr/bin/env python3
"""Append-only project history/provenance ledger.

Snapshot remains the authoritative point-in-time state.  This module records
why and how snapshots, sync transactions, and semantic-map revisions changed;
it never mutates or replaces project source state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, Mapping

from .process_file_lock import exclusive_process_lock


LEDGER_EVENT_SCHEMA = "PROJECT_LEDGER_EVENT_V1"
LEDGER_INDEX_SCHEMA = "PROJECT_LEDGER_INDEX_V1"
LEDGER_INSPECTION_SCHEMA = "PROJECT_LEDGER_INSPECTION_V1"
LEDGER_QUERY_SCHEMA = "PROJECT_LEDGER_QUERY_RESULT_V1"
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MAX_CHANGED_FILES = 10000
_MAX_NOTES_CHARS = 16000


def project_ledger_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".agents" / "project_context" / "ledger"


def project_ledger_path(workspace: str | Path) -> Path:
    return project_ledger_root(workspace) / "ledger.jsonl"


def project_ledger_index_path(workspace: str | Path) -> Path:
    return project_ledger_root(workspace) / "index.json"


def _project_id(root: Path) -> str:
    normalized = str(root).replace("\\", "/").casefold()
    return "PROJECT-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24].upper()


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_index(root: Path) -> dict:
    path = project_ledger_index_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict) or value.get("schema") != LEDGER_INDEX_SCHEMA:
        return {
            "schema": LEDGER_INDEX_SCHEMA,
            "project_id": _project_id(root),
            "workspace_root": str(root),
            "event_count": 0,
            "event_type_counts": {},
            "latest_event_id": "",
            "latest_snapshot_id": "",
            "latest_semantic_map_revision": "",
            "event_keys": {},
            "updated_at": 0.0,
        }
    return value


def _normalize_changed_files(values: Iterable[object] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise ValueError("project_ledger_changed_files_must_be_list")
    normalized: list[str] = []
    for value in values:
        path = str(value or "").strip().replace("\\", "/").lstrip("/")
        if not path or path == "." or ".." in Path(path).parts:
            raise ValueError(f"project_ledger_changed_path_invalid:{path}")
        normalized.append(path)
        if len(normalized) > _MAX_CHANGED_FILES:
            raise ValueError("project_ledger_changed_files_limit_exceeded")
    return sorted(set(normalized))


def create_ledger_entry(
    workspace: str | Path,
    *,
    event_type: str,
    snapshot_id: str = "",
    parent_snapshot_id: str = "",
    changed_files: Iterable[object] | None = None,
    semantic_map_revision: str = "",
    operation_result: str = "",
    actor: str = "runtime",
    source: str = "",
    notes: str = "",
    request_id: str = "",
    sync_id: str = "",
    details: Mapping[str, object] | None = None,
    event_key: str = "",
    timestamp: float | None = None,
) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_ledger_workspace_not_directory:{root}")
    kind = str(event_type or "").strip().lower()
    if not _EVENT_TYPE_RE.fullmatch(kind):
        raise ValueError(f"project_ledger_event_type_invalid:{kind}")
    note = str(notes or "").strip()
    if len(note) > _MAX_NOTES_CHARS:
        raise ValueError("project_ledger_notes_too_large")
    detail_value = dict(details or {})
    created_at = float(time.time() if timestamp is None else timestamp)
    identity_basis = {
        "project_id": _project_id(root),
        "event_type": kind,
        "snapshot_id": str(snapshot_id or ""),
        "parent_snapshot_id": str(parent_snapshot_id or ""),
        "semantic_map_revision": str(semantic_map_revision or ""),
        "request_id": str(request_id or ""),
        "sync_id": str(sync_id or ""),
        "event_key": str(event_key or ""),
        "timestamp": created_at,
    }
    event_id = "PLE-" + hashlib.sha256(
        json.dumps(identity_basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24].upper()
    return {
        "schema": LEDGER_EVENT_SCHEMA,
        "event_id": event_id,
        "timestamp": created_at,
        "project_id": _project_id(root),
        "workspace_root": str(root),
        "snapshot_id": str(snapshot_id or ""),
        "event_type": kind,
        "parent_snapshot_id": str(parent_snapshot_id or ""),
        "changed_files": _normalize_changed_files(changed_files),
        "semantic_map_revision": str(semantic_map_revision or ""),
        "operation_result": str(operation_result or "").strip(),
        "actor": str(actor or "runtime").strip()[:256],
        "source": str(source or "").strip()[:512],
        "notes": note,
        "request_id": str(request_id or "").strip(),
        "sync_id": str(sync_id or "").strip(),
        "details": detail_value,
        "event_key": str(event_key or "").strip(),
    }


def append_project_event(workspace: str | Path, event_type: str, **fields) -> dict:
    """Append one event and atomically refresh the compact index.

    ``event_key`` is an optional idempotency key.  Replaying the same logical
    lifecycle event returns the original entry instead of duplicating history.
    """
    root = Path(workspace).expanduser().resolve()
    entry = create_ledger_entry(root, event_type=event_type, **fields)
    ledger_root = project_ledger_root(root)
    ledger_root.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_root / "ledger.lock"
    with exclusive_process_lock(lock_path, timeout_sec=10.0, label="project ledger"):
        index = _load_index(root)
        event_key = str(entry.get("event_key", "") or "")
        existing_id = str(dict(index.get("event_keys", {}) or {}).get(event_key, "")) if event_key else ""
        if existing_id:
            for item in reversed(_read_events(root)):
                if item.get("event_id") == existing_id:
                    return {**item, "deduplicated": True}
        path = project_ledger_path(root)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        counts = dict(index.get("event_type_counts", {}) or {})
        normalized_event_type = str(entry["event_type"])
        counts[normalized_event_type] = int(counts.get(normalized_event_type, 0) or 0) + 1
        keys = dict(index.get("event_keys", {}) or {})
        if event_key:
            keys[event_key] = entry["event_id"]
        index.update({
            "event_count": int(index.get("event_count", 0) or 0) + 1,
            "event_type_counts": counts,
            "latest_event_id": entry["event_id"],
            "latest_snapshot_id": entry["snapshot_id"] or str(index.get("latest_snapshot_id", "") or ""),
            "latest_semantic_map_revision": (
                entry["semantic_map_revision"]
                or str(index.get("latest_semantic_map_revision", "") or "")
            ),
            "event_keys": keys,
            "updated_at": entry["timestamp"],
        })
        _atomic_json(project_ledger_index_path(root), index)
    return entry


def _read_events(workspace: str | Path) -> list[dict]:
    path = project_ledger_path(workspace)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return []
    events: list[dict] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"project_ledger_corrupt_line:{line_number}") from exc
        if not isinstance(value, dict) or value.get("schema") != LEDGER_EVENT_SCHEMA:
            raise ValueError(f"project_ledger_event_invalid:{line_number}")
        events.append(value)
    return events


def query_project_history(
    workspace: str | Path,
    *,
    event_types: Iterable[str] | None = None,
    snapshot_id: str = "",
    path_contains: str = "",
    since: float | None = None,
    limit: int = 50,
) -> dict:
    root = Path(workspace).expanduser().resolve()
    kinds = {str(value or "").strip().lower() for value in (event_types or []) if str(value or "").strip()}
    needle = str(path_contains or "").strip().replace("\\", "/").casefold()
    bounded_limit = max(1, min(int(limit), 500))
    matches = []
    for event in reversed(_read_events(root)):
        if kinds and str(event.get("event_type", "")) not in kinds:
            continue
        if snapshot_id and str(event.get("snapshot_id", "")) != str(snapshot_id):
            continue
        if since is not None and float(event.get("timestamp", 0.0) or 0.0) < float(since):
            continue
        if needle and not any(needle in str(path).casefold() for path in event.get("changed_files", [])):
            continue
        matches.append(event)
        if len(matches) >= bounded_limit:
            break
    return {
        "schema": LEDGER_QUERY_SCHEMA,
        "project_id": _project_id(root),
        "workspace_root": str(root),
        "match_count": len(matches),
        "events": matches,
    }


def inspect_project_ledger(workspace: str | Path, *, limit: int = 20) -> dict:
    root = Path(workspace).expanduser().resolve()
    index = _load_index(root)
    history = query_project_history(root, limit=limit)
    return {
        "schema": LEDGER_INSPECTION_SCHEMA,
        "status": "READY" if int(index.get("event_count", 0) or 0) else "EMPTY",
        "project_id": index["project_id"],
        "workspace_root": str(root),
        "event_count": int(index.get("event_count", 0) or 0),
        "event_type_counts": dict(index.get("event_type_counts", {}) or {}),
        "latest_event_id": str(index.get("latest_event_id", "") or ""),
        "latest_snapshot_id": str(index.get("latest_snapshot_id", "") or ""),
        "latest_semantic_map_revision": str(index.get("latest_semantic_map_revision", "") or ""),
        "ledger_path": str(project_ledger_path(root)),
        "index_path": str(project_ledger_index_path(root)),
        "recent_events": history["events"],
    }


def changed_files_between(parent: Mapping[str, object] | None, current: Mapping[str, object] | None) -> list[str]:
    old = {
        str(item.get("path", "")): str(item.get("sha256", ""))
        for item in list((parent or {}).get("files", []) or [])
        if isinstance(item, Mapping) and str(item.get("path", ""))
    }
    new = {
        str(item.get("path", "")): str(item.get("sha256", ""))
        for item in list((current or {}).get("files", []) or [])
        if isinstance(item, Mapping) and str(item.get("path", ""))
    }
    return sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))


__all__ = [
    "LEDGER_EVENT_SCHEMA", "append_project_event", "changed_files_between",
    "create_ledger_entry", "inspect_project_ledger", "project_ledger_index_path",
    "project_ledger_path", "project_ledger_root", "query_project_history",
]
