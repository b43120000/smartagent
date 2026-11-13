"""Git54: task-bound, lossless Runtime evidence source of truth.

Planner receives bounded previews and can request arbitrary slices by evidence
reference. The immutable producer result, rather than a Blackboard projection,
is the authority. This store is never an execution or ACL authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Mapping

SCHEMA = "SMARTAGENT_RUNTIME_EVIDENCE_STORE_V1"
REF_PATTERN = re.compile(r"^MIN-EV-[a-f0-9]{24}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
SECTIONS = frozenset({"raw_result", "receipt", "stdout", "stderr", "metadata"})
DEFAULT_SLICE_CHARS = 1800
MAX_SLICE_CHARS = 2400


class RuntimeEvidenceError(ValueError):
    """A missing, unbound, damaged or unavailable trusted evidence record."""


def _identifier(value: object, *, ref: bool = False) -> str:
    value = str(value or "")
    pattern = REF_PATTERN if ref else ID_PATTERN
    if not pattern.fullmatch(value):
        raise RuntimeEvidenceError("EVIDENCE_IDENTIFIER_INVALID")
    return value


def _root(root: str | Path) -> Path:
    path = Path(root).absolute()
    if path.name.casefold() == "source" and (path.parent / "collect.bat").is_file():
        path = path.parent
    # Never follow a reparse/junction outside the Runtime-owned storage root.
    for component in [path, path / "localdata", path / "localdata" / "runtime",
                      path / "localdata" / "runtime" / "evidence_store"]:
        if component.exists():
            attributes = getattr(component.lstat(), "st_file_attributes", 0)
            if component.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeEvidenceError("EVIDENCE_STORAGE_REPARSE_FORBIDDEN")
    return path / "localdata" / "runtime" / "evidence_store"


def _record_path(root, task_id, evidence_ref) -> Path:
    return _root(root) / _identifier(task_id) / (_identifier(evidence_ref, ref=True) + ".json")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def persist_runtime_evidence(
    root: str | Path, *, task_id: str, request_id: str, task_epoch: str,
    action_id: str, evidence_ref: str, raw_result: str, receipt: Mapping,
) -> dict:
    """Append-only, read-back-verified copy. Existing mismatched record fails."""
    task_id = _identifier(task_id)
    request_id = _identifier(request_id)
    task_epoch = _identifier(task_epoch)
    action_id = _identifier(action_id)
    evidence_ref = _identifier(evidence_ref, ref=True)
    if not isinstance(raw_result, str) or not isinstance(receipt, Mapping):
        raise RuntimeEvidenceError("EVIDENCE_PAYLOAD_INVALID")
    raw_hash = _hash(raw_result)
    data = dict(receipt.get("data") or {}) if isinstance(receipt.get("data"), Mapping) else {}
    record = {
        "schema": SCHEMA, "task_id": task_id, "request_id": request_id,
        "task_epoch": task_epoch, "action_id": action_id,
        "evidence_ref": evidence_ref,
        "raw_result_sha256": raw_hash, "raw_result_chars": len(raw_result),
        "raw_result": raw_result, "receipt": dict(receipt),
        "output_capture": {
            "stdout_bytes": data.get("stdout_bytes"),
            "stderr_bytes": data.get("stderr_bytes"),
            "stdout_truncated": data.get("stdout_truncated"),
            "stderr_truncated": data.get("stderr_truncated"),
            "output_complete": data.get("output_complete"),
        },
    }
    path = _record_path(root, task_id, evidence_ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (
        getattr(path.parent.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeEvidenceError("EVIDENCE_STORAGE_REPARSE_FORBIDDEN")
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        # A retry may observe the same immutable record, never overwrite it.
        existing = _load_record(path)
        if (existing.get("raw_result_sha256") != raw_hash
                or existing.get("action_id") != action_id
                or existing.get("request_id") != request_id
                or existing.get("task_epoch") != task_epoch):
            raise RuntimeEvidenceError("EVIDENCE_IMMUTABLE_CONFLICT")
    existing = _load_record(path)
    if (existing.get("raw_result_sha256") != raw_hash
            or existing.get("raw_result") != raw_result):
        raise RuntimeEvidenceError("EVIDENCE_WRITE_READBACK_FAILED")
    return {
        "evidence_ref": evidence_ref,
        "store_status": "PERSISTED_AND_READBACK_VERIFIED",
        "source_sha256": raw_hash,
        "source_chars": len(raw_result),
        "sections": sorted(SECTIONS),
        "task_bound": True,
    }


def _load_record(path: Path) -> dict:
    if path.is_symlink() or (
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeEvidenceError("EVIDENCE_STORAGE_REPARSE_FORBIDDEN")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeEvidenceError("EVIDENCE_NOT_FOUND_OR_DAMAGED") from exc
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA
            or not isinstance(record.get("raw_result"), str)
            or _hash(record["raw_result"]) != record.get("raw_result_sha256")):
        raise RuntimeEvidenceError("EVIDENCE_DIGEST_OR_SCHEMA_INVALID")
    return record


def read_runtime_evidence(
    root: str | Path, *, task_id: str, request_id: str, task_epoch: str,
    evidence_ref: str, section: str = "raw_result",
    offset: int = 0, max_chars: int = DEFAULT_SLICE_CHARS,
) -> dict:
    """Read one same-task, same-request, immutable source slice; no path input."""
    if section not in SECTIONS or type(offset) is not int or offset < 0:
        raise RuntimeEvidenceError("EVIDENCE_READ_ARGUMENT_INVALID")
    if type(max_chars) is not int or not (1 <= max_chars <= MAX_SLICE_CHARS):
        raise RuntimeEvidenceError("EVIDENCE_SLICE_SIZE_INVALID")
    path = _record_path(root, task_id, evidence_ref)
    record = _load_record(path)
    if (record.get("task_id") != _identifier(task_id)
            or record.get("request_id") != _identifier(request_id)
            or record.get("task_epoch") != _identifier(task_epoch)
            or record.get("evidence_ref") != evidence_ref):
        raise RuntimeEvidenceError("EVIDENCE_REQUEST_BINDING_MISMATCH")
    receipt = record.get("receipt")
    if not isinstance(receipt, Mapping):
        raise RuntimeEvidenceError("EVIDENCE_RECEIPT_INVALID")
    data = receipt.get("data") if isinstance(receipt.get("data"), Mapping) else {}
    if section in {"stdout", "stderr"}:
        content = data.get(section, "")
        if not isinstance(content, str):
            raise RuntimeEvidenceError("EVIDENCE_STREAM_NOT_CAPTURED")
        # Legacy producer with no structured stream: full raw transcript is
        # still available by section=raw_result (never pretend stream is empty).
        stream_known = section in data
        if not stream_known:
            raise RuntimeEvidenceError("EVIDENCE_STREAM_USE_RAW_RESULT")
    elif section == "receipt":
        content = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), default=str)
    elif section == "metadata":
        content = json.dumps({
            key: value for key, value in record.items()
            if key not in {"raw_result", "receipt"}
        }, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        content = record["raw_result"]
    start = min(offset, len(content))
    stop = min(start + max_chars, len(content))
    return {
        "schema": "SMARTAGENT_RUNTIME_EVIDENCE_READ_V1",
        "evidence_ref": evidence_ref, "section": section,
        "content": content[start:stop], "offset": start,
        "next_offset": stop, "total_chars": len(content),
        "complete": stop == len(content),
        "source_sha256": _hash(content),
        "task_bound": True, "read_only": True,
        "source_result_sha256": record["raw_result_sha256"],
        "capture_truncated": bool(
            (record.get("output_capture") or {}).get(section + "_truncated")
        ) if section in {"stdout", "stderr"} else False,
    }


__all__ = [
    "RuntimeEvidenceError", "persist_runtime_evidence", "read_runtime_evidence",
    "DEFAULT_SLICE_CHARS", "MAX_SLICE_CHARS", "SCHEMA",
]
