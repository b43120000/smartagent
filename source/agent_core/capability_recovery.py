"""Bounded recovery hints for model replies that falsely deny local access.

The model never receives direct host access.  It selects a canonical tool and
the Runtime performs the operation inside the already-authorized scope.  This
module only detects a likely capability misunderstanding and renders trusted
capability metadata; it never converts prose into executable authority.
"""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from .path_security import is_within
from .project_artifact_policy import SOURCE_CODE, TEXT_DATA_CONFIG, classify_project_file
from .tool_capabilities import describe_tools, get_allowed_tools
from .task_plan import TASK_PLAN_SCHEMA, canonicalize_task_plan, task_plan_schema_contract


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


def build_evidence_to_action_route(
    actions: Iterable[Mapping[str, Any]],
    authorized_paths: Iterable[str],
) -> dict[str, Any]:
    """Route Web Planner project-source reads through ``query_project``.

    ``read_file`` is attachment-backed for the Web Planner.  Once the user has
    authorized a project directory, source and text files beneath that exact
    root must instead be read through Runtime's bounded INDEX_ONLY query path.
    This function is deliberately non-executing: it returns a canonical action
    suggestion so protocol validation can require the model to correct its
    action without silently changing action identity or authority.
    """
    roots: list[Path] = []
    for raw in authorized_paths:
        try:
            candidate = Path(str(raw)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)

    grouped: dict[Path, list[dict[str, Any]]] = {}
    routed_files: list[str] = []
    for action in actions:
        if str(action.get("tool", "") or "") != "read_file":
            continue
        try:
            target = Path(str(action.get("path", "") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not target.is_file():
            continue
        artifact_class = str(classify_project_file(target).get("classification", ""))
        if artifact_class not in {SOURCE_CODE, TEXT_DATA_CONFIG}:
            continue
        matching = [root for root in roots if target != root and is_within(target, root)]
        if not matching:
            continue
        project_root = max(matching, key=lambda item: len(item.parts))
        relative = target.relative_to(project_root).as_posix()
        query = {
            "operation": "read_range",
            "path": relative,
            "start_line": 1,
            "end_line": 240,
        }
        if query not in grouped.setdefault(project_root, []):
            grouped[project_root].append(query)
        if str(target) not in routed_files:
            routed_files.append(str(target))

    suggested_actions: list[dict[str, Any]] = []
    for root, queries in grouped.items():
        for offset in range(0, len(queries), 8):
            suggested_actions.append({
                "tool": "query_project",
                "project_root": str(root),
                "queries": queries[offset:offset + 8],
                "max_bytes": 24576,
            })
    return {
        "active": bool(suggested_actions),
        "reason": "web_planner_project_read_requires_query_project" if suggested_actions else "",
        "routed_files": routed_files,
        "suggested_actions": suggested_actions,
        "fallback": (
            "Only use explicit upload_file/upload_files when bounded query_project "
            "operations cannot provide the required evidence."
        ) if suggested_actions else "",
    }


def render_evidence_to_action_guidance(context: Mapping[str, Any] | None) -> str:
    payload = dict(context or {})
    if not payload.get("active"):
        return ""
    return (
        "[WEBAGENT_EVIDENCE_TO_ACTION_ROUTE]\n"
        "Project source must stay in Runtime-held INDEX_ONLY context. Replace the rejected "
        "read_file action(s) with the canonical query_project action(s) below; use read_symbol "
        "when a symbol is known, or continue read_range from next_cursor when truncated.\n"
        "route="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n[/WEBAGENT_EVIDENCE_TO_ACTION_ROUTE]"
    )


def _json_result_payload(result: object) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    try:
        value = json.loads(str(result or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _result_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "schema", "status", "reason", "missing_fields", "current_snapshot_id",
        "snapshot_id", "base_snapshot_id", "plan_id", "plan_sha256",
        "semantic_map_revision", "scope", "required_paths", "needs_analysis_count",
        "applied", "rolled_back", "error", "submitted_schema", "expected_schema",
        "required_action", "diagnostics", "schema_contract", "canonical_repairs",
    )
    summary = {key: payload[key] for key in keys if key in payload}
    for nested_key in ("validation", "edit_plan_validation", "semantic_map_status"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            summary[nested_key] = {
                key: nested[key]
                for key in (
                    "status", "reason", "missing_fields", "current_snapshot_id",
                    "base_snapshot_id", "semantic_map_revision", "needs_analysis_count",
                )
                if key in nested
            }
    return summary


def _workspace_relative_error_path(payload: Mapping[str, Any], code: str) -> str:
    """Extract one safe workspace-relative path from a ``CODE:path`` result."""
    candidates = [
        str(payload.get("error", "") or ""),
        str(payload.get("reason", "") or ""),
    ]
    validation = payload.get("validation")
    if isinstance(validation, Mapping):
        candidates.extend([
            str(validation.get("error", "") or ""),
            str(validation.get("reason", "") or ""),
        ])
        if str(validation.get("reason", "") or "").upper() == code:
            candidates.append(f"{code}:{validation.get('path', '')}")
    if str(payload.get("reason", "") or "").upper() == code:
        candidates.append(f"{code}:{payload.get('path', '')}")
    prefix = code + ":"
    for candidate in candidates:
        if not candidate.upper().startswith(prefix):
            continue
        relative = candidate[len(prefix):].strip().replace("\\", "/").lstrip("/")
        parts = [part for part in relative.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
            return ""
        return "/".join(parts)
    return ""


def _edit_plan_content_read_action(
    workspace: str, snapshot_id: str, path: str, start_line: int = 1,
) -> dict[str, Any]:
    start = max(1, int(start_line or 1))
    action: dict[str, Any] = {
        "tool": "query_project",
        "project_root": workspace or "<exact project root>",
        "queries": [{
            "operation": "read_range",
            "path": path,
            "start_line": start,
            "end_line": start + 239,
        }],
        "max_bytes": 49152,
    }
    if snapshot_id:
        action["snapshot_id"] = snapshot_id
    return action


def _task_plan_semantic_paths(action: Mapping[str, Any]) -> list[str]:
    plan = action.get("plan")
    if not isinstance(plan, Mapping):
        return []
    paths = {
        str(path).replace("\\", "/").lstrip("/")
        for path in list(plan.get("files_to_read") or [])
        if str(path).strip()
    }
    edit_plan = plan.get("edit_plan")
    if isinstance(edit_plan, Mapping):
        for row in list(edit_plan.get("files_to_modify") or []):
            if isinstance(row, Mapping) and str(row.get("path", "") or "").strip():
                paths.add(str(row["path"]).replace("\\", "/").lstrip("/"))
    return sorted(paths)


def _resume_task_plan_action(
    action: Mapping[str, Any], payload: Mapping[str, Any], workspace: str,
) -> dict[str, Any]:
    plan = dict(action.get("plan") or {})
    semantic_status = payload.get("semantic_map_status")
    if not isinstance(semantic_status, Mapping):
        semantic_status = {}
    current_snapshot = str(
        payload.get("current_snapshot_id", "")
        or semantic_status.get("snapshot_id", "")
        or ""
    )
    current_revision = str(
        payload.get("current_semantic_map_revision", "")
        or semantic_status.get("semantic_map_revision", "")
        or ""
    )
    if current_snapshot:
        plan["base_snapshot_id"] = current_snapshot
    if current_revision:
        plan["semantic_map_revision"] = current_revision
    resumed = {
        "tool": "propose_task_plan",
        "action_id": "<fresh action_id>",
        "plan": plan,
    }
    if workspace:
        resumed["workspace"] = workspace
    return resumed


def build_result_evidence_to_action_route(
    action: Mapping[str, Any],
    result: object,
    *,
    project_root_hint: str = "",
    prior_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded, non-executing recovery metadata for rejected plan tools.

    The Runtime never invents source edits.  It only preserves known evidence,
    shows the canonical field nesting, and identifies the prerequisite action
    needed to obtain a value that is still unknown.
    """
    tool = str(action.get("tool", "") or "")
    payload = _json_result_payload(result)
    status = str(payload.get("status", "") or "").upper()
    workspace = str(
        action.get("workspace", "")
        or action.get("project_root", "")
        or project_root_hint
        or ""
    ).strip()
    prior = dict(prior_context or {})

    if tool in {"validate_edit_plan", "apply_edit_plan"}:
        validation = payload.get("validation")
        if not isinstance(validation, Mapping):
            validation = payload
        validation_status = str(validation.get("status", status) or status).upper()
        if validation_status in {"VALID", "APPLIED"}:
            return {"active": False}
        existing = dict(action.get("plan") or {})
        current_snapshot = str(
            validation.get("current_snapshot_id", "")
            or payload.get("current_snapshot_id", "")
            or ""
        )
        canonical_plan = {
            "base_snapshot_id": (
                existing.get("base_snapshot_id")
                or current_snapshot
                or "<project_sync INDEX_ONLY snapshot_id>"
            ),
            "files_to_modify": existing.get("files_to_modify") or [{
                "path": "<workspace-relative path>",
                "base_sha256": "<query_project file sha256 when available>",
                "modification_intent": "<bounded intended change>",
                "mode": "exact_replace",
                "old": "<exact existing text>",
                "new": "<replacement text>",
            }],
            "verification_commands": existing.get("verification_commands") or [
                "<bounded verification command>"
            ],
            "expected_observable_result": existing.get("expected_observable_result") or (
                "<observable acceptance result>"
            ),
            "rollback_condition": existing.get("rollback_condition") or (
                "<condition requiring rollback>"
            ),
        }
        next_actions: list[dict[str, Any]] = []
        if not current_snapshot and not existing.get("base_snapshot_id"):
            next_actions.append({
                "tool": "project_sync",
                "strategy": "INDEX_ONLY",
                "project_root": workspace or "<exact project root>",
            })
        if not existing.get("files_to_modify"):
            next_actions.append({
                "tool": "query_project",
                "project_root": workspace or "<exact project root>",
                "queries": [{
                    "operation": "read_range",
                    "path": "<workspace-relative target>",
                    "start_line": 1,
                    "end_line": 240,
                }],
            })
        canonical_action: dict[str, Any] = {
            "tool": tool,
            "action_id": "<fresh action_id>",
            "plan": canonical_plan,
        }
        if workspace:
            canonical_action["workspace"] = workspace
        missing_content_path = _workspace_relative_error_path(payload, "MISSING_CONTENT")
        if missing_content_path:
            snapshot_id = str(canonical_plan.get("base_snapshot_id", "") or "")
            return {
                "active": True,
                "reason": "edit_plan_missing_content",
                "observed": _result_summary(payload),
                "blocked_tool": "apply_edit_plan",
                "prerequisite_satisfied": False,
                "missing_content_paths": [missing_content_path],
                "completed_content_paths": [],
                "content_file_sha256": {},
                "resume_action": canonical_action,
                "canonical_action": canonical_action,
                "next_actions": [
                    _edit_plan_content_read_action(
                        workspace, snapshot_id, missing_content_path,
                    )
                ],
                "rules": [
                    "Run the exact structured query_project read_range prerequisite before emitting apply_edit_plan again.",
                    "Follow next_cursor until the missing file content is complete.",
                    "After evidence is complete, emit a fresh apply_edit_plan with content for whole_file or old/new for exact_replace.",
                    "Keep workspace, snapshot_id, path, and file_sha256 bound to Runtime evidence.",
                ],
            }
        return {
            "active": True,
            "reason": "edit_plan_requires_canonical_replan",
            "observed": _result_summary(payload),
            "required_plan_fields": [
                "base_snapshot_id", "files_to_modify", "verification_commands",
                "expected_observable_result", "rollback_condition",
            ],
            "canonical_action": canonical_action,
            "next_actions": next_actions,
            "rules": [
                "All edit-plan fields belong inside plan; only tool, action_id, workspace, and plan are top-level.",
                "Do not execute placeholder values; obtain missing source evidence first.",
                "Keep workspace bound to the exact project root used by project_sync/query_project.",
            ],
        }

    if tool == "query_project" and str(prior.get("reason", "")).startswith(
        "edit_plan_"
    ) and prior.get("blocked_tool") == "apply_edit_plan":
        expected_paths = {
            str(path).replace("\\", "/").lstrip("/")
            for path in list(prior.get("missing_content_paths") or [])
            if str(path).strip()
        }
        completed_paths = {
            str(path).replace("\\", "/").lstrip("/")
            for path in list(prior.get("completed_content_paths") or [])
            if str(path).strip()
        }
        file_sha256 = dict(prior.get("content_file_sha256") or {})
        continuation_queries: list[dict[str, Any]] = []
        for row in list(payload.get("results") or []):
            if not isinstance(row, Mapping):
                continue
            if str(row.get("operation", "") or "").lower() != "read_range":
                continue
            if str(row.get("status", "") or "").upper() != "OK":
                continue
            path = str(row.get("path", "") or "").replace("\\", "/").lstrip("/")
            if path not in expected_paths:
                continue
            digest = str(row.get("file_sha256", "") or "")
            if digest:
                file_sha256[path] = digest
            if row.get("truncated") and row.get("next_cursor"):
                start = int(row["next_cursor"])
                continuation_queries.append({
                    "operation": "read_range", "path": path,
                    "start_line": start, "end_line": start + 239,
                })
            else:
                completed_paths.add(path)
        pending_paths = sorted(expected_paths - completed_paths)
        canonical = dict(prior.get("canonical_action") or {})
        canonical_plan = dict(canonical.get("plan") or {})
        snapshot_id = str(
            action.get("snapshot_id", "")
            or canonical_plan.get("base_snapshot_id", "")
            or ""
        )
        if continuation_queries:
            next_action: dict[str, Any] = {
                "tool": "query_project",
                "project_root": workspace or "<exact project root>",
                "queries": continuation_queries[:8],
                "max_bytes": 49152,
            }
            if snapshot_id:
                next_action["snapshot_id"] = snapshot_id
            return {
                **prior,
                "active": True,
                "reason": "edit_plan_content_read_continuation",
                "observed": _result_summary(payload),
                "prerequisite_satisfied": False,
                "completed_content_paths": sorted(completed_paths),
                "content_file_sha256": file_sha256,
                "next_actions": [next_action],
            }
        if expected_paths and not pending_paths:
            return {
                **prior,
                "active": True,
                "reason": "edit_plan_source_evidence_ready",
                "observed": _result_summary(payload),
                "prerequisite_satisfied": True,
                "completed_content_paths": sorted(completed_paths),
                "content_file_sha256": file_sha256,
                "next_actions": [],
                "rules": [
                    "Source evidence is ready. Rebuild the edit plan with a fresh action_id.",
                    "For whole_file provide content; for exact_replace provide exact old and new strings.",
                    "Use the returned file_sha256 as base_sha256 and keep the same snapshot and workspace.",
                ],
            }
        return {
            **prior,
            "active": True,
            "reason": "edit_plan_content_read_incomplete",
            "observed": _result_summary(payload),
            "prerequisite_satisfied": False,
            "completed_content_paths": sorted(completed_paths),
            "content_file_sha256": file_sha256,
            "next_actions": [
                _edit_plan_content_read_action(workspace, snapshot_id, path)
                for path in pending_paths
            ][:8],
            "terminal_if_no_action": "PAUSED: missing edit-plan source content produced no usable read_range evidence",
        }

    if tool in {"propose_task_plan", "repair_task_plan", "propose_task_plan_file"}:
        if status == "PLAN_FROZEN":
            return {"active": False}
        if status == "INVALID_PLAN":
            submitted_plan = action.get("plan")
            canonical_plan, canonical_repairs = canonicalize_task_plan(submitted_plan)
            diagnostics = list(payload.get("diagnostics") or [])
            diagnostic_basis = {
                "reason": payload.get("reason", ""),
                "submitted_schema": payload.get("submitted_schema", ""),
                "expected_schema": payload.get("expected_schema", TASK_PLAN_SCHEMA),
                "diagnostics": diagnostics,
            }
            diagnostic_signature = hashlib.sha256(
                json.dumps(
                    diagnostic_basis, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            previous_signature = str(prior.get("diagnostic_signature", "") or "")
            same_diagnostic_count = (
                int(prior.get("same_diagnostic_count", 0) or 0) + 1
                if previous_signature == diagnostic_signature else 1
            )
            repair_action: dict[str, Any] = {
                "tool": "repair_task_plan",
                "action_id": "<fresh action_id>",
                "plan": canonical_plan,
            }
            if workspace:
                repair_action["workspace"] = workspace
            stalled = tool == "repair_task_plan" and same_diagnostic_count >= 2
            return {
                "active": True,
                "reason": (
                    "task_plan_repair_stalled" if stalled
                    else "task_plan_schema_repair_required"
                ),
                "runtime_state": "BLOCKED_ON_PREREQUISITE",
                "blocking_reason": "PLAN_SCHEMA_INVALID",
                "observed": _result_summary(payload),
                "expected_schema": payload.get("expected_schema", TASK_PLAN_SCHEMA),
                "schema_contract": payload.get("schema_contract") or task_plan_schema_contract(),
                "canonical_repairs": canonical_repairs,
                "diagnostic_signature": diagnostic_signature,
                "same_diagnostic_count": same_diagnostic_count,
                "blocked_tool": "propose_task_plan",
                "required_action": "repair_task_plan",
                "forbidden_actions": ["propose_task_plan", "propose_task_plan_file"],
                "prerequisite_satisfied": False,
                "next_actions": [] if stalled else [repair_action],
                "resume_action": repair_action,
                "terminal_if_no_action": (
                    "PLAN_REPAIR_STALLED: identical task-plan validation diagnostic repeated"
                    if stalled else ""
                ),
                "pause_immediately": stalled,
                "rules": [
                    "Do not resubmit propose_task_plan while the plan-repair latch is active.",
                    "Emit exactly one repair_task_plan using the Runtime-supplied canonical plan.",
                    "Only PLAN_FROZEN satisfies this prerequisite and releases the latch.",
                ],
            }
        next_actions = []
        semantic = payload.get("semantic_map_status")
        semantic_paths = list(payload.get("required_semantic_paths") or [])
        if not semantic_paths:
            semantic_paths = _task_plan_semantic_paths(action)
        resume_action = _resume_task_plan_action(action, payload, workspace)
        if status == "SEMANTIC_MAP_NOT_FRESH" or (
            isinstance(semantic, Mapping)
            and str(semantic.get("status", "") or "").upper() != "FRESH"
        ):
            next_actions.append({
                "tool": "inspect_semantic_map",
                "workspace": workspace or "<exact project root>",
                "paths": semantic_paths,
            })
        current_snapshot = str(payload.get("current_snapshot_id", "") or "")
        if status == "PLAN_BASE_MISMATCH" and current_snapshot:
            next_actions.append({
                "tool": "propose_task_plan",
                "workspace": workspace or "<exact project root>",
                "plan_patch": {"base_snapshot_id": current_snapshot},
            })
        return {
            "active": bool(status and status != "PLAN_FROZEN"),
            "reason": "task_plan_requires_prerequisite_evidence",
            "runtime_state": "BLOCKED_ON_PREREQUISITE",
            "blocking_reason": status or "TASK_PLAN_PREREQUISITE_REQUIRED",
            "observed": _result_summary(payload),
            "next_actions": next_actions,
            "semantic_map_paths": semantic_paths,
            "resume_action": resume_action,
            "rules": [
                "Do not repeat an unchanged propose_task_plan after the same rejection.",
                "Use the returned current snapshot/revision evidence in the next canonical plan.",
                "A frozen plan is executable only after status=PLAN_FROZEN returns a plan_id.",
            ],
        }

    if tool == "inspect_semantic_map":
        if status == "FRESH":
            resume = dict(prior.get("resume_action") or {})
            if resume:
                plan = dict(resume.get("plan") or {})
                if payload.get("snapshot_id"):
                    plan["base_snapshot_id"] = payload["snapshot_id"]
                if payload.get("semantic_map_revision"):
                    plan["semantic_map_revision"] = payload["semantic_map_revision"]
                resume["plan"] = plan
            return {
                "active": True,
                "reason": "semantic_map_prerequisite_satisfied",
                "observed": _result_summary(payload),
                "next_actions": [resume] if resume else [],
                "resume_action": resume,
                "semantic_map_paths": list(payload.get("required_paths") or prior.get("semantic_map_paths") or []),
                "terminal_if_no_action": "PAUSED: semantic map is fresh but no originating task plan is available",
                "rules": ["Resume the original task plan with the returned snapshot and semantic revision."],
            }
        if status in {"MISSING", "STALE"}:
            needs = [str(path) for path in list(payload.get("needs_analysis") or []) if str(path)]
            batch = needs[:8]
            queries = [
                {"operation": "read_range", "path": path, "start_line": 1, "end_line": 240}
                for path in batch
            ]
            next_actions = []
            if queries:
                next_actions.append({
                    "tool": "query_project",
                    "project_root": workspace or "<exact project root>",
                    "snapshot_id": str(payload.get("snapshot_id", "") or ""),
                    "queries": queries,
                    "max_bytes": 49152,
                })
            return {
                "active": True,
                "reason": "semantic_map_requires_bounded_source_evidence",
                "observed": _result_summary(payload),
                "next_actions": next_actions,
                "semantic_map_paths": list(payload.get("required_paths") or prior.get("semantic_map_paths") or []),
                "semantic_map_pending_paths": needs,
                "semantic_map_snapshot_id": str(payload.get("snapshot_id", "") or ""),
                "resume_action": dict(prior.get("resume_action") or {}),
                "terminal_if_no_action": "PAUSED: semantic map needs source evidence but no bounded path is available",
                "rules": [
                    "Read only the listed plan-scoped paths through query_project.",
                    "After evidence returns, emit update_semantic_map with source_sha256 and factual semantic fields.",
                    "Do not wait and do not scan unrelated project files.",
                ],
            }

    if tool == "query_project" and str(prior.get("reason", "")).startswith("semantic_map_"):
        result_rows = list(payload.get("results") or [])
        file_map = {
            str(row.get("path", "")): dict(row)
            for row in list(prior.get("semantic_map_file_templates") or [])
            if isinstance(row, Mapping) and str(row.get("path", ""))
        }
        continuation_queries = []
        for row in result_rows:
            if not isinstance(row, Mapping) or str(row.get("status", "")).upper() != "OK":
                continue
            path = str(row.get("path", "") or "")
            source_sha = str(row.get("file_sha256", "") or row.get("sha256", "") or "")
            if path and source_sha:
                file_map[path] = {
                    "path": path,
                    "source_sha256": source_sha,
                    "responsibility": "<derive from the returned source evidence>",
                    "public_symbols": [], "dependencies": [], "flows": [],
                    "invariants": [], "tests": [],
                }
            if row.get("truncated") and row.get("next_cursor") and path:
                start = int(row["next_cursor"])
                continuation_queries.append({
                    "operation": "read_range", "path": path,
                    "start_line": start, "end_line": start + 239,
                })
        files = [file_map[path] for path in sorted(file_map)]
        if continuation_queries:
            return {
                **prior,
                "active": True,
                "reason": "semantic_map_evidence_continuation_required",
                "observed": _result_summary(payload),
                "semantic_map_file_templates": files,
                "next_actions": [{
                    "tool": "query_project",
                    "project_root": workspace or "<exact project root>",
                    "snapshot_id": str(prior.get("semantic_map_snapshot_id", "") or ""),
                    "queries": continuation_queries[:8],
                    "max_bytes": 49152,
                }],
                "rules": ["Follow next_cursor until each selected source range is complete; do not upload files."],
            }
        update_action = {
            "tool": "update_semantic_map",
            "action_id": "<fresh action_id>",
            "workspace": workspace or "<exact project root>",
            "patch": {
                "base_snapshot_id": str(prior.get("semantic_map_snapshot_id", "") or ""),
                "required_paths": list(prior.get("semantic_map_paths") or []),
                "project_summary": "<preserve existing summary or provide a factual bounded summary>",
                "flows": [],
                "files": files,
            },
        }
        return {
            **prior,
            "active": True,
            "reason": "semantic_map_evidence_ready_for_update",
            "observed": _result_summary(payload),
            "next_actions": [update_action] if files else [],
            "canonical_action": update_action,
            "semantic_map_file_templates": files,
            "terminal_if_no_action": "PAUSED: query_project returned no usable semantic source evidence",
            "rules": [
                "Replace angle-bracket narrative placeholders with facts from this query result before execution.",
                "Do not change source_sha256 or widen the path set.",
            ],
        }

    if tool in {"update_semantic_map", "update_semantic_map_file"} and prior.get("resume_action"):
        paths = list(prior.get("semantic_map_paths") or [])
        return {
            **prior,
            "active": True,
            "reason": "semantic_map_update_requires_reinspection",
            "observed": _result_summary(payload),
            "next_actions": [{
                "tool": "inspect_semantic_map",
                "workspace": workspace or "<exact project root>",
                "paths": paths,
            }],
            "rules": ["Reinspect the same plan-scoped paths; resume the task plan only when status=FRESH."],
        }
    return {"active": False}


def render_result_evidence_to_action_guidance(
    context: Mapping[str, Any] | None,
) -> str:
    payload = dict(context or {})
    if not payload.get("active"):
        return ""
    return (
        "[WEBAGENT_RESULT_EVIDENCE_TO_ACTION]\n"
        "The previous tool result is authoritative Runtime evidence. Do not merely report that "
        "a schema is missing and do not repeat the rejected action unchanged. Use the bounded "
        "route below to either obtain missing evidence or emit one complete canonical action.\n"
        "route="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n[/WEBAGENT_RESULT_EVIDENCE_TO_ACTION]"
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
    "build_evidence_to_action_route",
    "build_result_evidence_to_action_route",
    "build_capability_recovery_context",
    "detect_false_local_access_refusal",
    "render_evidence_to_action_guidance",
    "render_result_evidence_to_action_guidance",
    "render_capability_recovery_guidance",
]
