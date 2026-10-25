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
