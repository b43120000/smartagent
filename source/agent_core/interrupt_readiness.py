"""Evidence-grounded readiness for a proposed missing-context interruption.

This module cannot authorize a read/write or execute an Action. An interruption
is deferred only when an exact, not-yet-disproven read candidate exists inside
an already authorized path. With no such candidate, USER_INPUT may be the
correct safe next step (for example approval for writing or a missing source
location). A missing target cannot authorize inspecting its parent.
"""
from __future__ import annotations

import ntpath
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_INTERRUPT_READINESS_V1"


def _path_key(value: object) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 2048:
        return ""
    return ntpath.normcase(ntpath.normpath(value))


def _is_missing(key: str, absences: list[dict]) -> bool:
    for row in absences:
        missing = _path_key(row.get("path"))
        if missing and (key == missing or key.startswith(missing + "\\")):
            return True
    return False


def interrupt_readiness(gaps: Mapping, capabilities: Mapping) -> dict:
    """Project unexhausted *scoped* discovery candidates, never widen scope.

    The candidate is only an advisory exact-path inspect. Normal scope and
    Tool Admission are still required; the code does not inspect disk.
    """
    absent = list(gaps.get("known_absences") or [])
    active = list(gaps.get("active_gaps") or [])
    tools = set(capabilities.get("read_tools") or [])
    paths = list(capabilities.get("authorized_paths") or [])
    candidates = []
    seen = set()
    # Only explicit, already-scoped paths; never infer parent or sibling from
    # an absent target. A path known absent cannot be probed indefinitely.
    if "inspect_directory" in tools:
        for raw in paths:
            key = _path_key(raw)
            if not key or key in seen or _is_missing(key, absent):
                continue
            seen.add(key)
            candidates.append({
                "tool": "inspect_directory",
                "paths": [str(raw)],
                "reason": "SCOPED_PATH_NOT_YET_OBSERVED_ABSENT",
                "normal_admission_required": True,
                "authorization_granted": False,
            })
            if len(candidates) >= 8:
                break
    return {
        "schema": SCHEMA,
        "active_goal_gap_ids": [str(g.get("gap_id", "")) for g in active][:16],
        "missing_target_fact_refs": [
            str(g.get("observed_fact_ref", "")) for g in absent
        ][:16],
        "candidate_reads": candidates,
        "candidate_count": len(candidates),
        "scope_expansion_granted": False,
        "write_authorization_granted": False,
        "candidate_is_admission": False,
        "next_safe_category": (
            "SCOPED_READ_DISCOVERY" if candidates else
            "USER_INPUT_OR_OBJECTIVE_BLOCKER_IF_NEEDED"
        ),
    }


_EXPLICIT_USER_CHOICE = re.compile(
    r"授權|批准|同意|允許|寫入權限|存取權限|權限範圍|"
    r"准許|審核|決定|確認是否同意|"
    r"\b(?:approve|approval|permission|consent|authorize|authorization)\b",
    re.I,
)
_RUNTIME_READ_REQUEST = re.compile(
    r"提供.{0,15}(?:檔案|內容|原始碼|source|目錄清單)|"
    r"上傳|貼上|自行檢查|請你檢查|"
    r"\b(?:upload|paste|source code|file contents)\b",
    re.I,
)


def proposed_interrupt_constraint(request: Mapping, readiness: Mapping) -> dict | None:
    """Return a narrow, evidence-based reason to continue instead of aborting.

    Do not override a real user approval request or an independently witnessed
    blocker. A suggested read alone is never authority to execute it.
    """
    kind = str(request.get("kind", "") or "")
    detail = str(request.get("detail", "") or "")
    if kind != "USER_INPUT" or not readiness.get("candidate_reads"):
        return None
    if _EXPLICIT_USER_CHOICE.search(detail):
        return None
    if not _RUNTIME_READ_REQUEST.search(detail):
        return None
    return {
        "marker": "[GOAL_PREREQUISITE_DISCOVERY_REQUIRED]",
        "reason": "known_scoped_discovery_not_exhausted",
        "repair_scope": "GOAL_INTERRUPT_READINESS",
        "detail": "A requested file/content read has a scoped candidate; it is not yet demonstrated to be unavailable.",
        "candidate_reads": list(readiness.get("candidate_reads") or [])[:4],
        "active_goal_gap_ids": list(readiness.get("active_goal_gap_ids") or [])[:8],
        "suggestion": (
            "Use CONTINUE with an exact scoped read Action; normal scope and "
            "tool admission still apply. If that read is denied or cannot "
            "find the source, cite the Runtime evidence and ask the user "
            "for an actual missing path/authorization. Never invent source, "
            "write permission, or a verified Goal."
        ),
    }


__all__ = ["SCHEMA", "interrupt_readiness", "proposed_interrupt_constraint"]
