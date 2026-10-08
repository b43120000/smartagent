#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .aggregated_verification import run_aggregated_verification
from .batch_apply import apply_edit_plan
from .edit_plan_contract import validate_edit_plan
from .project_sync import inspect_project_scope
from .semantic_map import (
    inspect_semantic_map, load_semantic_map, semantic_map_markdown_path,
    semantic_map_path, semantic_target_paths, update_semantic_map,
)

TASK_PLAN_SCHEMA = "TASK_PLAN_V1"
TASK_PLAN_FILE_MAX_BYTES = 32 * 1024 * 1024
_REQUIRED = {
    "schema", "base_snapshot_id", "semantic_map_revision", "goal", "affected_flows",
    "files_to_read", "edit_plan", "verification_commands", "acceptance_criteria", "rollback_condition",
    "post_change_semantic",
}
_REQUEST_SCOPE_FIELDS = (
    "request_id", "task_id", "task_epoch", "intent_digest",
    "request_phase", "continuation_seq",
)


def task_plan_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".agents" / "task_plans"


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _normalized_request_scope(value: object) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or set(value) != set(_REQUEST_SCOPE_FIELDS):
        raise ValueError("task_plan_invalid_request_scope_fields")
    scope = {field: value.get(field) for field in _REQUEST_SCOPE_FIELDS}
    for field in _REQUEST_SCOPE_FIELDS[:-1]:
        if not isinstance(scope[field], str):
            raise ValueError(f"task_plan_invalid_request_scope_{field}")
    if not scope["request_id"] or not scope["task_epoch"] or not scope["intent_digest"]:
        raise ValueError("task_plan_incomplete_request_scope")
    if type(scope["continuation_seq"]) is not int or scope["continuation_seq"] < 1:
        raise ValueError("task_plan_invalid_request_scope_continuation_seq")
    return scope


def _plan_hash_basis(plan: dict) -> dict:
    return {
        key: value for key, value in plan.items()
        if key not in {"plan_id", "plan_sha256", "state", "verification_status"}
    }


def _strings(value: object, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > 128 or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"task_plan_invalid_{field}")
    return [item.strip() for item in value]


def validate_task_plan(workspace: str | Path, plan: dict) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not isinstance(plan, dict):
        return {"status": "INVALID_PLAN", "reason": "PLAN_NOT_OBJECT"}
    missing = sorted(_REQUIRED - set(plan))
    if missing:
        return {"status": "INVALID_PLAN", "reason": "MISSING_FIELDS", "missing_fields": missing}
    if plan.get("schema") != TASK_PLAN_SCHEMA:
        return {"status": "INVALID_PLAN", "reason": "SCHEMA_MISMATCH"}
    snapshot = inspect_project_scope(root)
    if plan.get("base_snapshot_id") != snapshot["snapshot_id"]:
        return {"status": "PLAN_BASE_MISMATCH", "current_snapshot_id": snapshot["snapshot_id"]}
    try:
        request_scope = _normalized_request_scope(plan.get("request_scope"))
        goal = str(plan.get("goal", "")).strip()
        if not goal:
            raise ValueError("task_plan_invalid_goal")
        affected = _strings(plan.get("affected_flows"), "affected_flows", allow_empty=True)
        reads = _strings(plan.get("files_to_read"), "files_to_read", allow_empty=True)
        acceptance = _strings(plan.get("acceptance_criteria"), "acceptance_criteria")
        verification = _strings(plan.get("verification_commands"), "verification_commands")
        if any(len(command) > 1200 or "\x00" in command for command in verification):
            raise ValueError("task_plan_invalid_verification_command")
        post_semantic = plan.get("post_change_semantic")
        if not isinstance(post_semantic, list) or len(post_semantic) > 20000:
            raise ValueError("task_plan_invalid_post_change_semantic")
        post_paths = set()
        for row in post_semantic:
            if not isinstance(row, dict):
                raise ValueError("task_plan_invalid_post_change_semantic_entry")
            rel = str(row.get("path", "")).replace("\\", "/").lstrip("/")
            responsibility = str(row.get("responsibility", "")).strip()
            if not rel or not responsibility or len(responsibility) > 16000 or rel in post_paths:
                raise ValueError(f"task_plan_invalid_post_change_semantic_entry:{rel}")
            post_paths.add(rel)
            for key in ("public_symbols", "dependencies", "flows", "invariants", "tests"):
                _strings(row.get(key, []), f"post_change_semantic_{key}", allow_empty=True)
        rollback = str(plan.get("rollback_condition", "")).strip()
        if not rollback:
            raise ValueError("task_plan_invalid_rollback_condition")
        for rel in reads:
            target = root / rel.replace("\\", "/").lstrip("/")
            if not _inside(root, target) or not target.is_file():
                raise ValueError(f"task_plan_invalid_read_path:{rel}")
        edit_plan = dict(plan.get("edit_plan") or {})
        edit_plan["base_snapshot_id"] = plan["base_snapshot_id"]
        edit_plan["verification_commands"] = verification
        edit_plan["rollback_condition"] = rollback
        edit_check = validate_edit_plan(root, edit_plan)
        if edit_check.get("status") != "VALID":
            return {"status": "INVALID_EDIT_PLAN", "edit_plan_validation": edit_check}
        semantic_required_paths = sorted(set(reads) | {
            str(row.get("path", "") or "")
            for row in edit_check.get("files_to_modify", [])
            if str(row.get("path", "") or "")
        })
        semantic_required_paths = sorted(
            semantic_target_paths(root, snapshot, semantic_required_paths)
        )
        if semantic_required_paths:
            semantic = inspect_semantic_map(root, semantic_required_paths)
            if semantic["status"] != "FRESH":
                return {
                    "status": "SEMANTIC_MAP_NOT_FRESH",
                    "semantic_map_status": semantic,
                    "required_semantic_paths": semantic_required_paths,
                }
            if plan.get("semantic_map_revision") != semantic["semantic_map_revision"]:
                return {
                    "status": "SEMANTIC_MAP_REVISION_MISMATCH",
                    "current_semantic_map_revision": semantic["semantic_map_revision"],
                    "required_semantic_paths": semantic_required_paths,
                }
        source_targets = semantic_target_paths(root, snapshot)
        required_post = {row["path"] for row in edit_check["files_to_modify"] if row["path"] in source_targets or Path(row["path"]).name == "CMakeLists.txt" or Path(row["path"]).suffix.lower() in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".java", ".kt", ".kts", ".py", ".js", ".ts", ".rs", ".go", ".cmake", ".gradle"}}
        if not required_post <= post_paths:
            return {"status": "INVALID_PLAN", "reason": "MISSING_POST_CHANGE_SEMANTIC", "missing_paths": sorted(required_post - post_paths)}
    except ValueError as exc:
        return {"status": "INVALID_PLAN", "reason": str(exc)}
    normalized = dict(plan)
    normalized.update(goal=goal, affected_flows=affected, files_to_read=reads, acceptance_criteria=acceptance,
                      verification_commands=verification, rollback_condition=rollback, edit_plan=edit_plan, post_change_semantic=post_semantic)
    if request_scope:
        normalized["request_scope"] = request_scope
    return {"status": "VALID", "plan": normalized}


def freeze_task_plan(workspace: str | Path, plan: dict, request_scope: dict | None = None) -> dict:
    plan = dict(plan or {})
    try:
        trusted_scope = _normalized_request_scope(request_scope)
    except ValueError as exc:
        return {"status": "INVALID_PLAN", "reason": str(exc)}
    supplied_scope = plan.get("request_scope")
    if trusted_scope:
        if supplied_scope not in (None, {}) and supplied_scope != trusted_scope:
            return {"status": "PLAN_SCOPE_MISMATCH"}
        plan["request_scope"] = trusted_scope
    check = validate_task_plan(workspace, plan)
    if check.get("status") != "VALID":
        return check
    normalized = check["plan"]
    plan_sha256 = _hash(_plan_hash_basis(normalized))
    plan_id = "PLAN-" + plan_sha256[:20].upper()
    frozen = {**normalized, "plan_id": plan_id, "plan_sha256": plan_sha256, "state": "FROZEN"}
    path = task_plan_root(workspace) / f"{plan_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("plan_sha256") != frozen["plan_sha256"]:
            return {"status": "PLAN_ID_CONFLICT", "plan_id": plan_id}
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    return {"status": "PLAN_FROZEN", "plan_id": plan_id, "plan_sha256": frozen["plan_sha256"], "path": str(path)}


def freeze_task_plan_file(workspace: str | Path, path: str, expected_sha256: str = "",
                          request_scope: dict | None = None) -> dict:
    root = Path(workspace).expanduser().resolve()
    source = Path(path).expanduser().resolve()
    if not _inside(root, source) or not source.is_file():
        return {"status": "INVALID_PLAN_FILE", "reason": "PATH_OUTSIDE_WORKSPACE_OR_MISSING"}
    raw = source.read_bytes()
    if len(raw) > TASK_PLAN_FILE_MAX_BYTES:
        return {"status": "INVALID_PLAN_FILE", "reason": "FILE_TOO_LARGE", "max_bytes": TASK_PLAN_FILE_MAX_BYTES}
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        return {"status": "INVALID_PLAN_FILE", "reason": "SHA256_MISMATCH", "actual_sha256": digest}
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"status": "INVALID_PLAN_FILE", "reason": f"JSON_DECODE:{type(exc).__name__}"}
    return freeze_task_plan(root, value, request_scope=request_scope)


def _load_frozen(workspace: str | Path, plan_id: str,
                 request_scope: dict | None = None) -> tuple[Path, dict]:
    root = task_plan_root(workspace)
    path = root / f"{plan_id}.json"
    if not _inside(root, path) or not path.is_file():
        raise ValueError("frozen_plan_not_found")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("state") != "FROZEN" or value.get("plan_id") != plan_id:
        raise ValueError("frozen_plan_invalid_state")
    actual_sha256 = _hash(_plan_hash_basis(value))
    if value.get("plan_sha256") != actual_sha256:
        raise ValueError("frozen_plan_hash_mismatch")
    if plan_id != "PLAN-" + actual_sha256[:20].upper():
        raise ValueError("frozen_plan_id_mismatch")
    expected_scope = _normalized_request_scope(request_scope)
    plan_scope = _normalized_request_scope(value.get("request_scope"))
    if plan_scope != expected_scope:
        raise ValueError("frozen_plan_request_scope_mismatch")
    return path, value


def execute_frozen_task_plan(workspace: str | Path, plan_id: str, timeout: int = 120,
                             request_scope: dict | None = None) -> dict:
    root = Path(workspace).expanduser().resolve()
    if type(timeout) is not int or not 1 <= timeout <= 600:
        return {"status": "PLAN_NOT_EXECUTABLE", "reason": "INVALID_TIMEOUT"}
    try:
        path, plan = _load_frozen(root, plan_id, request_scope=request_scope)
    except Exception as exc:
        return {"status": "PLAN_NOT_EXECUTABLE", "reason": str(exc)}
    check = validate_task_plan(root, plan)
    if check.get("status") != "VALID":
        return {"status": "PLAN_NOT_EXECUTABLE", "validation": check}
    files = check["plan"]["edit_plan"]["files_to_modify"]
    backups: dict[Path, bytes] = {}
    created: list[Path] = []
    for item in files:
        target = root / item["path"]
        if target.exists():
            backups[target] = target.read_bytes()
        else:
            created.append(target)
    applied = apply_edit_plan(root, check["plan"]["edit_plan"])
    if applied.get("status") != "APPLIED":
        return {"status": "REPAIR_REQUIRED", "plan_id": plan_id, "apply": applied, "rolled_back": bool(applied.get("rolled_back"))}
    verification = run_aggregated_verification(root, check["plan"]["verification_commands"], timeout=timeout)
    if verification.get("status") != "PASS":
        for target, data in backups.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for target in created:
            if target.exists():
                target.unlink()
        return {"status": "REPAIR_REQUIRED", "plan_id": plan_id, "apply": applied, "verification": verification, "rolled_back": True}
    current = inspect_project_scope(root)
    hashes = {row["path"]: row["sha256"] for row in current.get("files", [])}
    semantic_before = load_semantic_map(root)
    semantic_files = (semantic_map_path(root), semantic_map_markdown_path(root))
    semantic_backups = {item: item.read_bytes() for item in semantic_files if item.is_file()}
    semantic_rows = []
    for row in check["plan"]["post_change_semantic"]:
        if row["path"] in hashes:
            semantic_rows.append({**row, "source_sha256": hashes[row["path"]]})
    semantic_result = update_semantic_map(root, {
        "base_snapshot_id": current["snapshot_id"],
        "project_summary": semantic_before.get("project_summary", ""),
        "flows": semantic_before.get("flows", []),
        "files": semantic_rows,
    })
    if semantic_result.get("status") != "UPDATED":
        for target, data in backups.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for target in created:
            if target.exists():
                target.unlink()
        for semantic_file in semantic_files:
            if semantic_file in semantic_backups:
                semantic_file.write_bytes(semantic_backups[semantic_file])
            elif semantic_file.exists():
                semantic_file.unlink()
        return {"status": "REPAIR_REQUIRED", "plan_id": plan_id, "apply": applied, "verification": verification, "semantic_update": semantic_result, "rolled_back": True}
    completed = {**plan, "state": "COMPLETED", "verification_status": "PASS"}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(completed, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return {"status": "PASS", "plan_id": plan_id, "applied": applied.get("applied", []), "verification": verification, "semantic_update": semantic_result, "rolled_back": False}
