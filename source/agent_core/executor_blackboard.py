"""Durable, task/epoch-bound Executor handoff for Goal and Prerequisite loops.

Only Runtime writes terminal receipts. Parent consumption and stage completion
are separate decisions; neither action observation nor a success flag can
silently advance a parent stage. No raw prompt, commands or tool output stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "SMARTAGENT_EXECUTOR_BLACKBOARD_V1"
TERMINAL = frozenset({"VERIFIED", "BLOCKED", "NEEDS_PREREQUISITE"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def _id(value: object) -> str:
    text = str(value or "")
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError("executor_blackboard_invalid_task_identity")
    return text


def _code(value: object) -> str:
    text = str(value or "").strip()
    return text[:160] if _CODE.fullmatch(text) else "UNCLASSIFIED_BLOCKER"


def _root(root: str | Path) -> Path:
    base = Path(root)
    if base.name.casefold() == "source" and (base.parent / "collect.bat").is_file():
        base = base.parent
    return base / "localdata" / "runtime" / "executor_blackboard"


def _path(root: str | Path, task_id: str) -> Path:
    return _root(root) / (_id(task_id) + ".json")


def _load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    handle, tmp = tempfile.mkstemp(prefix=".executor-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parent_binding(loop) -> dict:
    contract = getattr(loop, "atomic_task_contract", None)
    if (not isinstance(contract, Mapping)
            or contract.get("schema") != "SMARTAGENT_ATOMIC_TASK_V1"
            or contract.get("task_id") != getattr(loop, "task_id", None)):
        return {"parent_task_id": "", "parent_task_epoch": "",
                "stage_id": "", "atomic_task_id": str(getattr(loop, "task_id", ""))}
    return {
        "parent_task_id": str(contract.get("parent_task_id") or "")[:160],
        "parent_task_epoch": str(contract.get("parent_task_epoch") or "")[:160],
        "stage_id": str(contract.get("stage_id") or "")[:160],
        "atomic_task_id": str(contract.get("atomic_task_id") or loop.task_id)[:160],
    }


def publish_executor_terminal(loop, result: Mapping, *,
                              root: str | Path, goal: str,
                              evidence_refs: list[str] | tuple[str, ...] = (),
                              resolution_attempts: int = 0,
                              resolution_state: str = "NOT_REQUIRED") -> dict:
    """Reject UNKNOWN and reject VERIFIED without binding to real runtime proof."""
    status = str(result.get("outcome") or "")
    if status not in TERMINAL:
        raise ValueError("executor_blackboard_nonterminal_status")
    if status == "VERIFIED" and (
            result.get("goal_verified") is not True
            or not getattr(loop, "terminal_acceptance", {}).get("evidence_verdict") == "PASS"):
        raise ValueError("executor_blackboard_unverified_success")
    task_id = _id(getattr(loop, "task_id", ""))
    request_id = str(getattr(loop, "run_id", ""))
    epoch = str(getattr(loop, "task_epoch", ""))
    if not request_id or not epoch:
        raise ValueError("executor_blackboard_missing_identity")
    evidence = list(dict.fromkeys(
        str(ref)[:160] for ref in evidence_refs if isinstance(ref, str) and ref
    ))[:24]
    if status == "VERIFIED" and not evidence:
        raise ValueError("executor_blackboard_verified_without_evidence_refs")
    action_ids = list(dict.fromkeys(
        str(ref)[:160] for ref in [result.get("action_id", "")] if ref
    ))
    existing = _load(_path(root, task_id))
    if existing and (existing["request_id"], existing["task_epoch"]) != (request_id, epoch):
        # A task ID may not silently overwrite a previous request/epoch receipt.
        raise ValueError("executor_blackboard_identity_collision")
    binding = _parent_binding(loop)
    data = {
        "schema": SCHEMA, "task_id": task_id, "request_id": request_id,
        "task_epoch": epoch,
        "goal_sha256": hashlib.sha256(str(goal).encode("utf-8")).hexdigest(),
        **binding,
        "executor_state": "TERMINAL", "terminal_status": status,
        "reason_code": _code(result.get("reason_code")),
        "goal_verified": status == "VERIFIED",
        "original_tool_status": str(result.get("original_tool_status") or
                                    result.get("tool_status") or "")[:24],
        "last_observed_tool_status": str(result.get("last_observed_tool_status") or
                                         result.get("tool_status") or "")[:24],
        "side_effect_outcome_confirmed": bool(
            result.get("side_effect_outcome_confirmed", status == "VERIFIED")),
        "side_effect_may_have_occurred": bool(
            result.get("side_effect_may_have_occurred", False)),
        "action_ids": action_ids,
        "evidence_refs": evidence,
        "resolution_attempts": min(16, max(0, int(resolution_attempts))),
        "resolution_state": str(resolution_state)[:40],
        "model_round": max(0, int(result.get("model_round") or 0)),
        "parent_consumption": {
            "status": "PENDING", "stage_verification": "NOT_EVALUATED",
            "consumer_evidence_refs": [],
        },
        "grants_authorization": False,
        "advances_parent_stage": False,
    }
    if existing:
        # Idempotent replay of a terminal receipt is safe; contradictory
        # terminal transitions are rejected rather than overwritten.
        stable = ("terminal_status", "reason_code", "task_epoch", "request_id")
        if any(existing.get(k) != data.get(k) for k in stable):
            raise ValueError("executor_blackboard_terminal_conflict")
        return existing
    _atomic_write(_path(root, task_id), data)
    return data


def read_executor_terminal(root: str | Path, *, task_id: str,
                           request_id: str | None = None,
                           task_epoch: str | None = None) -> dict | None:
    record = _load(_path(root, task_id))
    if not record or record.get("terminal_status") not in TERMINAL:
        return None
    if request_id is not None and record.get("request_id") != request_id:
        return None
    if task_epoch is not None and record.get("task_epoch") != task_epoch:
        return None
    return record


def executor_child_results(root: str | Path, *, parent_task_id: str,
                           parent_task_epoch: str = "", limit: int = 16) -> list[dict]:
    parent = _id(parent_task_id)
    directory = _root(root)
    if not directory.is_dir():
        return []
    results = []
    for path in sorted(directory.glob("*.json"))[-1024:]:
        value = _load(path)
        if not value or value.get("parent_task_id") != parent:
            continue
        if parent_task_epoch and value.get("parent_task_epoch") != parent_task_epoch:
            continue
        if value.get("terminal_status") not in TERMINAL:
            continue
        results.append(value)
    return results[-max(1, min(int(limit), 32)):]


def consume_executor_terminal(root: str | Path, *, child_task_id: str,
                              parent_task_id: str, parent_task_epoch: str,
                              stage_id: str, stage_verification: str,
                              consumer_evidence_refs: list[str]) -> dict:
    """Parent acknowledgement only; NEVER mark a parent Goal/Stage completed."""
    path = _path(root, child_task_id)
    value = _load(path)
    if not value:
        raise ValueError("executor_blackboard_child_not_found")
    if (value.get("parent_task_id") != parent_task_id
            or value.get("parent_task_epoch") != parent_task_epoch
            or value.get("stage_id") != stage_id):
        raise ValueError("executor_blackboard_parent_binding_mismatch")
    if stage_verification not in {"PASS", "FAIL", "NEEDS_RECHECK"}:
        raise ValueError("executor_blackboard_invalid_stage_verification")
    refs = list(dict.fromkeys(str(r)[:160] for r in consumer_evidence_refs if r))[:24]
    if not refs:
        raise ValueError("executor_blackboard_consumer_evidence_required")
    old = value.get("parent_consumption", {})
    if old.get("status") == "CONSUMED":
        if old.get("stage_verification") != stage_verification:
            raise ValueError("executor_blackboard_consumer_conflict")
        return value
    value["parent_consumption"] = {
        "status": "CONSUMED", "stage_verification": stage_verification,
        "consumer_evidence_refs": refs,
    }
    # Even PASS acknowledges one child, not the entire parent stage.
    value["advances_parent_stage"] = False
    _atomic_write(path, value)
    return value


def project_executor_for_parent(value: Mapping | None) -> dict:
    """Bounded safe projection, equally usable by Goal and Prerequisite."""
    if not value or value.get("terminal_status") not in TERMINAL:
        return {}
    fields = (
        "task_id", "parent_task_id", "parent_task_epoch", "stage_id",
        "atomic_task_id", "task_epoch", "terminal_status", "reason_code",
        "goal_verified", "original_tool_status", "last_observed_tool_status",
        "side_effect_outcome_confirmed", "side_effect_may_have_occurred",
        "action_ids", "evidence_refs", "resolution_attempts",
        "resolution_state", "parent_consumption", "grants_authorization",
        "advances_parent_stage",
    )
    return {k: value[k] for k in fields if k in value}
