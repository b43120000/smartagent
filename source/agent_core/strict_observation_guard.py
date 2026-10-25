"""Request-scoped hard gate for strict, exclusively canonical fact observation tasks.

This is intentionally narrow. It reads ONLY the original trusted request,
never model progress, tool results or untrusted file contents. A strict request
does not authorize preliminary reads as an implicit way to mint condition IDs.
"""
from __future__ import annotations

import re


def strict_observation_only_request(request: str) -> bool:
    text = str(request or "")
    if "canonical observe_fact" not in text:
        return False
    if "completion_contract.success" not in text:
        return False
    if not re.search(r"各(?:使用|執行)一次", text):
        return False
    if not re.search(r"(?:不允許|不得)[^。\n]{0,60}補救操作", text):
        return False
    # Explicitly requested reads must NOT accidentally enter this exclusive
    # contract: normal requests can include legitimate reads before observation.
    if "read_file" in text or "請先讀取" in text:
        return False
    return True


def forbidden_strict_actions(request: str, operational: list[dict]) -> list[str]:
    if not strict_observation_only_request(request):
        return []
    return [
        str(action.get("tool", "") or "")
        for action in operational
        if str(action.get("tool", "") or "") not in {"observe_fact", "final_response"}
    ]

def strict_observation_action_diagnostic(request: str, operational: list[dict]) -> dict | None:
    """Return the same fail-closed admission error before any Tool execution."""
    forbidden = forbidden_strict_actions(request, operational)
    if not forbidden:
        return None
    import json
    return {
        "marker": "[WEBAGENT_STRICT_OBSERVATION_REJECTED]",
        "reason": "strict_observation_extra_action_forbidden",
        "detail": "forbidden_tools=" + json.dumps(forbidden, separators=(",", ":")),
        "suggestion": (
            "原始 Request 僅允許指定次數的 canonical observe_fact，不可用 read_file／"
            "inspect_project_ledger 等占位或補救工具換取 condition_id。"
            "首輪只提交 report_progress 登記兩個 success conditions，"
            "加 turn_commit(action_count=1)；Runtime 發行正式 condition_id 後"
            "才依序執行兩次 canonical observe_fact，不得修改 Predicate。"
        ),
    }

