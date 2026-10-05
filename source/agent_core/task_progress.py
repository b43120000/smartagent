#!/usr/bin/env python3
"""Ephemeral, software-owned progress ledger for one RemoteAgent task."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

PROGRESS_SCHEMA = "SMARTAGENT_TASK_PROGRESS_V1"
RUNTIME_STATES = {"PROCESSING", "COMPLETED", "INTERRUPTED", "PAUSED"}
STEP_STATES = {"PENDING", "IN_PROGRESS", "COMPLETED"}
PROGRESS_DECISIONS = {"CONTINUE", "COMPLETE", "INTERRUPT"}
PROGRESS_OUTCOMES = {"PENDING", "SUCCESS", "FAILED", "PARTIAL", "UNKNOWN"}
COMPLETION_CONTRACT_KEYS = ("success", "failure", "in_progress", "interrupted")
MAX_STEPS = 64
MAX_CONDITIONS_PER_CLASS = 16
MAX_EVIDENCE_REFS = 32
MAX_HISTORY = 96
MAX_TEXT = 2000
MAX_GOAL_TEXT = 32768


class TaskProgressError(ValueError):
    pass


@dataclass
class TaskProgressLedger:
    task_id: str
    request_id: str
    goal: str
    runtime_state: str = "PROCESSING"
    base_evaluation: str = ""
    total_steps: float | int = 0
    current_step: float | int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    current_focus: str = "等待模型評估任務階段"
    next_action: str = ""
    completion_contract: dict[str, list[str]] = field(default_factory=dict)
    decision: str = "CONTINUE"
    outcome: str = "PENDING"
    matched_condition: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    decision_reason: str = ""
    round_id: int = 0
    interruption_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema: str = PROGRESS_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskProgressLedger":
        return cls(
            task_id=str(data.get("task_id", "") or ""),
            request_id=str(data.get("request_id", "") or ""),
            goal=str(data.get("goal", "") or ""),
            runtime_state=str(data.get("runtime_state", "PROCESSING") or "PROCESSING"),
            base_evaluation=str(data.get("base_evaluation", "") or ""),
            total_steps=data.get("total_steps", 0),
            current_step=data.get("current_step", 0),
            steps=list(data.get("steps") or []),
            current_focus=str(data.get("current_focus", "") or ""),
            next_action=str(data.get("next_action", "") or ""),
            completion_contract={
                str(key): [str(item) for item in list(value or [])]
                for key, value in dict(data.get("completion_contract") or {}).items()
            },
            decision=str(data.get("decision", "CONTINUE") or "CONTINUE"),
            outcome=str(data.get("outcome", "PENDING") or "PENDING"),
            matched_condition=str(data.get("matched_condition", "") or ""),
            evidence_refs=[str(item) for item in list(data.get("evidence_refs") or [])],
            decision_reason=str(data.get("decision_reason", "") or ""),
            round_id=int(data.get("round_id", 0) or 0),
            interruption_reason=str(data.get("interruption_reason", "") or ""),
            history=list(data.get("history") or []),
            created_at=float(data.get("created_at", 0.0) or time.time()),
            updated_at=float(data.get("updated_at", 0.0) or time.time()),
            schema=str(data.get("schema", PROGRESS_SCHEMA) or PROGRESS_SCHEMA),
        )


def _progress_dir(root: str | Path | None = None) -> Path:
    from .paths import task_progress_root
    directory = task_progress_root(root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_ledger_path(task_id: str, root: str | Path | None = None) -> Path:
    raw = str(task_id or "").strip()
    safe = "".join(char for char in raw if char.isalnum() or char in "-_")
    if not safe or safe != raw or len(safe) > 160:
        raise TaskProgressError("invalid task_id for progress ledger")
    return _progress_dir(root) / f"{safe}.json"


def _short_text(
    value: Any,
    field_name: str,
    *,
    required: bool = False,
    max_length: int = MAX_TEXT,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise TaskProgressError(f"{field_name} is required")
    if len(text) > max_length:
        raise TaskProgressError(f"{field_name} exceeds {max_length} characters")
    return text


def _number(value: Any, field_name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskProgressError(f"{field_name} must be a number")
    if not math.isfinite(float(value)):
        raise TaskProgressError(f"{field_name} must be finite")
    return value


def _validate_steps(steps: Any, total_steps: float | int) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise TaskProgressError(f"steps must contain 1..{MAX_STEPS} items")
    normalized: list[dict[str, Any]] = []
    previous = -1.0
    for item in steps:
        if not isinstance(item, Mapping):
            raise TaskProgressError("each progress step must be an object")
        number = _number(item.get("step"), "steps[].step")
        if float(number) <= previous or float(number) > float(total_steps):
            raise TaskProgressError("steps must be ascending and within total_steps")
        status = str(item.get("status", "PENDING") or "PENDING").upper()
        if status not in STEP_STATES:
            raise TaskProgressError(f"invalid step status: {status}")
        normalized.append({
            "step": number,
            "desc": _short_text(item.get("desc", item.get("description")), "steps[].desc", required=True),
            "status": status,
        })
        previous = float(number)
    return normalized


def _validate_condition_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_CONDITIONS_PER_CLASS:
        raise TaskProgressError(
            f"{field_name} must contain 1..{MAX_CONDITIONS_PER_CLASS} conditions"
        )
    normalized: list[str] = []
    for item in value:
        condition = _short_text(item, f"{field_name}[]", required=True)
        if condition in normalized:
            raise TaskProgressError(f"{field_name} contains duplicate conditions")
        normalized.append(condition)
    return normalized


def _validate_completion_contract(
    value: Any,
    existing: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    previous = dict(existing or {})
    if value is None:
        if not previous:
            raise TaskProgressError("the first progress update must include completion_contract")
        return {
            key: [str(item) for item in list(previous.get(key) or [])]
            for key in COMPLETION_CONTRACT_KEYS
        }
    if not isinstance(value, Mapping):
        raise TaskProgressError("completion_contract must be an object")
    unknown = set(value) - set(COMPLETION_CONTRACT_KEYS)
    if unknown:
        raise TaskProgressError(
            "completion_contract contains unknown classes: " + ",".join(sorted(unknown))
        )
    proposed: dict[str, list[str]] = {}
    for key in COMPLETION_CONTRACT_KEYS:
        candidate = value.get(key)
        if previous and (candidate is None or candidate == []):
            # Once accepted, completion boundaries are Runtime-owned.  Models
            # commonly clear a condition class after it no longer applies;
            # treating that as a malformed contract can turn a valid terminal
            # result into a protocol failure.  Preserve the accepted class and
            # interpret an omitted/empty later class as "no extension".
            proposed[key] = []
            continue
        proposed[key] = _validate_condition_list(
            candidate, f"completion_contract.{key}"
        )
    normalized: dict[str, list[str]] = {}
    for key in COMPLETION_CONTRACT_KEYS:
        # Runtime owns the accepted contract history.  A later model response
        # may omit or reword an existing condition, but that must not erase an
        # already accepted decision boundary.  Preserve old clauses and append
        # only genuinely new coverage.
        merged = [str(item) for item in list(previous.get(key) or [])]
        merged.extend(item for item in proposed[key] if item not in merged)
        if len(merged) > MAX_CONDITIONS_PER_CLASS:
            raise TaskProgressError(
                f"completion_contract.{key} exceeds preserved condition limit"
            )
        normalized[key] = merged
    return normalized


def _validate_evidence_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_EVIDENCE_REFS:
        raise TaskProgressError(
            f"evidence_refs must contain 1..{MAX_EVIDENCE_REFS} references"
        )
    normalized: list[str] = []
    for item in value:
        reference = _short_text(item, "evidence_refs[]", required=True, max_length=256)
        if reference not in normalized:
            normalized.append(reference)
    return normalized


def _condition_class_for_decision(decision: str, outcome: str) -> str:
    if decision == "CONTINUE":
        return "in_progress"
    if decision == "INTERRUPT":
        return "interrupted"
    return "success" if outcome == "SUCCESS" else "failure"


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_progress(task_id: str, root: str | Path | None = None) -> TaskProgressLedger | None:
    path = get_ledger_path(task_id, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != PROGRESS_SCHEMA:
            return None
        ledger = TaskProgressLedger.from_dict(data)
        if ledger.task_id != task_id or ledger.runtime_state not in RUNTIME_STATES:
            return None
        return ledger
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def initialize_progress(task_id: str, *, request_id: str, goal: str, root: str | Path | None = None) -> TaskProgressLedger:
    existing = read_progress(task_id, root)
    if existing is not None:
        if existing.request_id != str(request_id or "") or existing.goal != str(goal or "").strip():
            raise TaskProgressError("progress ledger identity mismatch")
        return existing
    now = time.time()
    ledger = TaskProgressLedger(
        task_id=str(task_id), request_id=str(request_id or ""),
        goal=_short_text(
            goal, "goal", required=True, max_length=MAX_GOAL_TEXT
        ),
        created_at=now,
        updated_at=now,
    )
    _atomic_write(get_ledger_path(task_id, root), ledger.to_dict())
    return ledger


def validate_model_progress(
    payload: Mapping[str, Any], existing: TaskProgressLedger | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TaskProgressError("report_progress payload must be an object")
    total = _number(payload.get("total_steps"), "total_steps")
    current = _number(payload.get("current_step"), "current_step")
    if float(total) <= 0 or float(current) < 0 or float(current) > float(total):
        raise TaskProgressError("progress step must satisfy 0 <= current_step <= total_steps")
    if existing is not None and float(current) < float(existing.current_step):
        raise TaskProgressError("current_step cannot move backwards")
    supplied_steps = payload.get("steps")
    if supplied_steps is None:
        if existing is None or not existing.steps:
            raise TaskProgressError("the first progress update must include steps")
        if float(total) != float(existing.total_steps):
            raise TaskProgressError("changing total_steps requires a complete steps list")
        steps = existing.steps
    else:
        steps = _validate_steps(supplied_steps, total)
    base = _short_text(
        payload.get("base_evaluation", existing.base_evaluation if existing else ""),
        "base_evaluation", required=not bool(existing and existing.base_evaluation),
    )
    focus = _short_text(payload.get("current_focus"), "current_focus", required=True)
    next_action = _short_text(payload.get("next_action", ""), "next_action")
    completion_contract = _validate_completion_contract(
        payload.get("completion_contract"),
        existing.completion_contract if existing else None,
    )
    decision = str(payload.get("decision", "") or "").strip().upper()
    if decision not in PROGRESS_DECISIONS:
        raise TaskProgressError(f"invalid progress decision: {decision or '(missing)'}")
    outcome = str(payload.get("outcome", "") or "").strip().upper()
    if outcome not in PROGRESS_OUTCOMES:
        raise TaskProgressError(f"invalid progress outcome: {outcome or '(missing)'}")
    if decision == "CONTINUE" and outcome != "PENDING":
        raise TaskProgressError("CONTINUE requires outcome=PENDING")
    if decision == "CONTINUE" and float(current) >= float(total):
        raise TaskProgressError("CONTINUE requires current_step < total_steps")
    if decision == "COMPLETE":
        if float(current) < float(total):
            raise TaskProgressError("COMPLETE requires current_step=total_steps")
        if outcome not in {"SUCCESS", "FAILED", "PARTIAL"}:
            raise TaskProgressError("COMPLETE requires SUCCESS, FAILED, or PARTIAL outcome")
        if any(str(item.get("status", "")).upper() != "COMPLETED" for item in steps):
            raise TaskProgressError("COMPLETE requires every progress step to be COMPLETED")
    if decision == "INTERRUPT" and outcome not in {"FAILED", "UNKNOWN"}:
        raise TaskProgressError("INTERRUPT requires FAILED or UNKNOWN outcome")
    if decision == "CONTINUE" and not next_action:
        raise TaskProgressError("CONTINUE requires next_action")
    matched_condition = _short_text(
        payload.get("matched_condition"), "matched_condition", required=True
    )
    condition_class = _condition_class_for_decision(decision, outcome)
    if matched_condition not in completion_contract[condition_class]:
        raise TaskProgressError(
            f"matched_condition is not declared in completion_contract.{condition_class}"
        )
    evidence_refs = _validate_evidence_refs(payload.get("evidence_refs"))
    decision_reason = _short_text(
        payload.get("decision_reason"), "decision_reason", required=True
    )
    return {
        "total_steps": total, "current_step": current, "steps": steps,
        "base_evaluation": base, "current_focus": focus, "next_action": next_action,
        "completion_contract": completion_contract,
        "decision": decision, "outcome": outcome,
        "matched_condition": matched_condition, "evidence_refs": evidence_refs,
        "decision_reason": decision_reason,
    }


def record_model_progress(
    task_id: str, payload: Mapping[str, Any], *, request_id: str, goal: str,
    round_id: int, root: str | Path | None = None,
) -> TaskProgressLedger:
    ledger = initialize_progress(task_id, request_id=request_id, goal=goal, root=root)
    normalized = validate_model_progress(payload, ledger)
    total = normalized["total_steps"]
    current = normalized["current_step"]
    steps = normalized["steps"]
    base = normalized["base_evaluation"]
    focus = normalized["current_focus"]
    next_action = normalized["next_action"]
    completion_contract = normalized["completion_contract"]
    decision = normalized["decision"]
    outcome = normalized["outcome"]
    matched_condition = normalized["matched_condition"]
    evidence_refs = normalized["evidence_refs"]
    decision_reason = normalized["decision_reason"]
    now = time.time()
    history = list(ledger.history)
    history.append({
        "round_id": int(round_id), "current_step": current, "total_steps": total,
        "current_focus": focus, "next_action": next_action, "updated_at": now,
        "decision": decision, "outcome": outcome,
        "matched_condition": matched_condition, "evidence_refs": evidence_refs,
    })
    updated = TaskProgressLedger(
        task_id=ledger.task_id, request_id=ledger.request_id, goal=ledger.goal,
        runtime_state="PROCESSING", base_evaluation=base,
        total_steps=total, current_step=current, steps=steps,
        current_focus=focus, next_action=next_action, round_id=int(round_id),
        completion_contract=completion_contract, decision=decision, outcome=outcome,
        matched_condition=matched_condition, evidence_refs=evidence_refs,
        decision_reason=decision_reason,
        history=history[-MAX_HISTORY:], created_at=ledger.created_at, updated_at=now,
    )
    _atomic_write(get_ledger_path(task_id, root), updated.to_dict())
    return updated


def set_runtime_state(task_id: str, state: str, *, reason: str = "", root: str | Path | None = None) -> TaskProgressLedger | None:
    ledger = read_progress(task_id, root)
    if ledger is None:
        return None
    normalized = str(state or "").upper()
    if normalized not in RUNTIME_STATES:
        raise TaskProgressError(f"invalid runtime progress state: {normalized}")
    ledger.runtime_state = normalized
    ledger.interruption_reason = _short_text(reason, "interruption_reason")
    ledger.updated_at = time.time()
    _atomic_write(get_ledger_path(task_id, root), ledger.to_dict())
    return ledger


def delete_progress(task_id: str, root: str | Path | None = None) -> bool:
    try:
        get_ledger_path(task_id, root).unlink()
        return True
    except FileNotFoundError:
        return False


def format_telegram_status_view(ledger: TaskProgressLedger | Mapping[str, Any]) -> str:
    if isinstance(ledger, Mapping):
        ledger = TaskProgressLedger.from_dict(ledger)
    labels = {
        "PROCESSING": "執行中",
        "COMPLETED": "執行完畢",
        "INTERRUPTED": "已中斷",
        "PAUSED": "已暫停（等待明確決策）",
    }
    lines = ["📋 任務執行進度", f"狀態：{labels.get(ledger.runtime_state, ledger.runtime_state)}"]
    lines.append(f"目標：{ledger.goal}")
    lines.append(
        f"目前階段：{ledger.current_step} / {ledger.total_steps}"
        if ledger.total_steps else "目前階段：等待模型完成初始評估"
    )
    if ledger.base_evaluation:
        lines.append(f"Base：{ledger.base_evaluation}")
    for step in ledger.steps:
        icon = {"COMPLETED": "✓", "IN_PROGRESS": "▶"}.get(step.get("status"), "·")
        lines.append(f"{icon} {step.get('step')}: {step.get('desc', '')}")
    if ledger.current_focus:
        lines.append(f"目前處理：{ledger.current_focus}")
    if ledger.next_action:
        lines.append(f"下一步：{ledger.next_action}")
    if ledger.decision:
        lines.append(f"模型決策：{ledger.decision}")
    if ledger.outcome and ledger.outcome != "PENDING":
        lines.append(f"執行結果：{ledger.outcome}")
    if ledger.matched_condition:
        lines.append(f"符合條件：{ledger.matched_condition}")
    if ledger.interruption_reason:
        lines.append(f"中斷原因：{ledger.interruption_reason}")
    lines.append("更新時間：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ledger.updated_at)))
    return "\n".join(lines)


def format_prompt_context(ledger: TaskProgressLedger | Mapping[str, Any]) -> str:
    if isinstance(ledger, Mapping):
        ledger = TaskProgressLedger.from_dict(ledger)
    payload = {
        "goal": ledger.goal, "base_evaluation": ledger.base_evaluation,
        "total_steps": ledger.total_steps, "current_step": ledger.current_step,
        "steps": ledger.steps, "current_focus": ledger.current_focus,
        "next_action": ledger.next_action, "runtime_state": ledger.runtime_state,
        "completion_contract": ledger.completion_contract,
        "decision": ledger.decision, "outcome": ledger.outcome,
        "matched_condition": ledger.matched_condition,
        "evidence_refs": ledger.evidence_refs,
        "decision_reason": ledger.decision_reason,
    }
    return (
        "[CURRENT_TASK_PROGRESS]\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n[/CURRENT_TASK_PROGRESS]\n"
        + "以上是 runtime 已接受的進度。請以原始目標、Base、最新工具結果與此進度決定下一步；"
        "本輪仍須先輸出一個 report_progress。若新 evidence 未被 completion_contract 覆蓋，"
        "先擴充對應條件且不得刪除既有條件，再回傳 decision/outcome/matched_condition/evidence_refs。"
    )


__all__ = [
    "COMPLETION_CONTRACT_KEYS", "PROGRESS_DECISIONS", "PROGRESS_OUTCOMES",
    "PROGRESS_SCHEMA", "TaskProgressError", "TaskProgressLedger", "delete_progress",
    "format_prompt_context", "format_telegram_status_view", "get_ledger_path",
    "initialize_progress", "read_progress", "record_model_progress", "set_runtime_state",
    "validate_model_progress",
]
