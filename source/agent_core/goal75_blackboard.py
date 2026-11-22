"""Git75 durable, task-scoped Big Blackboard transition log.

A diagnostic observer, never an admission or evidence authority. Events contain
typed state changes and SHA-256 digests, not prompts, commands, tokens or ToolResult
bodies. Every producer writes to the same request/epoch-owned event chain.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from collections.abc import Mapping

from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_GOAL75_BIG_BLACKBOARD_EVENT_V1"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_VISIBLE = frozenset({
    "runtime_state", "task_state", "state", "status", "stage_status",
    "phase", "outcome", "decision", "current_step", "total_steps",
    "strategy_revision", "revision", "round", "event_count_total",
    "execution_status", "verification_status", "goal_verified",
    "goal_stage", "active_stage", "terminal_kind", "next_category",
})
_MAX_EVENT_BYTES = 32768


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _root(root: str | Path) -> Path:
    base = Path(root).resolve()
    if base.name.casefold() == "source" and (base.parent / "collect.bat").exists():
        base = base.parent
    return base / "localdata" / "runtime" / "goal75_big_blackboard"


def _id(value: object, kind: str) -> str:
    value = str(value or "")
    if not _ID.fullmatch(value):
        raise ValueError("goal75_" + kind + "_invalid")
    return value


def _code(value: object) -> str:
    text = str(value or "")
    return text if _SAFE_CODE.fullmatch(text) else "REDACTED_NONCODE"


def _public_scalar(value: object) -> object:
    if type(value) in {bool, int, float}:
        return value
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return "[REDACTED_OR_COMPLEX]"


def _delta(before: object, after: object) -> tuple[list[str], dict]:
    left = before if isinstance(before, Mapping) else {}
    right = after if isinstance(after, Mapping) else {}
    keys = sorted(set(left) | set(right))
    changed = [str(k) for k in keys if left.get(k) != right.get(k)]
    safe = {}
    for key in changed:
        if key in _VISIBLE:
            safe[key] = {
                "before": _public_scalar(left.get(key)),
                "after": _public_scalar(right.get(key)),
            }
    return changed[:128], safe


def read_events(root: str | Path, task_id: str) -> list[dict]:
    path = _root(root) / (_id(task_id, "task_id") + ".jsonl")
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            raise ValueError("goal75_event_corrupt")
        result.append(row)
    previous = "GENESIS"
    for index, row in enumerate(result, 1):
        body = {k: v for k, v in row.items() if k != "event_sha256"}
        if (row["sequence"] != index or row["prev_event_sha256"] != previous
                or digest(body) != row.get("event_sha256")):
            raise ValueError("goal75_event_chain_invalid")
        previous = row["event_sha256"]
    return result


def emit_transition(
    root: str | Path, *, task_id: str, request_id: str, task_epoch: str,
    module: str, event: str, before: object = None, after: object = None,
    stage_id: str = "", reason_code: str = "",
    evidence_refs: tuple[str, ...] | list[str] = (),
) -> dict:
    """Write one exactly ordered, hash-chained, fsync'd metadata event.

    Callers should pass before/after snapshots *after* an authoritative write.
    The logger cannot change state, grant permission or manufacture evidence.
    """
    task = _id(task_id, "task_id")
    request = _id(request_id, "request_id")
    epoch = _id(task_epoch, "task_epoch")
    path = _root(root) / (task + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    changed, safe = _delta(before, after)
    with exclusive_process_lock(
        path.with_suffix(".lock"), timeout_sec=10.0, label="goal75-big-blackboard"
    ):
        prior = read_events(root, task)
        if prior and (prior[0]["request_id"] != request
                      or prior[0]["task_epoch"] != epoch):
            raise ValueError("goal75_trace_identity_conflict")
        body = {
            "schema": SCHEMA,
            "sequence": len(prior) + 1,
            "time_unix": time.time(),
            "task_id": task, "request_id": request, "task_epoch": epoch,
            "module": _code(module), "event": _code(event),
            "stage_id": _code(stage_id) if stage_id else "",
            "reason_code": _code(reason_code) if reason_code else "",
            "before_sha256": digest(before),
            "after_sha256": digest(after),
            "changed_fields": changed,
            "visible_changes": safe,
            "evidence_refs": [
                _code(ref) for ref in evidence_refs[:24]
            ],
            "prev_event_sha256": (
                prior[-1]["event_sha256"] if prior else "GENESIS"
            ),
            "authorization_granted": False,
            "goal_completion_inferred": False,
        }
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("goal75_event_too_large")
        row = {**body, "event_sha256": digest(body)}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return row


def record_blackboard_write(
    path: str | Path, before: Mapping | None, after: Mapping, *,
    module: str,
) -> dict | None:
    """Best-effort observer for authoritative task/blackboard persistence.

    Missing request/epoch identities are not silently invented or cross-bound.
    """
    if not isinstance(after, Mapping):
        return None
    task = after.get("task_id")
    request = after.get("request_id")
    epoch = after.get("task_epoch") or after.get("epoch") or "LEGACY"
    if not task or not request:
        return None
    base = Path(path).resolve()
    for parent in [base.parent, *base.parents]:
        if parent.name.casefold() == "localdata":
            root = parent.parent
            break
    else:
        return None
    return emit_transition(
        root, task_id=str(task), request_id=str(request),
        task_epoch=str(epoch), module=module, event="AUTHORITATIVE_WRITE",
        before=before, after=after,
    )
