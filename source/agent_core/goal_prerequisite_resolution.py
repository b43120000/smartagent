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
    source_view: Mapping | None = None,
    source_integrity_view: Mapping | None = None,
    artifact_view: Mapping | None = None,
    remote_access_view: Mapping | None = None,
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
    source_records = list((source_view or {}).get("candidates") or [])[:16]
    remote_artifacts = list((artifact_view or {}).get("artifact_references") or [])[:8]
    remote_view = remote_access_view if isinstance(remote_access_view, Mapping) else {}
    remote_state = str(remote_view.get("authorization_state") or "UNKNOWN")
    if remote_state not in {"UNKNOWN", "AUTHORIZED", "DENIED", "NEEDS_USER_APPROVAL"}:
        remote_state = "UNKNOWN"
    verified_structures = list((source_integrity_view or {}).get("sources") or [])[:16]
    source_ready = any(row.get("source_ready") is True for row in verified_structures
                       if isinstance(row, Mapping))
    structure_observed = any(row.get("structure_state") == "STRUCTURE_VERIFIED"
                             for row in verified_structures if isinstance(row, Mapping))
    source_reads = [row for row in candidates
                    if isinstance(row, Mapping) and row.get("reason") == "GROUNDED_SOURCE_CANDIDATE"]
    non_source_reads = [row for row in candidates
                        if isinstance(row, Mapping) and row.get("reason") != "GROUNDED_SOURCE_CANDIDATE"]
    outside = [row for row in source_records
               if isinstance(row, Mapping) and row.get("scope_status") == "OUTSIDE_DECLARED_PATHS"]
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
    elif need == "REQUIRED" and source_ready:
        next_category, reason = (
            "CHECK_DEPLOYMENT_ADMISSION", "SOURCE_INTEGRITY_EVIDENCE_AVAILABLE"
        )
    elif need == "REQUIRED" and structure_observed:
        next_category, reason = (
            "VERIFY_SOURCE_CONTENTS", "SOURCE_STRUCTURE_ONLY_NOT_CONTENT_INTEGRITY"
        )
    elif need == "REQUIRED" and source_reads:
        next_category, reason = (
            "DISCOVER_SOURCE_WITHIN_SCOPE", "GROUNDED_SOURCE_CANDIDATE_UNVERIFIED"
        )
    elif need == "REQUIRED" and outside:
        next_category, reason = (
            "SOURCE_SCOPE_AUTHORIZATION_REQUIRED", "GROUNDED_SOURCE_OUTSIDE_READ_SCOPE"
        )
    elif need == "REQUIRED" and remote_artifacts:
        next_category, reason = (
            "RESOLVE_REGISTERED_SOURCE_ARTIFACT",
            "PINNED_REMOTE_ARTIFACT_REFERENCE_REQUIRES_ACQUISITION",
        )
    elif need == "REQUIRED" and non_source_reads:
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
        "source_candidates": source_records,
        "registered_artifact_references": remote_artifacts,
        "registered_artifact_count": len(remote_artifacts),
        "artifact_retrieval_is_source_observation": False,
        "remote_access_authorization_state": remote_state,
        "remote_access_authorized": (
            True if remote_state == "AUTHORIZED"
            else False if remote_state == "DENIED"
            else None
        ),
        "remote_access_authorization_evidence_ref": str(
            remote_view.get("authorization_evidence_ref") or ""
        )[:160],
        "remote_access_next_verification": str(
            remote_view.get("next_verification") or
            "CHECK_EXISTING_GIT_AND_SCOPE_ADMISSION"
        )[:160],
        "git_executable_status": str(remote_view.get("git_executable_status") or "UNKNOWN"),
        "git_action_route": {"tool": "run_command", "operation": "GIT_INSPECT"},
        "remote_access_missing_evidence_is_denial": False,
        "artifact_integrity_verified": False,
        "out_of_scope_source_candidates": [
            {"candidate_id": str(row.get("candidate_id", ""))[:80],
             "path": str(row.get("path", ""))[:1024],
             "provenance_ref": str(row.get("provenance_ref", ""))[:160]}
            for row in outside[:4]
        ],
        "source_structure_observed": structure_observed,
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
    if request.get("kind") != "USER_INPUT":
        return None
    category = resolution.get("next_category")
    detail = str(request.get("detail") or "")
    remote_reference = (
        category == "RESOLVE_REGISTERED_SOURCE_ARTIFACT"
        and int(resolution.get("registered_artifact_count", 0) or 0) > 0
    )
    # A missing receipt is UNKNOWN, not an evidence-backed denial. When the
    # bundled Git executable was actually probed and no readiness probe has
    # yet been attempted, request a normal admitted capability check before
    # accepting a premature generic GitHub approval interruption. This nudge
    # never authorizes network access or mutates the project.
    if (remote_reference
            and resolution.get("remote_access_authorization_state") == "UNKNOWN"
            and resolution.get("git_executable_status") == "AVAILABLE"
            and not resolution.get("remote_access_probe_guidance_already_issued")
            and re.search(r"(?i)github|repository|私人儲存庫|遠端存取|網路授權|取得壓縮檔", detail)
            and _PERMISSION_REQUEST.search(detail)):
        return {
            "marker": "[REMOTE_ARTIFACT_ACCESS_READINESS_REQUIRED]",
            "reason": "remote_access_unknown_git_capability_probe_required",
            "repair_scope": "REMOTE_ARTIFACT_ACCESS_READINESS",
            "suggestion": (
                "The bundled Git binary has been verified and the exact "
                "remote Artifact is already known. UNKNOWN means untested; "
                "it is not a network denial. Prefer a narrowly scoped "
                "run_command/GIT_INSPECT GitHub access/readiness check under "
                "ordinary Tool/Scope/Network Admission. Do not run a network "
                "operation if security policy requires prior approval: "
                "instead cite that concrete Runtime preflight result. "
                "If denied or approval-required, preserve the exact evidence "
                "and request only the missing authorization. Do not assume "
                "download, write permission, repository authentication, or "
                "verified source integrity."
            ),
        }
    if (remote_reference and _SOURCE_REQUEST.search(detail)
            and not re.search(
                r"(?:github|repository|repo access|authentication|auth token)"
                r"|GitHub|私人儲存庫|遠端存取|網路授權|取得壓縮檔",
                detail, re.I,
            )):
        return {
            "marker": "[REGISTERED_SOURCE_ARTIFACT_REFERENCE_AVAILABLE]",
            "reason": "remote_artifact_reference_grounded_no_source_path_request",
            "repair_scope": "SOURCE_DISCOVERY_BOOTSTRAP",
            "missing_gap_refs": [
                row["gap_id"] for row in list(
                    resolution.get("confirmed_missing_goal_targets") or []
                )[:8]
            ],
            "suggestion": (
                "A pinned source artifact is already registered for this Goal. "
                "Do not ask for an unspecified existing local source folder. "
                "Instead evaluate whether the authenticated GitHub repository "
                "and archive acquisition capabilities are available and authorized. "
                "If missing, request the precise access/capability approval. "
                "Do not claim a local source or D4 PASS until a new source "
                "observation and content integrity check succeed."
            ),
        }
    direct_creation = category == "PROPOSE_GOAL_ACTION_FOR_ADMISSION"
    scoped_source = (category == "DISCOVER_SOURCE_WITHIN_SCOPE" and
                     any(row.get("reason") == "GROUNDED_SOURCE_CANDIDATE"
                         for row in list(resolution.get("source_candidate_reads") or [])
                         if isinstance(row, Mapping)))
    if not (direct_creation or scoped_source):
        return None
    detail = str(request.get("detail") or "")
    if not _SOURCE_REQUEST.search(detail) or _PERMISSION_REQUEST.search(detail):
        return None
    return {
        "marker": "[GOAL_PREREQUISITE_ACTION_PREFERRED]",
        "reason": ("source_not_required_for_explicit_creation_goal" if direct_creation else
                   "scoped_grounded_source_candidate_unexhausted"),
        "repair_scope": "GOAL_DIRECTED_PREREQUISITE_RESOLUTION",
        "missing_gap_refs": [
            row["gap_id"] for row in list(resolution.get("confirmed_missing_goal_targets") or [])[:8]
        ],
        "suggestion": (
            ("The target is confirmed absent, but a source candidate with "
             "current provenance is available for a scoped read. Propose the "
             "precise source Action for normal Tool/Scope Admission rather "
             "than requesting its path again. An actual permission request "
             "is still allowed." if scoped_source else
             "The target is confirmed absent and the Goal requires creation, "
             "not a source search. Propose a create Action for normal "
             "Tool/Scope/Write Admission. Never assume permission or success.")
        ),
    }


__all__ = [
    "SCHEMA", "source_requirement", "resolve_goal_prerequisites",
    "premature_source_interrupt_diagnostic",
]
