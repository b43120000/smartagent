"""Runtime-evidence-owned registry of additional software environments.

This is an advisory catalog, NOT an ACL, executable allowlist, or authority to
download. A model cannot write entries by declaring success. Only an actual
admitted, successful version probe can publish a candidate. Recorded paths must
be re-probed under the next task's current security context before use.
"""
from __future__ import annotations

import json
import os
import re
import ntpath
import time
import shlex
from pathlib import Path
from collections.abc import Mapping

from .paths import install_root
from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_SOFTWARE_REGISTRY_V1"
_MAX = 64


def software_registry_path(root: str | Path | None = None) -> Path:
    return install_root(root) / "localdata" / "metadata" / "software_registry.json"


def read_software_registry(root: str | Path | None = None) -> dict:
    file = software_registry_path(root)
    if not file.is_file():
        return {"schema": SCHEMA, "revision": 0, "records": {}}
    value = read_json_retry(file)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("software_registry_schema_invalid")
    if not isinstance(value.get("records"), dict):
        raise ValueError("software_registry_records_invalid")
    return value


def _probe_executable(command: str) -> str:
    """Only a single explicit executable --version command, no shell grammar."""
    text = str(command or "").strip()
    if len(text) > 2000 or any(c in text for c in "\r\n;|&<>$" + chr(96)):
        return ""
    # Posix-false shlex preserves Windows backslashes and quote boundaries.
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        return ""
    if len(parts) != 2 or parts[1].casefold() not in {"--version", "-version", "-v"}:
        return ""
    raw = parts[0].strip("'\"")
    if not (re.match(r"(?i)^[a-z]:[\\/]", raw) or Path(raw).is_absolute()):
        return ""
    if not raw.lower().endswith((".exe", ".com")):
        return ""
    return raw


def register_verified_software_probe(action: Mapping, receipt: Mapping,
                                     evidence_ref: str, *,
                                     root: str | Path | None = None) -> dict | None:
    """Called by Executor after a trusted ToolResult is durably recorded."""
    if (action.get("tool") != "run_command"
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("executed") is not True
            or not str(evidence_ref).startswith("MIN-EV-")):
        return None
    data = receipt.get("data")
    if (not isinstance(data, Mapping)
            or data.get("process_state") != "EXITED"
            or type(data.get("exit_code")) is not int or data["exit_code"] != 0
            or data.get("timed_out") is not False):
        return None
    exe = _probe_executable(action.get("command") or "")
    if not exe:
        return None
    target = Path(exe).expanduser()
    try:
        target = target.resolve(strict=True)
        if not target.is_file() or target.is_symlink():
            return None
    except (ValueError, OSError, RuntimeError):
        return None
    import hashlib
    digest = hashlib.sha256(str(target).casefold().encode("utf-8")).hexdigest()
    key = digest[:32]
    row = {
        "name": ntpath.basename(str(target))[:100],
        "path": str(target)[:1024],
        "status": "VERIFIED_AT_LAST_PROBE",
        "last_evidence_ref": str(evidence_ref)[:160],
        "last_probe_at": int(time.time()),
        "probe_stdout_sha256": str(
            (data.get("stdout_or_local_ref") or {}).get("sha256") or ""
        )[:64] if isinstance(data.get("stdout_or_local_ref"), Mapping) else "",
        "reprobe_required": True,
        "source": "ADMITTED_EXECUTOR_VERSION_PROBE",
        "execution_authorized": False,
    }
    file = software_registry_path(root)
    lock = file.with_name("software_registry.lock")
    with exclusive_process_lock(lock, label="software-registry"):
        existing = read_software_registry(root)
        records = dict(existing["records"])
        records[key] = row
        if len(records) > _MAX:
            for discard in sorted(records, key=lambda k: records[k].get("last_probe_at", 0))[:-_MAX]:
                records.pop(discard, None)
        payload = {"schema": SCHEMA,
                   "revision": int(existing.get("revision", 0) or 0) + 1,
                   "records": records}
        file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(file, payload)
        return row


def software_registry_snapshot(root: str | Path | None = None) -> dict:
    """Bounded advisory hints; no binary is executed simply because of registry."""
    state = read_software_registry(root)
    result = []
    for value in list(state["records"].values())[:_MAX]:
        if not isinstance(value, Mapping):
            continue
        path = str(value.get("path") or "")
        if not path or len(path) > 1024:
            continue
        try:
            file = Path(path)
            present = file.is_file() and not file.is_symlink()
        except (ValueError, OSError):
            present = False
        result.append({
            "name": str(value.get("name") or "")[:100],
            "path_hint": path,
            "availability": "RECORDED_REPROBE_REQUIRED" if present else "MISSING",
            "source": str(value.get("source") or "")[:80],
            "last_evidence_ref": str(value.get("last_evidence_ref") or "")[:160],
            "execution_authorized": False,
        })
    return {"schema": SCHEMA, "revision": state.get("revision", 0),
            "items": result[:24], "no_authorization_granted": True}


__all__ = [
    "SCHEMA", "software_registry_path", "read_software_registry",
    "register_verified_software_probe", "software_registry_snapshot",
]
