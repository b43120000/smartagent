"""Bounded Runtime metadata for source location leads across task epochs.

The registry is NOT a filesystem observation of source, nor a scope grant.
Only a successfully executed current read of all required top-level item names
may add a historical lead. Previous leads always require fresh probes.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from .process_file_lock import exclusive_process_lock
from .tool_result_contract import decode_result

SCHEMA = "SMARTAGENT_SOURCE_HISTORY_V1"


def _target_key(path: object) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(ntpath.normcase(ntpath.normpath(raw)).encode("utf-8")).hexdigest()


def _location(root: Path) -> Path:
    # Runtime-owned state under the SmartAgent installation, never under E:
    # source/destination project. This is not a user-project file write.
    return Path(root).expanduser().resolve() / "localdata" / "runtime" / "source_history_v1.json"


def _load(path: Path) -> dict:
    try:
        if path.stat().st_size > 1024 * 1024:
            return {"schema": SCHEMA, "records": []}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, UnicodeError):
        return {"schema": SCHEMA, "records": []}
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "records": []}
    return {"schema": SCHEMA, "records": list(data.get("records") or [])[:48]}


def load_source_history(root: Path, target: str) -> dict:
    """Read only the exact target's prior leads; never claim present files."""
    key = _target_key(target)
    if not key:
        return {"schema": SCHEMA, "candidates": []}
    rows = []
    for row in _load(_location(Path(root))).get("records", []):
        if (isinstance(row, Mapping) and row.get("target_sha256") == key
                and isinstance(row.get("path"), str)
                and isinstance(row.get("provenance_ref"), str)):
            rows.append({
                "path": row["path"], "origin": "PREVIOUS_TASK_RECEIPT",
                "provenance_ref": row["provenance_ref"],
            })
    return {"schema": SCHEMA, "candidates": rows[:16]}


def record_source_history(
    root: Path, *, target: str, source: str, expected_entries: list[str],
    actual_tool: str, result: str, action_id: str, task_id: str,
) -> bool:
    """Save a historical location only after the *current* list succeeded.

    Does not assert payload hashes. Stale hints will still be reported as
    UNKNOWN in every later request.
    """
    target_hash = _target_key(target)
    if (not target_hash or not source or not expected_entries
            or ntpath.normcase(ntpath.normpath(target)) ==
               ntpath.normcase(ntpath.normpath(source))
            or actual_tool != "list_directory" or not action_id or not task_id):
        return False
    receipt = decode_result(result)
    if (not isinstance(receipt, Mapping)
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("executed") is not True
            or receipt.get("tool") != "list_directory"
            or receipt.get("reason") != "DIRECTORY_LISTED"
            or ntpath.normcase(ntpath.normpath(str(receipt.get("requested_path", "")))) !=
               ntpath.normcase(ntpath.normpath(source))):
        return False
    entries = (receipt.get("data") or {}).get("entries")
    if not isinstance(entries, list) or len(entries) > 2048:
        return False
    observed: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            return False
        name = item["name"].casefold()
        if name in observed or not name or name in (".", ".."):
            return False
        observed[name] = str(item.get("kind", ""))
    for name in expected_entries:
        expected = "FILE" if "." in name else "DIRECTORY"
        if observed.get(name.casefold()) != expected:
            return False
    destination = _location(Path(root))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_process_lock(destination.with_suffix(".lock"), timeout_sec=2.0,
                                    label="source_candidate_history"):
            doc = _load(destination)
            rows = [row for row in doc["records"] if isinstance(row, Mapping)
                    and not (row.get("target_sha256") == target_hash
                             and ntpath.normcase(str(row.get("path", ""))) ==
                                 ntpath.normcase(source))]
            rows.append({
                "target_sha256": target_hash,
                "path": source,
                "provenance_ref": str(task_id)[:100] + "/" + str(action_id)[:100],
                "integrity_level_at_recording": "STRUCTURE_VERIFIED_ONLY",
            })
            doc = {"schema": SCHEMA, "records": rows[-48:]}
            tmp = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True),
                               encoding="utf-8")
                tmp.replace(destination)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
    except (OSError, RuntimeError, ValueError):
        return False
    return True


__all__ = ["SCHEMA", "load_source_history", "record_source_history"]
