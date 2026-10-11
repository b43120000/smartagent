#!/usr/bin/env python3
"""Durable execution plans referenced by a stable ``PLAN-*`` identifier."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock


PLAN_SCHEMA = "SMARTAGENT_EXECUTION_PLAN_V1"
PLAN_STATES = {"READY", "EXECUTING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
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
        authorized_paths: list[str] | None = None,
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
            "authorized_paths": list(
                dict.fromkeys(
                    str(item).strip()
                    for item in (authorized_paths or [])
                    if str(item).strip()
                )
            ),
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
        if str(payload.get("state", "") or "").upper() not in PLAN_STATES:
            raise PlanLedgerError("plan_state_invalid")
        required = ("task_id", "request_id", "workspace", "original_request", "final_summary")
        if any(not str(payload.get(field, "") or "").strip() for field in required):
            raise PlanLedgerError("plan_record_incomplete")
        return dict(payload)

    def authorize_execution(
        self,
        plan_id: str,
        *,
        execution_request_id: str,
        source_message_id: str,
    ) -> str:
        """Issue a one-time ledger-backed authorization for one ingress turn."""
        nonce = secrets.token_hex(24)
        target = self.path_for(plan_id)
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0):
            record = self.load(plan_id)
            if str(record.get("state", "") or "").upper() != "READY":
                raise PlanLedgerError("plan_not_ready")
            pending = dict(record.get("pending_executions") or {})
            pending[nonce] = {
                "execution_request_id": str(execution_request_id or ""),
                "source_message_id": str(source_message_id or ""),
                "authorized_at": time.time(),
            }
            record["pending_executions"] = pending
            record["updated_at"] = time.time()
            write_json_atomic(target, record)
        return nonce

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

    @classmethod
    def execution_request_sha256(cls, record: Mapping[str, Any]) -> str:
        """Bind a queued execution task to the exact ledger-derived envelope."""
        request = cls.execution_request(record)
        return hashlib.sha256(request.encode("utf-8", errors="replace")).hexdigest()

    def _validate_execution_task_record(
        self, task: Any, record: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Revalidate a saved-plan task at the worker trust boundary.

        Ingress metadata is provenance, not authority by itself.  The worker
        must reload the durable plan and prove that the queued request is the
        exact envelope produced from that record before bypassing the generic
        internal-turn rejection rule.
        """
        metadata = dict(getattr(task, "metadata", {}) or {})
        if str(metadata.get("request_kind", "") or "") != "EXECUTE_SAVED_PLAN":
            raise PlanLedgerError("plan_execution_kind_invalid")
        if metadata.get("plan_execution_authorized") is not True:
            raise PlanLedgerError("plan_execution_not_authorized")
        if str(metadata.get("ingress_schema", "") or "") != "AGENT_INGRESS_V1":
            raise PlanLedgerError("plan_execution_ingress_invalid")

        plan_id = str(metadata.get("source_plan_id", "") or "").strip().upper()
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            raise PlanLedgerError("plan_execution_id_invalid")
        self.ensure_workspace(record, str(getattr(task, "workspace", "") or ""))

        if str(metadata.get("source_plan_request_id", "") or "") != str(
            record.get("request_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_request_identity_mismatch")
        if str(metadata.get("source_plan_task_id", "") or "") != str(
            record.get("task_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_task_identity_mismatch")

        expected_request = self.execution_request(record)
        actual_request = str(getattr(task, "request", "") or "")
        if actual_request != expected_request:
            raise PlanLedgerError("plan_execution_envelope_mismatch")
        expected_sha256 = hashlib.sha256(
            expected_request.encode("utf-8", errors="replace")
        ).hexdigest()
        if str(metadata.get("plan_execution_request_sha256", "") or "").lower() != expected_sha256:
            raise PlanLedgerError("plan_execution_digest_mismatch")
        nonce = str(metadata.get("plan_execution_nonce", "") or "").strip()
        authorization = dict((record.get("pending_executions") or {}).get(nonce) or {})
        if not nonce or not authorization:
            raise PlanLedgerError("plan_execution_authorization_missing")
        if str(authorization.get("execution_request_id", "") or "") != str(
            getattr(task, "request_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_authorization_mismatch")
        if str(authorization.get("source_message_id", "") or "") != str(
            metadata.get("source_message_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_source_message_mismatch")
        return nonce, dict(record)

    def validate_execution_task(self, task: Any) -> dict[str, Any]:
        metadata = dict(getattr(task, "metadata", {}) or {})
        plan_id = str(metadata.get("source_plan_id", "") or "").strip().upper()
        record = self.load(plan_id)
        if str(record.get("state", "") or "").upper() != "READY":
            raise PlanLedgerError("plan_not_ready")
        _, validated = self._validate_execution_task_record(task, record)
        return validated

    def claimed_execution_record(self, task: Any) -> dict[str, Any]:
        """Return the ledger-authoritative record for an already claimed task."""
        metadata = dict(getattr(task, "metadata", {}) or {})
        plan_id = str(metadata.get("source_plan_id", "") or "").strip().upper()
        record = self.load(plan_id)
        if str(record.get("state", "") or "").upper() != "EXECUTING":
            raise PlanLedgerError("plan_execution_not_active")
        self._validate_claimed_execution_task(task, record)
        return dict(record)

    def claim_execution_task(self, task: Any) -> dict[str, Any]:
        """Atomically consume authorization and claim one PLAN execution."""
        metadata = dict(getattr(task, "metadata", {}) or {})
        plan_id = str(metadata.get("source_plan_id", "") or "").strip().upper()
        target = self.path_for(plan_id)
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0):
            record = self.load(plan_id)
            state = str(record.get("state", "") or "").upper()
            task_id = str(getattr(task, "task_id", "") or "")
            request_id = str(getattr(task, "request_id", "") or "")
            if state == "EXECUTING":
                self._validate_claimed_execution_task(task, record)
                return record
            if state != "READY":
                raise PlanLedgerError("plan_not_ready")
            nonce, record = self._validate_execution_task_record(task, record)
            pending = dict(record.get("pending_executions") or {})
            pending.pop(nonce, None)
            record.update(
                {
                    "state": "EXECUTING",
                    "pending_executions": pending,
                    "execution_task_id": task_id,
                    "execution_request_id": request_id,
                    "execution_nonce": nonce,
                    "execution_envelope_sha256": self.execution_request_sha256(record),
                    "execution_source_message_id": str(
                        metadata.get("source_message_id", "") or ""
                    ),
                    "execution_started_at": time.time(),
                    "updated_at": time.time(),
                }
            )
            write_json_atomic(target, record)
        return record

    def _validate_claimed_execution_task(
        self, task: Any, record: Mapping[str, Any]
    ) -> None:
        """Revalidate immutable claim fields when the same task is redispatched."""
        metadata = dict(getattr(task, "metadata", {}) or {})
        task_id = str(getattr(task, "task_id", "") or "")
        request_id = str(getattr(task, "request_id", "") or "")
        if (
            str(record.get("execution_task_id", "") or "") != task_id
            or str(record.get("execution_request_id", "") or "") != request_id
        ):
            raise PlanLedgerError("plan_execution_already_claimed")
        if str(metadata.get("request_kind", "") or "") != "EXECUTE_SAVED_PLAN":
            raise PlanLedgerError("plan_execution_kind_invalid")
        if metadata.get("plan_execution_authorized") is not True:
            raise PlanLedgerError("plan_execution_not_authorized")
        if str(metadata.get("ingress_schema", "") or "") != "AGENT_INGRESS_V1":
            raise PlanLedgerError("plan_execution_ingress_invalid")
        if str(metadata.get("source_plan_id", "") or "").upper() != str(
            record.get("plan_id", "") or ""
        ).upper():
            raise PlanLedgerError("plan_execution_id_invalid")
        self.ensure_workspace(record, str(getattr(task, "workspace", "") or ""))
        if str(metadata.get("source_plan_request_id", "") or "") != str(
            record.get("request_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_request_identity_mismatch")
        if str(metadata.get("source_plan_task_id", "") or "") != str(
            record.get("task_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_task_identity_mismatch")
        if str(metadata.get("plan_execution_nonce", "") or "") != str(
            record.get("execution_nonce", "") or ""
        ):
            raise PlanLedgerError("plan_execution_authorization_mismatch")
        if str(metadata.get("source_message_id", "") or "") != str(
            record.get("execution_source_message_id", "") or ""
        ):
            raise PlanLedgerError("plan_execution_source_message_mismatch")
        actual_sha256 = hashlib.sha256(
            str(getattr(task, "request", "") or "").encode("utf-8", errors="replace")
        ).hexdigest()
        claimed_sha256 = str(record.get("execution_envelope_sha256", "") or "").lower()
        if not claimed_sha256 or actual_sha256 != claimed_sha256:
            raise PlanLedgerError("plan_execution_envelope_mismatch")
        if str(metadata.get("plan_execution_request_sha256", "") or "").lower() != claimed_sha256:
            raise PlanLedgerError("plan_execution_digest_mismatch")

    def finish_execution_task(
        self, task: Any, *, state: str, detail: str = ""
    ) -> dict[str, Any]:
        """Persist the terminal state only for the task that owns the claim."""
        target_state = str(state or "").upper()
        if target_state not in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
            raise PlanLedgerError("plan_terminal_state_invalid")
        metadata = dict(getattr(task, "metadata", {}) or {})
        plan_id = str(metadata.get("source_plan_id", "") or "").strip().upper()
        if not plan_id:
            return {}
        target = self.path_for(plan_id)
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0):
            record = self.load(plan_id)
            task_id = str(getattr(task, "task_id", "") or "")
            request_id = str(getattr(task, "request_id", "") or "")
            if (
                str(record.get("execution_task_id", "") or "") != task_id
                or str(record.get("execution_request_id", "") or "") != request_id
            ):
                raise PlanLedgerError("plan_execution_owner_mismatch")
            current = str(record.get("state", "") or "").upper()
            if current == target_state:
                return record
            if current != "EXECUTING":
                raise PlanLedgerError("plan_execution_not_active")
            record.update(
                {
                    "state": target_state,
                    "execution_finished_at": time.time(),
                    "execution_detail": str(detail or ""),
                    "updated_at": time.time(),
                }
            )
            write_json_atomic(target, record)
        return record


def publish_completed_plan(
    *,
    ledger: PlanLedger,
    task: Any,
    final_summary: str,
    progress: Any | None,
    authorized_paths: list[str] | None = None,
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
        authorized_paths=authorized_paths,
    )
    plan_id = str(record["plan_id"])
    rendered = str(final_summary or "").rstrip()
    rendered += f"\n\nplan_id: {plan_id}\n後續可直接回覆：執行 {plan_id}"
    return rendered, plan_id


__all__ = [
    "PLAN_SCHEMA",
    "PLAN_STATES",
    "PlanLedger",
    "PlanLedgerError",
    "is_planning_request",
    "parse_execute_plan_command",
    "plan_ledger_root_from_task_store",
    "publish_completed_plan",
]
