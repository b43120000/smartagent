"""Goal-directed prerequisite choices derived from current trusted Goal Gap evidence.

Pure, non-authorizing projection. It never scans the filesystem, selects an
Action, grants a parent/sibling scope or assumes that a mutation may run.
Planner owns the decision; existing Tool/Scope/Permission Admission owns execution.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from .conditional_goal_policy import conditional_goal_policy

SCHEMA = "SMARTAGENT_GOAL_DIRECTED_PREREQUISITE_RESOLUTION_V1"

# The dependency is semantic: a missing *destination* need not imply a missing
# *source*. Require explicit provenance language before claiming source REQUIRED.
_SOURCE = re.compile(
    r"(?:先前|既有|原始|既存|已存在).{0,24}?(?:部署)?來源|"
    r"來源(?:完整性|位置|路徑|檔案|資料夾|目錄)|"
    r"(?:部署|複製|還原|解壓|搬移).{0,28}?(?:從|由|來源)|"
    r"(?:從|由).{0,80}?(?:複製|部署|還原|解壓)|"
    r"\b(?:source (?:directory|folder|path|archive|integrity)|"
    r"existing source|from (?:an? )?(?:existing|source)|"
    r"copy from|restore from|extract from)\b",
    re.I,
)
_STANDALONE_CREATE = re.compile(
    r"(?:建立|新增|創建|新建|產生|生成).{0,48}?(?:資料夾|目錄|檔案|空資料夾|folder|directory|file)|"
    r"(?:資料夾|目錄).{0,48}?(?:建立|新增|創建|新建)|"
    r"\b(?:mkdir|create (?:a |the )?(?:new |missing )?"
    r"(?:directory|folder|file))\b",
    re.I,
)
_REQUIRES_PAYLOAD = re.compile(
    r"(?:複製|拷貝|還原|解壓|下載|同步|匯入|部署既有|"
    r"安裝套件)|"
    r"\b(?:copy|clone|checkout|extract|download|restore|import|sync)\b",
    re.I,
)
_SOURCE_REQUEST = re.compile(
    r"(?:提供|告知|確認|指定).{0,32}(?:來源|源目錄|source)|"
    r"(?:來源|source).{0,30}(?:路徑|位置|path)|"
    r"\b(?:provide|upload|paste).{0,40}(?:source|existing folder)\b",
    re.I,
)
_PERMISSION_REQUEST = re.compile(
    r"(?:授權|批准|同意|允許|寫入權限|存取權限)|"
    r"\b(?:permission|approval|authorize|consent)\b",
    re.I,
)


def source_requirement(goal: object, policy: Mapping | None = None) -> str:
    """Source dependency, not a guess about the existence of source files."""
    text = str(goal or "")
    policy = policy or conditional_goal_policy(text)
    if policy.get("intent") == "READ_ONLY_ONLY":
        return "NOT_APPLICABLE"
    if _SOURCE.search(text) or _REQUIRES_PAYLOAD.search(text):
        return "REQUIRED"
    if _STANDALONE_CREATE.search(text):
        return "NOT_REQUIRED"
    return "UNDETERMINED"


def resolve_goal_prerequisites(
    gap_view: Mapping, interrupt_view: Mapping | None = None,
) -> dict:
    """Prefer the shortest Goal-relevant transition after confirmed absence.

    A create candidate is *not* write permission. Source candidates come only
    from the already-scoped, independently computed interrupt readiness view.
    A confirmed absence is reusable; do not re-inspect it without new evidence.
    """
    policy = gap_view.get("goal_policy") or {}
    need = str(gap_view.get("source_requirement") or "UNDETERMINED")
    if need not in {"REQUIRED", "NOT_REQUIRED", "UNDETERMINED", "NOT_APPLICABLE"}:
        need = "UNDETERMINED"
    gaps = [
        row for row in list(gap_view.get("active_gaps") or [])[:16]
        if isinstance(row, Mapping)
        and row.get("status") == "UNRESOLVED"
        and row.get("observation_status") == "CONFIRMED"
        and row.get("observed_value") is False
        and row.get("desired_value") is True
        and isinstance(row.get("observed_fact_ref"), str)
        and bool(row.get("observed_fact_ref"))
    ]
    candidates = list((interrupt_view or {}).get("candidate_reads") or [])[:4]
    # This view does not promote an authorization ceiling or a list of readable
    # paths into permission to create/overwrite a target.
    if not gaps:
        next_category, reason = "EVALUATE_GOAL_OR_OBSERVE", "NO_CONFIRMED_UNRESOLVED_TARGET"
    elif policy.get("intent") == "READ_ONLY_ONLY":
        next_category, reason = "REPORT_CONFIRMED_ABSENCE", "MUTATION_PROHIBITED_BY_GOAL"
    elif need == "NOT_REQUIRED":
        next_category, reason = (
            "PROPOSE_GOAL_ACTION_FOR_ADMISSION", "TARGET_ABSENT_SOURCE_NOT_REQUIRED"
        )
    elif need == "REQUIRED" and candidates:
        next_category, reason = (
            "DISCOVER_SOURCE_WITHIN_SCOPE", "EXPLICIT_SOURCE_DEPENDENCY_UNVERIFIED"
        )
    elif need == "REQUIRED":
        next_category, reason = (
            "SOURCE_LOCATION_OR_SCOPE_REQUIRED", "EXPLICIT_SOURCE_DEPENDENCY_UNVERIFIED"
        )
    else:
        next_category, reason = (
            "RESOLVE_SOURCE_SEMANTICS", "SOURCE_REQUIREMENT_UNDETERMINED"
        )
    return {
        "schema": SCHEMA,
        "next_category": next_category,
        "reason_code": reason,
        "source_requirement": need,
        "confirmed_missing_goal_targets": [
            {"gap_id": str(row.get("gap_id", ""))[:80],
             "path": str(row.get("path", ""))[:1024],
             "evidence_ref": str(row["observed_fact_ref"])[:160]}
            for row in gaps
        ],
        "source_candidate_reads": candidates if next_category == "DISCOVER_SOURCE_WITHIN_SCOPE" else [],
        "source_integrity_verified": False,
        "write_authorization_granted": False,
        "parent_scope_expansion_granted": False,
        "new_filesystem_scan_performed": False,
        "action_selected_or_executed": False,
        "normal_tool_admission_required": True,
        "repeat_absence_observation_needed": False if gaps else None,
        "strategy_authority": "PLANNER",
        "admission_authority": "RUNTIME",
    }


def premature_source_interrupt_diagnostic(
    request: Mapping, resolution: Mapping,
) -> dict | None:
    """Reject asking for a source that the *explicit* create Goal does not need.

    Never suppress genuine permission, approval, or ambiguous-payload questions.
    This does not approve a write: Planner must propose a normal admitted Action.
    """
    if (request.get("kind") != "USER_INPUT"
            or resolution.get("next_category") != "PROPOSE_GOAL_ACTION_FOR_ADMISSION"):
        return None
    detail = str(request.get("detail") or "")
    if not _SOURCE_REQUEST.search(detail) or _PERMISSION_REQUEST.search(detail):
        return None
    return {
        "marker": "[GOAL_PREREQUISITE_ACTION_PREFERRED]",
        "reason": "source_not_required_for_explicit_creation_goal",
        "repair_scope": "GOAL_DIRECTED_PREREQUISITE_RESOLUTION",
        "missing_gap_refs": [
            row["gap_id"] for row in list(resolution.get("confirmed_missing_goal_targets") or [])[:8]
        ],
        "suggestion": (
            "The requested target absence is already confirmed and the Goal "
            "requires local creation, not an existing source. Do not re-inspect "
            "the absent target or request an irrelevant source. Propose the Goal "
            "Action with normal Tool/Scope/Write Admission, or ask for actual "
            "missing write permission. Never assume permission or success."
        ),
    }


__all__ = [
    "SCHEMA", "source_requirement", "resolve_goal_prerequisites",
    "premature_source_interrupt_diagnostic",
]
