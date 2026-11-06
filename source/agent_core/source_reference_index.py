"""Agent-instance Source Reference Index: location leads only.

No source file access or project writes. All references are segregated by
(owner, Agent instance, workspace, exact goal identity). Previous references
never become current filesystem facts or authorization.
"""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from .paths import runtime_root
from .process_file_lock import exclusive_process_lock
from .source_goal_identity import context_identity, normalized_path

SCHEMA = "SMARTAGENT_SOURCE_REFERENCE_INDEX_V1"
_MAX_BYTES = 1024 * 1024
_MAX_RECORDS = 160
_ALLOWED_ORIGINS = {
    "HISTORICAL_USER_SOURCE_LABEL",
    "HISTORICAL_RUNTIME_LIST_RECEIPT",
    "PREVIOUS_TASK_RECEIPT",
    "PROJECT_LEDGER",
}
_LEVELS = {"E1", "E2"}


def index_path(agent_root: str | Path) -> Path:
    return runtime_root(agent_root) / "source_reference_index_v1.json"


def _read(path: Path) -> dict:
    try:
        if not path.exists():
            return {"schema": SCHEMA, "status": "INDEX_NOT_FOUND", "records": []}
        if path.stat().st_size > _MAX_BYTES:
            return {"schema": SCHEMA, "status": "INDEX_INVALID", "records": []}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"schema": SCHEMA, "status": "INDEX_INVALID", "records": []}
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("records"), list):
        return {"schema": SCHEMA, "status": "INDEX_INVALID", "records": []}
    if len(raw["records"]) > _MAX_RECORDS or not all(isinstance(item, dict) for item in raw["records"]):
        return {"schema": SCHEMA, "status": "INDEX_INVALID", "records": []}
    return {"schema": SCHEMA, "status": "INDEX_READY" if raw["records"] else "INDEX_EMPTY",
            "records": raw["records"]}


def read_references(agent_root: str | Path, *, owner: str, instance: str,
                    workspace: str, goal_digest: str) -> dict:
    ctx = context_identity(owner, instance, workspace)
    if not ctx or len(str(goal_digest)) != 64:
        return {"schema": SCHEMA, "index_status": "CONTEXT_UNAVAILABLE", "candidates": []}
    doc = _read(index_path(agent_root))
    leads = []
    for row in doc["records"]:
        if (row.get("context_digest") != ctx or row.get("goal_digest") != goal_digest
                or row.get("origin") not in _ALLOWED_ORIGINS
                or row.get("evidence_level") not in _LEVELS
                or not normalized_path(row.get("path"))
                or not row.get("provenance_ref")):
            continue
        leads.append({
            "path": row["path"],
            "origin": row["origin"],
            "provenance_ref": row["provenance_ref"],
            "evidence_level": row["evidence_level"],
            "current_existence": "UNKNOWN",
            "current_integrity": "UNKNOWN",
            "read_authorization": "NOT_EVALUATED",
        })
    return {"schema": SCHEMA, "index_status": doc["status"],
            "candidates": leads[-16:],
            "index_hits": len(leads[-16:]),
            "history_is_current_evidence": False,
            "grants_authorization": False}


def publish_reference(agent_root: str | Path, *, owner: str, instance: str,
                      workspace: str, goal_digest: str, path: str,
                      provenance_ref: str, origin: str, evidence_level: str) -> bool:
    ctx = context_identity(owner, instance, workspace)
    path_key = normalized_path(path)
    if (not ctx or len(str(goal_digest)) != 64
            or not path_key or not provenance_ref
            or origin not in _ALLOWED_ORIGINS or evidence_level not in _LEVELS):
        return False
    record = {
        "context_digest": ctx,
        "goal_digest": goal_digest,
        "path": str(path)[:1024],
        "origin": origin,
        "evidence_level": evidence_level,
        "provenance_ref": str(provenance_ref)[:160],
    }
    target = index_path(agent_root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_process_lock(
                target.with_suffix(".lock"), timeout_sec=2.0,
                label="source_reference_index"):
            doc = _read(target)
            if doc["status"] == "INDEX_INVALID":
                return False
            for old in doc["records"]:
                if (old.get("context_digest") == ctx
                        and old.get("goal_digest") == goal_digest
                        and normalized_path(old.get("path")) == path_key
                        and old.get("evidence_level") == "E2"
                        and evidence_level == "E1"):
                    # An older structural Runtime receipt stays stronger
                    # than a newly rediscovered original-request source label.
                    return True
            records = [
                row for row in doc["records"]
                if not (row.get("context_digest") == ctx
                        and row.get("goal_digest") == goal_digest
                        and normalized_path(row.get("path")) == path_key)
            ]
            records.append(record)
            payload = json.dumps({"schema": SCHEMA, "records": records[-_MAX_RECORDS:]},
                                 ensure_ascii=False, sort_keys=True)
            if len(payload.encode("utf-8")) > _MAX_BYTES:
                return False
            temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_text(payload, encoding="utf-8")
                os.replace(temp, target)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
    except (OSError, ValueError, RuntimeError):
        return False
    return True


__all__ = ["SCHEMA", "index_path", "read_references", "publish_reference"]
