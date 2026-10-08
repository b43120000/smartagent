#!/usr/bin/env python3
"""Durable execution plans referenced by a stable ``PLAN-*`` identifier."""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock


PLAN_SCHEMA = "SMARTAGENT_EXECUTION_PLAN_V1"
PLAN_ID_PATTERN = re.compile(r"PLAN-[A-F0-9]{20}", re.IGNORECASE)
EXECUTE_PLAN_PATTERN = re.compile(
    r"^\s*(?:請\s*)?(?:開始\s*)?(?:執行|實作|套用)\s*(?:規劃\s*)?"
    r"(?P<plan_id>PLAN-[A-F0-9]{20})\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


class PlanLedgerError(RuntimeError):
    pass


def plan_ledger_root_from_task_store(task_store_path: str | Path) -> Path:
    return Path(task_store_path).resolve().parent / "plan_ledger"


def parse_execute_plan_command(text: str) -> str:
    match = EXECUTE_PLAN_PATTERN.fullmatch(str(text or ""))
    return match.group("plan_id").upper() if match else ""


def is_planning_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().casefold().split())
    if (
        not normalized
        or parse_execute_plan_command(normalized)
        or "[smartagent_execute_saved_plan]" in normalized
    ):
        return False
    explicit_planning = (
        "先規劃不改",
        "只規劃不改",
        "先評估不改",
        "只評估不改",
        "先分析不改",
        "只分析不改",
        "不要修改",
        "不做修改",
        "盤點與改動規劃",
    )
    if any(marker in normalized for marker in explicit_planning):
        return True
    planning_markers = ("規劃", "盤點", "評估", "分析", "rca", "設計")
    execution_markers = (
        "開始修改",
        "直接修改",
        "開始實作",
        "直接實作",
        "執行規劃",
        "執行 plan-",
        "實作 plan-",
    )
    return any(marker in normalized for marker in planning_markers) and not any(
        marker in normalized for marker in execution_markers
    )


def _normalized_workspace(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


class PlanLedger:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".plan-ledger.lock"

    @classmethod
    def from_task_store(cls, task_store_path: str | Path) -> "PlanLedger":
        return cls(plan_ledger_root_from_task_store(task_store_path))

    @staticmethod
    def plan_id_for(*, task_id: str, request_id: str) -> str:
        seed = f"{str(task_id).strip()}\0{str(request_id).strip()}"
        return "PLAN-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20].upper()

    def path_for(self, plan_id: str) -> Path:
        canonical = str(plan_id or "").strip().upper()
        if not PLAN_ID_PATTERN.fullmatch(canonical):
            raise PlanLedgerError("invalid_plan_id")
        return self.root / f"{canonical}.json"

    def create(
        self,
        *,
        task_id: str,
        request_id: str,
        workspace: str,
        original_request: str,
        final_summary: str,
        progress: Mapping[str, Any] | None = None,
        conversation_url: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        if not is_planning_request(original_request):
            raise PlanLedgerError("request_is_not_a_planning_task")
        plan_id = self.plan_id_for(task_id=task_id, request_id=request_id)
        target = self.path_for(plan_id)
        now = time.time()
        payload = {
            "schema": PLAN_SCHEMA,
            "plan_id": plan_id,
            "state": "READY",
            "task_id": str(task_id or ""),
            "request_id": str(request_id or ""),
            "workspace": str(Path(workspace).resolve()),
            "original_request": str(original_request or "").strip(),
            "final_summary": str(final_summary or "").strip(),
            "progress": dict(progress or {}),
            "conversation_url": str(conversation_url or ""),
            "session_id": str(session_id or ""),
            "created_at": now,
            "updated_at": now,
        }
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0):
            if target.is_file():
                existing = self.load(plan_id)
                if (
                    existing.get("task_id") != payload["task_id"]
                    or existing.get("request_id") != payload["request_id"]
                ):
                    raise PlanLedgerError("plan_identity_conflict")
                return existing
            write_json_atomic(target, payload)
        return payload

    def load(self, plan_id: str) -> dict[str, Any]:
        target = self.path_for(plan_id)
        if not target.is_file():
            raise PlanLedgerError("plan_not_found")
        try:
            payload = read_json_retry(target)
        except (OSError, ValueError, TypeError) as exc:
            raise PlanLedgerError("plan_record_unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema") != PLAN_SCHEMA:
            raise PlanLedgerError("plan_record_invalid")
        if str(payload.get("plan_id", "")).upper() != target.stem.upper():
            raise PlanLedgerError("plan_identity_mismatch")
        required = ("task_id", "request_id", "workspace", "original_request", "final_summary")
        if any(not str(payload.get(field, "") or "").strip() for field in required):
            raise PlanLedgerError("plan_record_incomplete")
        return dict(payload)

    @staticmethod
    def ensure_workspace(record: Mapping[str, Any], workspace: str | Path) -> None:
        if _normalized_workspace(record.get("workspace", "")) != _normalized_workspace(workspace):
            raise PlanLedgerError("plan_workspace_mismatch")

    @staticmethod
    def execution_request(record: Mapping[str, Any]) -> str:
        plan_id = str(record.get("plan_id", "") or "").upper()
        progress = dict(record.get("progress") or {})
        progress_summary = {
            "base_evaluation": progress.get("base_evaluation", ""),
            "steps": progress.get("steps", []),
            "completion_contract": progress.get("completion_contract", {}),
            "decision": progress.get("decision", ""),
            "outcome": progress.get("outcome", ""),
        }
        import json

        return (
            "[SMARTAGENT_EXECUTE_SAVED_PLAN]\n"
            f"plan_id: {plan_id}\n"
            "這是使用者明確要求執行的既有規劃。不要重新停在盤點或規劃階段；"
            "先依目前檔案狀態核對規劃仍適用，再直接執行可安全執行的修改與驗證。"
            "所有 Workspace、路徑、權限及安全規則維持不變。\n"
            "[ORIGINAL_PLANNING_REQUEST]\n"
            f"{record.get('original_request', '')}\n"
            "[/ORIGINAL_PLANNING_REQUEST]\n"
            "[ACCEPTED_PLAN]\n"
            f"{record.get('final_summary', '')}\n"
            "[/ACCEPTED_PLAN]\n"
            "[PLAN_PROGRESS_SNAPSHOT]\n"
            f"{json.dumps(progress_summary, ensure_ascii=False)}\n"
            "[/PLAN_PROGRESS_SNAPSHOT]\n"
            "[/SMARTAGENT_EXECUTE_SAVED_PLAN]"
        )


def publish_completed_plan(
    *,
    ledger: PlanLedger,
    task: Any,
    final_summary: str,
    progress: Any | None,
) -> tuple[str, str]:
    request = str(getattr(task, "request", "") or "")
    if not is_planning_request(request):
        return str(final_summary or ""), ""
    progress_payload = progress.to_dict() if hasattr(progress, "to_dict") else dict(progress or {})
    if (
        str(progress_payload.get("decision", "") or "").upper() != "COMPLETE"
        or str(progress_payload.get("outcome", "") or "").upper()
        not in {"SUCCESS", "PARTIAL"}
    ):
        return str(final_summary or ""), ""
    record = ledger.create(
        task_id=str(getattr(task, "task_id", "") or ""),
        request_id=str(getattr(task, "request_id", "") or ""),
        workspace=str(getattr(task, "workspace", "") or ""),
        original_request=request,
        final_summary=str(final_summary or ""),
        progress=progress_payload,
        conversation_url=str(getattr(task, "conversation_url", "") or ""),
        session_id=str(getattr(task, "session_id", "") or ""),
    )
    plan_id = str(record["plan_id"])
    rendered = str(final_summary or "").rstrip()
    rendered += f"\n\nplan_id: {plan_id}\n後續可直接回覆：執行 {plan_id}"
    return rendered, plan_id


__all__ = [
    "PLAN_SCHEMA",
    "PlanLedger",
    "PlanLedgerError",
    "is_planning_request",
    "parse_execute_plan_command",
    "plan_ledger_root_from_task_store",
    "publish_completed_plan",
]
