"""PokeClaw-style per-round Executor record; independent of parent-agent states.

A tool's three-field PokeClaw response is NOT a claim of whole-goal success.
Every accepted/rejected tool proposal is captured before the Planner's next
turn, with the original typed Runtime status in a separate evidence namespace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_EXECUTOR_RUN_BLACKBOARD_V1"
MAX_EVENTS = 128
MAX_TEXT = 12000


def _id(text: object) -> str:
    value = str(text or "")
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("executor_run_blackboard_task_id_invalid")
    return value


def _path(root: str | Path, task_id: str) -> Path:
    base = Path(root).resolve()
    if base.name.casefold() == "source" and (base.parent / "collect.bat").is_file():
        base = base.parent
    return base / "localdata" / "runtime" / "executor_run_blackboard" / (_id(task_id) + ".json")


def _atomic(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd, temporary = tempfile.mkstemp(prefix=".executor-run-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")).hexdigest()


def _safe_arguments(args: object) -> dict:
    """Keep exact path binding but never persist secrets or shell contents."""
    source = args if isinstance(args, Mapping) else {}
    safe = {}
    for key, value in source.items():
        if key in {"path", "root", "workspace", "working_directory", "project_root"}:
            safe[key] = str(value)[:512]
        elif key == "paths" and isinstance(value, list):
            safe[key] = [str(item)[:512] for item in value[:16]]
        elif key in {"recursive", "operation", "timeout", "max_files", "max_bytes"}:
            safe[key] = value if isinstance(value, (str, int, bool, float, type(None))) else str(type(value).__name__)
        elif key in {"command", "content", "text", "code", "password", "token", "api_key", "secret"}:
            safe[key] = "[REDACTED]"
    return safe


def read_executor_run(root: str | Path, *, task_id: str, request_id: str | None = None,
                      task_epoch: str | None = None) -> dict | None:
    path = _path(root, task_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    if request_id is not None and value.get("request_id") != request_id:
        return None
    if task_epoch is not None and value.get("task_epoch") != task_epoch:
        return None
    return value


def begin_executor_run(root: str | Path, loop, request: str, *, evidence_requirement: Mapping | None = None) -> dict:
    task_id = _id(loop.task_id)
    prior = read_executor_run(root, task_id=task_id)
    if prior:
        # No append to another request/epoch; a restarted task has to use the
        # existing continuation protocol, not overwrite earlier evidence.
        if prior.get("request_id") != loop.run_id or prior.get("task_epoch") != loop.task_epoch:
            raise ValueError("executor_run_blackboard_identity_conflict")
        if prior.get("events"):
            raise ValueError("executor_run_blackboard_restart_requires_continuation")
        if evidence_requirement is not None and prior.get("evidence_requirement") != dict(evidence_requirement):
            raise ValueError("executor_run_blackboard_requirement_conflict")
        return prior
    bind = "\0".join((task_id, str(loop.run_id), str(loop.task_epoch)))
    handoff_id = "HOF-" + hashlib.sha256(bind.encode("utf-8")).hexdigest()[:24].upper()
    record = {
        "schema": SCHEMA,
        "task_id": task_id, "request_id": str(loop.run_id),
        "task_epoch": str(loop.task_epoch),
        "request_sha256": hashlib.sha256(str(request).encode("utf-8")).hexdigest(),
        "execution_mode": "POKECLAW_AGENT_LOOP",
        "evidence_requirement": dict(evidence_requirement or {}),
        "executor_state": "RUNNING",
        "goal_driver_state": "DISABLED",
        "prerequisite_state": "DISABLED",
        "parent_dispatch_state": "NOT_DISPATCHED",
        "parent_consumption_state": "NOT_APPLICABLE_EXECUTOR_ONLY",
        "handoff_id": handoff_id,
        "events": [], "terminal": None,
        "authorization_granted_by_blackboard": False,
    }
    _atomic(_path(root, task_id), record)
    return record


def append_executor_event(root: str | Path, loop, event: dict) -> dict:
    value = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if value is None or value.get("terminal") is not None:
        raise ValueError("executor_run_blackboard_not_open")
    if len(value["events"]) >= MAX_EVENTS:
        raise ValueError("executor_run_blackboard_event_limit")
    number = len(value["events"]) + 1
    row = {**event, "sequence": number}
    row["event_id"] = "ER-EV-" + _digest({
        "task_id": loop.task_id, "task_epoch": loop.task_epoch,
        "sequence": number, "event": row,
    })[:24].upper()
    value["events"].append(row)
    _atomic(_path(root, loop.task_id), value)
    return row


def finish_executor_run(root: str | Path, loop, *,
                        terminal_kind: str, summary: str,
                        reported_outcome: str = "UNKNOWN") -> dict:
    value = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if value is None or value.get("terminal") is not None:
        raise ValueError("executor_run_blackboard_terminal_conflict")
    kind = str(terminal_kind)
    if kind not in {
        "MODEL_REPORTED_COMPLETE_UNVERIFIED", "BLOCKED", "MAX_ROUNDS",
        "FORMAT_ERROR", "EXECUTION_OUTCOME_UNKNOWN",
    }:
        raise ValueError("executor_run_blackboard_terminal_kind_invalid")
    value["executor_state"] = kind
    value["terminal"] = {
        "kind": kind, "round": int(loop.turn_id),
        "summary_sha256": hashlib.sha256(str(summary).encode("utf-8")).hexdigest(),
        "outcome": str(reported_outcome),
        "goal_verified": False,
        "tool_evidence_refs": [
            row.get("runtimeEvidence", {}).get("evidence_ref")
            for row in value["events"]
            if row.get("runtimeEvidence", {}).get("evidence_ref")
        ],
        "admission_rejection_count": sum(
            1 for row in value["events"]
            if row.get("runtimeEvidence", {}).get("status") == "REJECTED"
        ),
    }
    _atomic(_path(root, loop.task_id), value)
    readback = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if (readback is None or readback.get("terminal") != value["terminal"]
            or readback.get("handoff_id") != value["handoff_id"]
            or readback.get("events") != value["events"]):
        raise ValueError("executor_run_blackboard_readback_failed")
    return readback


def record_finish_comparison(root: str | Path, loop, comparison: Mapping) -> dict:
    """Persist Runtime's own comparison after model Finish and verify readback."""
    value = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if (value is None or not isinstance(value.get("terminal"), dict)
            or value["terminal"].get("kind") != "MODEL_REPORTED_COMPLETE_UNVERIFIED"
            or "finish_comparison" in value["terminal"]):
        raise ValueError("executor_finish_comparison_terminal_invalid")
    verdict = comparison.get("verdict")
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("executor_finish_comparison_verdict_invalid")
    record = {
        "verdict": verdict,
        "reason_code": str(comparison.get("reason_code") or "")[:160],
        "goal_kind": str(comparison.get("goal_kind") or "")[:160],
        "goal_evidence_reason": str(comparison.get("goal_evidence_reason") or "")[:160],
        "coverage_basis": str(comparison.get("coverage_basis") or "NOT_VERIFIED")[:160],
        "completion_kind": str(comparison.get("completion_kind") or "EVIDENCE_BACKED")[:80],
        "answer_sha256": str(comparison.get("answer_sha256") or ""),
        "evidence_refs": list(comparison.get("evidence_refs") or []),
        "mismatches": list(comparison.get("mismatches") or [])[:20],
        "tool_results": list(comparison.get("tool_results") or []),
    }
    value["terminal"]["finish_comparison"] = record
    value["terminal"]["goal_verified"] = verdict == "PASS"
    value["terminal"]["outcome"] = verdict
    _atomic(_path(root, loop.task_id), value)
    checked = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if checked is None or checked.get("terminal") != value["terminal"]:
        raise ValueError("executor_finish_comparison_readback_failed")
    return checked


def projection(value: Mapping) -> dict:
    """Only actual receipt state, never a parent/Goal Driver verdict."""
    return {
        "task_id": value["task_id"], "request_id": value["request_id"],
        "executor_state": value["executor_state"],
        "goal_driver_state": value["goal_driver_state"],
        "prerequisite_state": value["prerequisite_state"],
        "handoff_id": value["handoff_id"],
        "parent_dispatch_state": value["parent_dispatch_state"],
        "parent_consumption_state": value["parent_consumption_state"],
        "event_count": len(value["events"]),
        "tool_evidence_refs": list((value.get("terminal") or {}).get("tool_evidence_refs") or []),
        "goal_verified": bool((value.get("terminal") or {}).get("goal_verified", False)),
    }


__all__ = [
    "begin_executor_run", "append_executor_event", "finish_executor_run",
    "read_executor_run", "record_finish_comparison", "projection", "_safe_arguments",
]
