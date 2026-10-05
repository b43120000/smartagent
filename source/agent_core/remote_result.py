#!/usr/bin/env python3
"""Stage 9 RemoteAgent result-return protocol helpers."""
from __future__ import annotations

import json
from typing import Any

RESULT_MARKER = "[REMOTE_AGENT_RESULT]"
MAX_SUMMARY_CHARS = 6000


def build_result_payload(*, task: Any, status: str, summary: str) -> dict[str, Any]:
    payload = {
        "type": "REMOTE_AGENT_RESULT",
        "protocol": "remote_agent",
        "protocol_version": 1,
        "request_id": str(task.request_id or ""),
        "task_id": str(task.task_id or ""),
        "status": str(status or "FAILED").upper(),
        "summary": str(summary or "")[:MAX_SUMMARY_CHARS],
    }
    route_context = dict(getattr(task, "metadata", {}).get("route_context", {}) or {})
    if route_context:
        mode = dict(route_context.get("mode") or {})
        carrier = dict(route_context.get("carrier") or {})
        payload["routing"] = {
            "mode": str(mode.get("selected_mode", "GENERAL_AGENT")),
            "execution": str(mode.get("execution_path", "CURRENT_PLANNER_LOOP")),
            "carrier": str(carrier.get("selected_carrier", "CHATGPT_CONVERSATION")),
        }
    return payload


def build_result_prompt(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{RESULT_MARKER}\n{encoded}\n"
        "這是 PC LocalAgent 回傳的最終執行結果，不是新的 RemoteAgent 任務。"
        "請只用一般繁體中文向手機使用者回覆結果；回覆必須包含 request_id、status 與 summary。"
        "若 payload 含 routing，回覆也必須清楚顯示 Mode 與 Carrier。"
        "禁止輸出 remoteagent_control、smartagent_tool、JSON code fence，禁止要求再次執行任務。"
    )


def validate_result_reply(reply: str, *, request_id: str) -> tuple[bool, str]:
    text = str(reply or "").strip()
    if not text:
        return False, "empty_result_reply"
    lowered = text.lower()
    if "remoteagent_control" in lowered or "smartagent_tool" in lowered:
        return False, "control_envelope_in_result_reply"
    if str(request_id or "") not in text:
        return False, "request_id_missing_from_result_reply"
    return True, "ok"


__all__ = [
    "RESULT_MARKER", "MAX_SUMMARY_CHARS", "build_result_payload",
    "build_result_prompt", "validate_result_reply",
]
