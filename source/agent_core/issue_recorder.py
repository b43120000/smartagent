#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 11.1 append-only issue recorder for record-only self repair mode."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .paths import self_repair_root

DEFAULT_EVENT_STORE = self_repair_root() / "issues.jsonl"
DEFAULT_ISSUE_DIR = self_repair_root() / "issues"
ISSUE_STORE_VERSION = 1

_WRITE_LOCK = threading.RLock()
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|cookie|session)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|auth|signature)=)[^&#\s]+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
)


def redact_sensitive(value: Any, *, limit: int = 12000) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[: max(0, int(limit))]


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value, limit=3000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive(value, limit=3000)


def classify_issue(exc: BaseException, context: dict[str, Any] | None = None) -> str:
    context = context or {}
    text = " ".join((type(exc).__name__, str(exc), str(context.get("error_code", "")))).lower()
    if any(token in text for token in ("rate_limit", "rate limit", "too many requests", "http 429", "timeout", "connectionerror", "network")):
        return "TRANSIENT_EXTERNAL"
    if any(token in text for token in ("login_required", "access_denied", "permission", "unauthorized", "forbidden")):
        return "EXPECTED_POLICY_STOP"
    if str(context.get("origin", "")).upper() == "USER_PROJECT":
        return "USER_PROJECT_FAILURE"
    if isinstance(exc, (OSError, ImportError)):
        return "ENVIRONMENT_DEFECT"
    module_path = str(context.get("source_file", "") or "").replace("\\", "/").lower()
    trace_paths = [str(frame.filename).replace("\\", "/").lower() for frame in traceback.extract_tb(exc.__traceback__)]
    if (
        "/agent_core/" in module_path or module_path.endswith("/smart_agent.py")
        or any("/agent_core/" in path or path.endswith("/smart_agent.py") for path in trace_paths)
    ):
        return "LOCALAGENT_DEFECT"
    return "SECURITY_OR_UNKNOWN"


def _exception_location(exc: BaseException) -> tuple[str, int, str]:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not frames:
        return "", 0, ""
    frame = frames[-1]
    return str(frame.filename), int(frame.lineno), str(frame.name)


def issue_fingerprint(exc: BaseException, context: dict[str, Any] | None = None) -> str:
    context = context or {}
    source_file, source_line, source_symbol = _exception_location(exc)
    stable = {
        "type": type(exc).__name__,
        "message": redact_sensitive(str(exc), limit=1000),
        "source_file": Path(source_file).name if source_file else str(context.get("source_file", "")),
        "source_line": source_line or int(context.get("source_line", 0) or 0),
        "source_symbol": source_symbol or str(context.get("source_symbol", "")),
    }
    if not source_file:
        stable.update({
            "stage": str(context.get("stage", "")),
            "error_code": str(context.get("error_code", "")),
        })
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(AGENT_PROJECT_ROOT),
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


class IssueRecorder:
    """Persist sanitized issue events and one human-readable document per fingerprint."""

    def __init__(self, event_store: str | Path = DEFAULT_EVENT_STORE, issue_dir: str | Path = DEFAULT_ISSUE_DIR):
        self.event_store = Path(event_store)
        self.issue_dir = Path(issue_dir)

    def _existing(self, fingerprint: str) -> tuple[str, int]:
        issue_id, count = "", 0
        try:
            for line in self.event_store.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("fingerprint") == fingerprint:
                    issue_id = str(event.get("issue_id", "") or issue_id)
                    count += 1
        except Exception:
            pass
        return issue_id, count

    def record(self, exc: BaseException, **context: Any) -> dict[str, Any]:
        now = time.time()
        source_file, source_line, source_symbol = _exception_location(exc)
        context = {**context}
        context.setdefault("source_file", source_file)
        context.setdefault("source_line", source_line)
        context.setdefault("source_symbol", source_symbol)
        fingerprint = issue_fingerprint(exc, context)
        with _WRITE_LOCK:
            issue_id, previous_count = self._existing(fingerprint)
            if not issue_id:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                issue_id = f"ISSUE-{stamp}-{uuid.uuid4().hex[:4].upper()}"
            classification = str(context.get("classification", "") or classify_issue(exc, context))
            trace = redact_sensitive("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            event = {
                "version": ISSUE_STORE_VERSION,
                "event_id": "ISSUE-EVT-" + uuid.uuid4().hex[:12].upper(),
                "issue_id": issue_id,
                "fingerprint": fingerprint,
                "occurrence": previous_count + 1,
                "recorded_at": now,
                "mode": "record_only",
                "classification": classification,
                "repair_eligible": classification == "LOCALAGENT_DEFECT",
                "disposition": str(context.get("disposition", "RECORDED") or "RECORDED"),
                "exception_type": type(exc).__name__,
                "message": redact_sensitive(exc, limit=2000),
                "traceback": trace,
                "run_id": str(context.get("run_id", "") or ""),
                "task_id": str(context.get("task_id", "") or ""),
                "actor": str(context.get("actor", "LOCAL_AGENT") or "LOCAL_AGENT"),
                "stage": str(context.get("stage", "") or ""),
                "iteration": int(context.get("iteration", 0) or 0),
                "tool": str(context.get("tool", "") or ""),
                "action_id": str(context.get("action_id", "") or ""),
                "error_code": str(context.get("error_code", "") or ""),
                "workspace": redact_sensitive(context.get("workspace", ""), limit=1000),
                "source_file": source_file or str(context.get("source_file", "") or ""),
                "source_line": source_line or int(context.get("source_line", 0) or 0),
                "source_symbol": source_symbol or str(context.get("source_symbol", "") or ""),
                "status": _sanitize_json(context.get("status", {})) if isinstance(context.get("status"), dict) else {},
                "detail": redact_sensitive(context.get("detail", ""), limit=3000),
                "localagent_revision": _git_revision(),
                "host_pid": os.getpid(),
            }
            self.event_store.parent.mkdir(parents=True, exist_ok=True)
            with self.event_store.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            self.issue_dir.mkdir(parents=True, exist_ok=True)
            doc_path = self.issue_dir / f"{issue_id}.md"
            self._write_markdown(doc_path, event)
            return {**event, "document": str(doc_path)}

    def _write_markdown(self, path: Path, event: dict[str, Any]) -> None:
        rel_store = os.path.relpath(self.event_store, path.parent).replace("\\", "/")
        lines = [
            f"# {event['issue_id']}", "",
            f"- Mode: `{event['mode']}`",
            f"- Classification: `{event['classification']}`",
            f"- Repair eligible: `{str(event['repair_eligible']).lower()}`",
            f"- Occurrences: `{event['occurrence']}`",
            f"- Fingerprint: `{event['fingerprint']}`",
            f"- Run ID: `{event['run_id'] or 'N/A'}`",
            f"- Task ID: `{event['task_id'] or 'N/A'}`",
            f"- Actor / Stage: `{event['actor']}` / `{event['stage'] or 'N/A'}`",
            f"- Tool / Action: `{event['tool'] or 'N/A'}` / `{event['action_id'] or 'N/A'}`",
            f"- LocalAgent revision: `{event['localagent_revision'] or 'unknown'}`",
            f"- Event store: [{self.event_store.name}]({rel_store})", "",
            "## Exception", "", f"`{event['exception_type']}: {event['message']}`", "",
            "## Source", "",
            f"`{event['source_file'] or 'unknown'}:{event['source_line']} ({event['source_symbol'] or 'unknown'})`", "",
            "## Sanitized traceback", "", "```text", event["traceback"].rstrip(), "```", "",
            "## Context", "", "```text", event["detail"].rstrip(), "```", "",
            "## Stage 11.1 disposition", "",
            "Recorded and deduplicated only. No pause, source modification, restart, or automatic repair was performed.", "",
        ]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(path)
