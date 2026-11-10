"""Read-only, evidence-grounded presentation of the Tri-One TaskResult for WebCopilot.

This is an *egress view*, not an alternate result/completion contract.
Do not parse model conversation text as a terminal result, and never transform
an unverified terminal status into PASS. The original event and TaskResult
remain untouched in RemoteEventStore.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _bounded(value: object, limit: int = 3200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").strip()
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def validated_tri_one_result(task: Mapping[str, Any], event_store,
                             conversation_url: str) -> dict | None:
    """Find a canonical, same-task, same-conversation result in durable egress.

    A missing or malformed TaskResult is not silently reconstructed. The
    UI falls back to an explicitly unverified state instead.
    """
    from .tri_one_task_result import validate_task_result, TaskResultContractError
    from .conversation_identity import same_conversation

    task_id = str(task.get("task_id") or "")
    request_id = str(task.get("request_id") or "")
    epoch = str(task.get("task_epoch") or "")
    if not (task_id and request_id and epoch and conversation_url):
        return None
    try:
        # Delivered events remain persisted; include all delivery states.
        with event_store._lock, event_store.process_lock():
            event_store.load()
            events = list(event_store.events.values())
    except (OSError, ValueError, RuntimeError):
        return None
    events.sort(key=lambda item: float(getattr(item, "created_at", 0) or 0),
                reverse=True)
    for event in events:
        if str(event.task_id) != task_id or str(event.request_id) != request_id:
            continue
        if str(event.event_type) not in {
            "TASK_COMPLETED", "TASK_FAILED", "TASK_INTERRUPTED", "TASK_PAUSED",
        }:
            continue
        route = event.reply_route if isinstance(event.reply_route, Mapping) else {}
        if str(route.get("transport") or "").upper() != "WEBGPT_COPILOT":
            continue
        if not same_conversation(
            str(route.get("conversation_url") or ""), conversation_url,
        ):
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        result = payload.get("task_result_v1")
        if not isinstance(result, Mapping):
            continue
        try:
            checked = validate_task_result(result)
        except (ValueError, TypeError, TaskResultContractError):
            continue
        if (checked["task_id"] != task_id
                or checked["request_id"] != request_id
                or checked["task_epoch"] != epoch
                or checked.get("source_interface") != "WEBGPT_COPILOT"):
            continue
        return checked
    return None


def build_webcopilot_result_view(task: Mapping[str, Any],
                                 canonical: Mapping[str, Any] | None = None) -> dict:
    """Produce a strictly bounded display model; no raw JSON or commands."""
    state = str(task.get("state") or "").upper()
    task_id = _bounded(task.get("task_id"), 128)
    final = (task.get("result_ledger") or {}).get("final")
    final = final if isinstance(final, Mapping) else {}
    canonical = canonical if isinstance(canonical, Mapping) else None
    verified = False
    completion_kind = "UNKNOWN"
    refs: list[str] = []
    summary = ""
    outcome = "UNKNOWN"

    if canonical is not None:
        outcome = str(canonical.get("outcome") or "UNKNOWN").upper()
        completion_kind = str(canonical.get("completion_kind") or "UNKNOWN").upper()
        terminal = canonical.get("terminal_acceptance") or {}
        accepted = terminal.get("status") == "ACCEPTED"
        verified = (
            state == "COMPLETED" and outcome == "SUCCESS"
            and accepted and completion_kind in {"EVIDENCE_BACKED", "ANSWER_ONLY"}
        )
        summary = _bounded(canonical.get("answer_text"), 3600)
        refs = [
            _bounded(ref, 128)
            for ref in (canonical.get("evidence_refs") or [])[:12]
            if isinstance(ref, str) and ref.strip()
        ]
    else:
        # Legacy fields can explain what the worker *reported*, not prove PASS.
        # Don't echo whole task/error dicts, commands, environment or stack traces.
        summary = _bounded(final.get("response"), 2400)
        if not summary:
            summary = _bounded(final.get("summary"), 2400)

    if verified:
        title, severity = "任務已完成", "success"
        note = ("已由三一接口驗收：純文字回答"
                if completion_kind == "ANSWER_ONLY"
                else "三一接口已驗收，執行證據已保留")
    elif state in {"FAILED", "CANCELLED"}:
        title, severity = "任務執行失敗" if state == "FAILED" else "任務已取消", "error"
        note = "尚未取得通過驗收的終止證據"
    elif state in {"INTERRUPTED", "PAUSED"}:
        title, severity = "任務已中斷" if state == "INTERRUPTED" else "任務已暫停", "warning"
        note = "尚未完成，可查看原始任務狀態並接續工作"
    elif state == "COMPLETED":
        title, severity = "Runtime 回報執行結束", "warning"
        note = "三一接口完成驗收尚未確認；不將此狀態視為 PASS"
    elif state == "RUNNING":
        title, severity = "任務執行中", "info"
        note = "等待 Runtime 的實際執行證據"
    else:
        title, severity = "任務已排入佇列", "info"
        note = "尚未開始執行"

    if not summary:
        summary = {
            "COMPLETED": "Worker 已回報處理結束；目前沒有可展示的自然語言摘要。",
            "FAILED": "任務未完成。請查看工作記錄或 Live RCA 以了解失敗原因。",
            "INTERRUPTED": "執行已中斷。請依任務 ID 查詢可接續狀態。",
            "CANCELLED": "本次任務已取消。",
            "PAUSED": "任務已暫停，尚未宣告完成。",
        }.get(state, "SmartAgent 正在處理這筆任務。")

    return {
        "schema": "SMARTAGENT_WEBCOPILOT_RESULT_VIEW_V1",
        "task_id": task_id,
        "state": state,
        "title": title,
        "severity": severity,
        "summary": summary,
        "note": note,
        "evidence_refs": refs,
        "evidence_count": len(canonical.get("evidence_refs") or []) if canonical else 0,
        "completion_kind": completion_kind,
        "outcome": outcome,
        "verified": verified,
        "source": "TRI_ONE_TASK_RESULT_V1" if canonical else "LEGACY_UNVERIFIED",
    }


__all__ = ["validated_tri_one_result", "build_webcopilot_result_view"]
