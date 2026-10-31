#!/usr/bin/env python3
"""P2 diagnostic evidence extractor. Never package raw runtime JSONL.

Only allowlisted scalar fields leave the runtime log. No user messages,
response bodies, URLs, stack traces, environment or serialized tools.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from agent_core.paths import log_root

EVENTS = frozenset({
    "request_started", "request_captured", "planner_response_received",
    "browser_response_completed", "browser_response_failed",
    "ack_accepted", "tool_started", "tool_completed",
    "action_evidence_recorded", "action_loop_state_updated",
    "runtime_terminal_accepted", "terminal_evidence_bound",
    "terminal_evidence_identity_audit", "terminal_decision_normalized",
    "request_local_loop_completed", "request_completed", "request_failed",
    "task_result_contract", "task_progress_updated",
    "interruption_render_unavailable", "action_dependency_blocked",
    "gar_safe_pause", "bounded_repair_observed",
})
FIELDS = frozenset({
    "request_id", "task_id", "round", "action_id", "tool",
    "status", "result_status", "state", "outcome", "terminal_outcome",
    "action_count", "error_type", "evidence_status", "protocol_version",
    "commit_state", "response_stable", "action_executed", "ack_id",
    "source_interface", "request_phase", "completion_status",
})
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _safe_field(key: str, value):
    if key in {"round", "action_count", "protocol_version"}:
        return value if type(value) is int and 0 <= value <= 1000000 else None
    if key in {"response_stable", "action_executed"}:
        return value if type(value) is bool else None
    if isinstance(value, str) and SAFE_TOKEN.fullmatch(value):
        return value
    return None


def _read_jsonl(path: Path, limit: int):
    if not path.is_file() or path.is_symlink():
        return []
    result = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if len(line) > 2 * 1024 * 1024:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                result.append(record)
    return list(result)


def _safe_turns(records: list[dict]):
    output = []
    keys = {
        "utc", "stage", "request_id", "round", "scope_present",
        "scope_id_sha256", "request_user_bound", "request_user_id_sha256",
        "assistant_id_sha256", "assistant_ordinal", "assistant_count_before",
        "user_count_before", "assistant_count_observed", "user_count_observed",
        "response_chars", "response_sha256",
    }
    for row in records:
        if row.get("schema") != "P2_READONLY_TURN_V1":
            continue
        clean = {k: row[k] for k in keys if k in row}
        clean["schema"] = "P2_READONLY_TURN_V1"
        output.append(clean)
    return output


def make_p2_evidence(root: Path) -> dict[str, bytes]:
    """Return named, bounded safe JSON files for the existing ZIP collector."""
    root = Path(root).resolve()
    logs = log_root(root)
    trace_path = logs / "p2-readonly" / "turn_readback.jsonl"
    writer_path = logs / "p2-readonly" / "hybrid_writer.jsonl"
    if not trace_path.is_file() and not writer_path.is_file():
        return {}
    turns = _safe_turns(_read_jsonl(trace_path, 120))
    raw_events = _read_jsonl(logs / "webagent_runtime.jsonl", 1500)
    events = []
    for row in raw_events:
        kind = str(row.get("event", ""))
        if kind not in EVENTS:
            continue
        result = {"event": kind}
        ts = row.get("timestamp")
        if isinstance(ts, str) and len(ts) <= 64:
            result["timestamp"] = ts
        for key in FIELDS:
            if key in row:
                value = _safe_field(key, row[key])
                if value is not None:
                    result[key] = value
        events.append(result)
    # Controller logs can include several tasks. Only identical Request IDs
    # to recorded turns are treated as scoped; unscoped events are diagnostic.
    ids = {r.get("request_id") for r in turns if r.get("request_id")}
    if ids:
        events = [r for r in events if not r.get("request_id")
                  or r["request_id"] in ids]
    accepted = sorted({r.get("request_id") for r in events
                       if r["event"] == "runtime_terminal_accepted"
                       and r.get("request_id")})
    completed = sorted({r.get("request_id") for r in events
                        if r["event"] == "request_completed"
                        and r.get("request_id")})
    failed = sorted({r.get("request_id") for r in events
                     if r["event"] == "request_failed"
                     and r.get("request_id")})
    summary = {
        "schema": "P2_TERMINAL_SUMMARY_V1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": ("FAILED_REQUEST_PRESENT" if failed else
                   "ACCEPTED_AND_COMPLETED" if accepted and set(accepted).issubset(completed)
                   else "UNKNOWN_NOT_FULLY_CORRELATED"),
        "accepted_request_ids": accepted, "completed_request_ids": completed,
        "failed_request_ids": failed, "admission_event_count": len(events),
        "turn_observations": len(turns),
        "limits": ["Events are a bounded window; no full history proof",
                   "Missing records must be treated as UNKNOWN",
                   "This does not assert tool effects or admission PASS on its own"],
    }
    # Hybrid writer log is created from controlled scalar-only instrumentation;
    # apply a second explicit allowlist as defense in depth.
    writers = []
    for row in _read_jsonl(writer_path, 50):
        if row.get("schema") != "COMPOSER_HYBRID_WRITER_V1":
            continue
        names = ("utc", "mode", "prefix_chars", "tail_units", "prompt_chars",
                 "elapsed_seconds", "prompt_sha256", "status")
        writers.append({k: row[k] for k in names if k in row})
    objects = {
        "p2_turn_observation.json": {"schema": "P2_TURN_OBSERVATIONS_V1", "records": turns},
        "p2_admission_trace.json": {"schema": "P2_ADMISSION_TRACE_V1", "events": events,
                                    "raw_runtime_log_included": False},
        "p2_terminal_summary.json": summary,
        "p2_writer_trace.json": {"schema": "P2_WRITER_TRACE_V1", "records": writers},
    }
    return {name: json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            for name, data in objects.items()}
