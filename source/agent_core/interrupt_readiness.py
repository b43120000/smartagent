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


def _read_attempts(runtime) -> list[dict]:
    """Bind prior reader attempts to the current Request/Epoch and exact paths."""
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    results = dict(getattr(runtime, "action_result_ledger", {}) or {})
    request_id = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    out = []
    for action_id, record in actions.items():
        result = results.get(action_id) or {}
        if (result.get("request_id") != request_id
                or result.get("task_epoch") != epoch
                or result.get("evidence_status", "ACTIVE") != "ACTIVE"):
            continue
        action = record.get("action") or {}
        tool = str(action.get("tool", ""))
        if tool == "inspect_directory":
            paths = list(action.get("paths") or [])
        elif tool == "list_directory":
            paths = [action.get("path")]
        else:
            continue
        for path in paths:
            if _path_key(path):
                out.append({"tool": tool, "path": str(path),
                            "action_id": str(action_id)})
    return out


def interrupt_readiness(
    gaps: Mapping, capabilities: Mapping, *,
    attempted_reads: list[dict] | None = None,
    source_candidates: Mapping | None = None,
) -> dict:
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
    known = {
        _path_key(row.get("path")): row.get("observed_value")
        for row in list(gaps.get("gaps") or [])
        if type(row.get("observed_value")) is bool and _path_key(row.get("path"))
    }
    attempted = {
        (str(row.get("tool", "")), _path_key(row.get("path")))
        for row in list(attempted_reads or [])
        if _path_key(row.get("path"))
    }
    # Prioritize provenance-grounded sources that are exact or descendants of
    # an explicitly declared read path. Descendant checks are lexical hints
    # only: real Tool/Scope Admission still decides actual access.
    blocked_candidates = 0
    for source in list((source_candidates or {}).get("candidates") or [])[:16]:
        if not isinstance(source, Mapping):
            continue
        raw = str(source.get("path", ""))
        key = _path_key(raw)
        scope = source.get("scope_status")
        if scope not in {"EXACT_DECLARED_PATH", "WITHIN_DECLARED_PATH"}:
            blocked_candidates += 1
            continue
        if not key or key in seen or _is_missing(key, absent):
            continue
        seen.add(key)
        known_state = known.get(key)
        if known_state is False:
            continue
        listed_parent = (
            source.get("origin") == "COMMITTED_PARENT_DIRECTORY_LISTING"
            and bool(source.get("parent_listing_ref"))
        )
        if known_state is True or listed_parent:
            tool = "list_directory"
            field = {"path": raw}
        else:
            tool = "inspect_directory"
            field = {"paths": [raw]}
        if tool not in tools or (tool, key) in attempted:
            continue
        candidates.append({
            "tool": tool, **field,
            "reason": "GROUNDED_SOURCE_CANDIDATE",
            "candidate_id": str(source.get("candidate_id", ""))[:80],
            "provenance_ref": str(source.get("provenance_ref", ""))[:160],
            "scope_status": str(scope),
            "normal_admission_required": True,
            "authorization_granted": False,
        })
        if len(candidates) >= 8:
            break
    # Existing exact path discovery remains as a fallback after known sources.
    # Use only exact explicitly authorized paths. Never infer parent access.
    # A completed observation cannot be repeated to manufacture progress.
    for raw in paths:
        if len(candidates) >= 8:
            break
        key = _path_key(raw)
        if not key or key in seen or _is_missing(key, absent):
            continue
        seen.add(key)
        known_state = known.get(key)
        if known_state is True:
            tool = "list_directory"
            field = {"path": str(raw)}
            reason = "SCOPED_PRESENT_DIRECTORY_NOT_YET_LISTED"
        elif known_state is False:
            continue
        else:
            tool = "inspect_directory"
            field = {"paths": [str(raw)]}
            reason = "SCOPED_DIRECTORY_NOT_YET_OBSERVED"
        if tool not in tools or (tool, key) in attempted:
            continue
        candidates.append({
            "tool": tool,
            **field,
            "reason": reason,
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
        "out_of_scope_source_candidate_count": blocked_candidates,
        "scope_expansion_granted": False,
        "write_authorization_granted": False,
        "candidate_is_admission": False,
        "next_safe_category": (
            "SCOPED_READ_DISCOVERY" if candidates else
            "USER_INPUT_OR_OBJECTIVE_BLOCKER_IF_NEEDED"
        ),
    }


_EXPLICIT_USER_CHOICE = re.compile(
    # Do not confuse "已授權目錄" (already-scoped location) with an
    # actual request to obtain additional user approval.
    r"(?:請|需要|要求|是否|確認|同意|取得|授予).{0,18}"
    r"(?:(?<!已)授權|批准|准許|允許|寫入權限|存取權限|權限範圍)"
    r"|(?:授權|批准|同意|允許)(?:本次|新增|建立|寫入|修改|部署)"
    r"|\b(?:approve|approval|consent|please authorize|grant permission|"
    r"permission required|need permission)\b",
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


__all__ = ["SCHEMA", "interrupt_readiness", "proposed_interrupt_constraint", "_read_attempts"]
