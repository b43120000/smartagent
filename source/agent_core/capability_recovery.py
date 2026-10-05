"""Bounded recovery hints for model replies that falsely deny local access.

The model never receives direct host access.  It selects a canonical tool and
the Runtime performs the operation inside the already-authorized scope.  This
module only detects a likely capability misunderstanding and renders trusted
capability metadata; it never converts prose into executable authority.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .tool_capabilities import describe_tools, get_allowed_tools


_LOCAL_ACCESS_REFUSAL_PATTERNS = (
    re.compile(r"(?:無法|不能|沒辦法|看不到|讀不到|存取不到).{0,28}(?:本機|電腦|路徑|資料夾|目錄|檔案|專案)"),
    re.compile(r"(?:請|需要).{0,12}(?:上傳|貼上).{0,20}(?:檔案|內容|程式碼|專案)"),
    re.compile(r"(?:cannot|can't|unable to).{0,40}(?:access|read|see|browse).{0,30}(?:local|computer|path|folder|file|project)", re.I),
    re.compile(r"(?:please|you need to).{0,20}upload.{0,30}(?:file|source|project)", re.I),
)

_RECOVERY_TOOL_ORDER = (
    "list_directory",
    "find_file",
    "read_file",
    "inspect_directory",
    "inspect_project_scope",
    "query_project",
    "project_sync",
    "run_command",
)


def detect_false_local_access_refusal(text: str) -> bool:
    """Return True only for a model-side local-access/upload refusal."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(normalized) and any(
        pattern.search(normalized) for pattern in _LOCAL_ACCESS_REFUSAL_PATTERNS
    )


def build_capability_recovery_context(
    source_text: str,
    authorized_paths: Iterable[str],
    *,
    interface: str = "web_direct",
) -> dict[str, Any]:
    """Build non-executable recovery context for one narrative conversion."""
    paths = [str(item).strip() for item in authorized_paths if str(item).strip()]
    allowed = get_allowed_tools(interface)
    tools = [name for name in _RECOVERY_TOOL_ORDER if name in allowed]
    active = bool(paths and tools and detect_false_local_access_refusal(source_text))
    return {
        "active": active,
        "reason": "model_false_local_access_refusal" if active else "",
        "authorized_paths": paths[:32] if active else [],
        "available_tools": describe_tools(tools) if active else [],
    }


def render_capability_recovery_guidance(context: Mapping[str, Any] | None) -> str:
    payload = dict(context or {})
    if not payload.get("active"):
        return ""
    return (
        "[V9_RUNTIME_CAPABILITY_RECOVERY]\n"
        "You do not access the host directly. The software Runtime executes canonical tools "
        "inside the authorized paths and returns objective results. Do not ask the user to "
        "upload or paste content that is already inside an authorized path. Classify the next "
        "decision as ACTION and choose the smallest suitable tool.\n"
        + "authorized_paths="
        + repr(list(payload.get("authorized_paths") or []))
        + "\navailable_tools="
        + repr(list(payload.get("available_tools") or []))
        + "\n[/V9_RUNTIME_CAPABILITY_RECOVERY]\n"
    )


__all__ = [
    "build_capability_recovery_context",
    "detect_false_local_access_refusal",
    "render_capability_recovery_guidance",
]
