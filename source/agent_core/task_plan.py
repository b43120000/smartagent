#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .aggregated_verification import run_aggregated_verification
from .batch_apply import apply_edit_plan, _write_atomic
from .edit_plan_contract import validate_edit_plan
from .project_sync import inspect_project_scope
from .path_security import authorize_path, reject_reparse_components
from .semantic_map import (
    inspect_semantic_map, load_semantic_map, semantic_map_markdown_path,
    semantic_map_path, semantic_target_paths, update_semantic_map,
)

TASK_PLAN_SCHEMA = "TASK_PLAN_V1"
TASK_PLAN_SCHEMA_ALIASES = frozenset({"SMARTAGENT_INSTANCE_AWARE_PLAN_V1"})
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


def task_plan_schema_contract() -> dict:
    """Return the single model/runtime contract for inline task plans."""
    return {
        "schema": TASK_PLAN_SCHEMA,
        "accepted_aliases_for_repair": sorted(TASK_PLAN_SCHEMA_ALIASES),
        "required_fields": sorted(_REQUIRED),
        "field_types": {
            "schema": "string",
            "base_snapshot_id": "string",
            "semantic_map_revision": "string",
            "goal": "string",
            "affected_flows": "array[string]",
            "files_to_read": "array[string]",
            "edit_plan": "object",
            "verification_commands": "array[string]",
            "acceptance_criteria": "array[string]",
            "rollback_condition": "string",
            "post_change_semantic": "array[object]",
        },
    }


def _invalid_plan(reason: str, *, diagnostics: list[dict] | None = None,
                  **fields: object) -> dict:
    result = {
        "status": "INVALID_PLAN",
        "reason": reason,
        "expected_schema": TASK_PLAN_SCHEMA,
        "required_action": "repair_task_plan",
        "schema_contract": task_plan_schema_contract(),
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    result.update(fields)
    return result


def canonicalize_task_plan(plan: object) -> tuple[dict, list[dict]]:
    """Apply only deterministic, lossless repairs to a submitted plan."""
    if not isinstance(plan, dict):
        return {}, []
    normalized = dict(plan)
    repairs: list[dict] = []
    submitted_schema = normalized.get("schema")
    if isinstance(submitted_schema, str) and submitted_schema in TASK_PLAN_SCHEMA_ALIASES:
        normalized["schema"] = TASK_PLAN_SCHEMA
        repairs.append({
            "path": "$.schema", "code": "SCHEMA_ALIAS_CANONICALIZED",
            "from": submitted_schema, "to": TASK_PLAN_SCHEMA,
        })
    rollback = normalized.get("rollback_condition")
    if isinstance(rollback, list) and all(isinstance(item, str) for item in rollback):
        normalized["rollback_condition"] = " OR ".join(
            item.strip() for item in rollback if item.strip()
        )
        repairs.append({
            "path": "$.rollback_condition", "code": "ARRAY_JOINED_TO_STRING",
        })
    edit_plan = normalized.get("edit_plan")
    if isinstance(edit_plan, list) and all(isinstance(item, dict) for item in edit_plan):
        wrapped_edit_plan = {"files_to_modify": list(edit_plan)}
        acceptance = normalized.get("acceptance_criteria")
        if isinstance(acceptance, list) and all(isinstance(item, str) for item in acceptance):
            observable = " AND ".join(item.strip() for item in acceptance if item.strip())
            if observable:
                wrapped_edit_plan["expected_observable_result"] = observable
        normalized["edit_plan"] = wrapped_edit_plan
        repairs.append({
            "path": "$.edit_plan", "code": "ARRAY_WRAPPED_AS_FILES_TO_MODIFY",
        })
    return normalized, repairs


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
        return _invalid_plan(
            "PLAN_NOT_OBJECT",
            diagnostics=[{"path": "$", "code": "TYPE_MISMATCH", "expected_type": "object"}],
        )
    missing = sorted(_REQUIRED - set(plan))
    if missing:
        return _invalid_plan(
            "MISSING_FIELDS", missing_fields=missing,
            diagnostics=[{"path": f"$.{field}", "code": "MISSING_REQUIRED_FIELD"} for field in missing],
        )
    if plan.get("schema") != TASK_PLAN_SCHEMA:
        return _invalid_plan(
            "SCHEMA_MISMATCH",
            submitted_schema=plan.get("schema"),
            diagnostics=[{
                "path": "$.schema", "code": "UNSUPPORTED_SCHEMA",
                "expected": TASK_PLAN_SCHEMA, "actual": plan.get("schema"),
            }],
        )
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
        if not isinstance(plan.get("edit_plan"), dict):
            raise ValueError("task_plan_invalid_edit_plan")
        edit_plan = dict(plan["edit_plan"])
        edit_plan["base_snapshot_id"] = plan["base_snapshot_id"]
        edit_plan["verification_commands"] = verification
        edit_plan["rollback_condition"] = rollback
        edit_check = validate_edit_plan(root, edit_plan)
        if edit_check.get("status") != "VALID":
            return _invalid_plan(
                "INVALID_EDIT_PLAN",
                edit_plan_validation=edit_check,
                diagnostics=[{
                    "path": "$.edit_plan",
                    "code": str(edit_check.get("reason", "INVALID_EDIT_PLAN") or "INVALID_EDIT_PLAN"),
                    "detail": edit_check,
                }],
            )
        edit_plan['files_to_modify'] = edit_check['files_to_modify']
        semantic_required_paths = sorted(set(reads) | {
            str(row.get("path", "") or "")
            for row in edit_check.get("files_to_modify", [])
            if str(row.get("path", "") or "")
        })
        # Non-source files still need byte/snapshot checks, not semantic records.
        source_targets = semantic_target_paths(root, snapshot)
        semantic_required_paths = sorted(set(semantic_required_paths) & source_targets)
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
        required_post = {row["path"] for row in edit_check["files_to_modify"] if row["path"] in source_targets or Path(row["path"]).name == "CMakeLists.txt" or Path(row["path"]).suffix.lower() in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".java", ".kt", ".kts", ".py", ".js", ".ts", ".rs", ".go", ".cmake", ".gradle"}}
        if not required_post <= post_paths:
            missing_paths = sorted(required_post - post_paths)
            return _invalid_plan(
                "MISSING_POST_CHANGE_SEMANTIC", missing_paths=missing_paths,
                diagnostics=[{
                    "path": "$.post_change_semantic", "code": "MISSING_PATH_ENTRIES",
                    "missing_paths": missing_paths,
                }],
            )
    except ValueError as exc:
        error_code = str(exc)
        diagnostic_path = "$"
        for field in (
            "post_change_semantic", "rollback_condition", "verification_command",
            "affected_flows", "files_to_read", "acceptance_criteria", "goal", "edit_plan",
        ):
            if field in error_code:
                diagnostic_path = "$." + (
                    "verification_commands" if field == "verification_command" else field
                )
                break
        return _invalid_plan(
            error_code,
            diagnostics=[{"path": diagnostic_path, "code": error_code}],
        )
    normalized = dict(plan)
    normalized.update(goal=goal, affected_flows=affected, files_to_read=reads, acceptance_criteria=acceptance,
                      verification_commands=verification, rollback_condition=rollback, edit_plan=edit_plan, post_change_semantic=post_semantic)
    if request_scope:
        normalized["request_scope"] = request_scope
    return {"status": "VALID", "plan": normalized}


def freeze_task_plan(workspace: str | Path, plan: dict, request_scope: dict | None = None) -> dict:
    if not isinstance(plan, dict):
        return validate_task_plan(workspace, plan)
    plan = dict(plan)
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
    reject_reparse_components(path, Path(workspace).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            valid = (isinstance(existing, dict)
                and existing.get("plan_sha256") == plan_sha256
                and existing.get("plan_id") == plan_id
                and _hash(_plan_hash_basis(existing)) == plan_sha256)
        except (ValueError, OSError):
            valid = False
        if not valid:
            return {"status": "PLAN_ID_CONFLICT", "plan_id": plan_id}
        if existing.get("state") != "FROZEN":
            return {"status": "PLAN_NOT_EXECUTABLE", "plan_id": plan_id,
                    "reason": "frozen_plan_invalid_state"}
    else:
        # Never truncate a predictable temp path: it could be a hardlink.
        fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            reject_reparse_components(path, Path(workspace).resolve())
            if os.name == 'nt':
                os.rename(tmp, path)  # Exclusive publication on Windows.
            else:
                os.link(tmp, path)
        except FileExistsError:
            return freeze_task_plan(workspace, plan, request_scope=request_scope)
        finally:
            tmp.unlink(missing_ok=True)
    return {"status": "PLAN_FROZEN", "plan_id": plan_id, "plan_sha256": frozen["plan_sha256"], "path": str(path)}


def repair_task_plan(workspace: str | Path, plan: dict,
                     request_scope: dict | None = None) -> dict:
    """Canonicalize one rejected plan and freeze it when validation succeeds."""
    normalized, repairs = canonicalize_task_plan(plan)
    result = freeze_task_plan(workspace, normalized, request_scope=request_scope)
    result["canonical_repairs"] = repairs
    if result.get("status") != "PLAN_FROZEN":
        result.setdefault("required_action", "repair_task_plan")
        result.setdefault("expected_schema", TASK_PLAN_SCHEMA)
        result.setdefault("schema_contract", task_plan_schema_contract())
    return result


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
    if not isinstance(plan_id, str) or not re.fullmatch(r'PLAN-[0-9A-F]{20}', plan_id):
        raise ValueError('frozen_plan_invalid_id')
    root = task_plan_root(workspace)
    path = root / f"{plan_id}.json"
    reject_reparse_components(path, Path(workspace).resolve())
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
    if bool(plan_scope) != bool(expected_scope) or (plan_scope and (
            any(plan_scope[field] != expected_scope[field] for field in _REQUEST_SCOPE_FIELDS[:4])
            or expected_scope['continuation_seq'] < plan_scope['continuation_seq'])):
        raise ValueError("frozen_plan_request_scope_mismatch")
    return path, value


def _restore_frozen_changes(root: Path, originals: dict, expected: dict,
                            directories: list, authorize_target=None) -> list[dict]:
    """Restore only known transaction bytes; never overwrite an unknown change."""
    errors = []
    for target, before in reversed(list(originals.items())):
        try:
            reject_reparse_components(target, root)
            if authorize_target:
                authorize_target(target)
            current = target.read_bytes() if target.exists() else None
            if current == before:
                continue
            if target not in expected or current != expected[target]:
                raise ValueError('ROLLBACK_TARGET_CHANGED_OR_UNCONFIRMED')
            if before is None:
                target.unlink()
                if target.exists():
                    raise ValueError('ROLLBACK_DELETE_READBACK_FAILED')
            else:
                _write_atomic(root, target, before, expected=current)
                if target.read_bytes() != before:
                    raise ValueError('ROLLBACK_READBACK_MISMATCH')
        except Exception as exc:
            errors.append({'path':target.relative_to(root).as_posix(),
                           'error':type(exc).__name__+':'+str(exc)})
    for directory in sorted(set(directories), key=lambda p: len(p.parts), reverse=True):
        try:
            reject_reparse_components(directory, root)
            if directory.exists():
                directory.rmdir()  # Empty, newly-created directories only.
        except Exception as exc:
            errors.append({'path':directory.relative_to(root).as_posix(),
                           'error':type(exc).__name__+':'+str(exc)})
    return errors


def execute_frozen_task_plan(workspace: str | Path, plan_id: str, timeout: int = 120,
                             request_scope: dict | None = None, *, authorize_target=None) -> dict:
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
    originals, expected, directories = {}, {}, []
    # Preflight every canonical target before the first side effect.
    try:
        targets = [(item, authorize_path(item['path'], [root], base=root, forbid_glob=True)) for item in files]
        for item, target in targets:
            reject_reparse_components(target, root)
            if authorize_target:
                authorize_target(target)
            before = None if item.get('create') else target.read_bytes()
            originals[target] = before
            encoding = item.get('encoding', 'utf-8')
            after = (item['content'] if item.get('mode', 'whole_file') == 'whole_file'
                     else before.decode(encoding).replace(item['old'], item['new'], 1))
            expected[target] = after.encode(encoding)
            cursor = target.parent
            while cursor != root and not cursor.exists():
                directories.append(cursor)
                cursor = cursor.parent
        if authorize_target:
            authorize_target(path)
        plan_bytes = path.read_bytes()
    except Exception as exc:
        return {'status':'PLAN_NOT_EXECUTABLE','reason':type(exc).__name__+':'+str(exc),
                'executed':False,'rolled_back':False}
    applied, verification, semantic_result = {}, {}, {'status':'NOT_REQUIRED'}
    uncertain_metadata = []
    try:
        applied = apply_edit_plan(root, check['plan']['edit_plan'], authorize_target=authorize_target)
        if applied.get('status') != 'APPLIED':
            uncertain = applied.get('status') == 'ROLLBACK_INCOMPLETE'
            return {'status':'ROLLBACK_INCOMPLETE' if uncertain else 'REPAIR_REQUIRED',
                    'plan_id':plan_id,'apply':applied,'rolled_back':bool(applied.get('rolled_back')),
                    'executed':applied.get('status') in {'ROLLED_BACK','ROLLBACK_INCOMPLETE'},
                    'possible_partial_execution':uncertain,
                    'rollback_errors':applied.get('rollback_errors',[])}
        verification = run_aggregated_verification(root, check['plan']['verification_commands'], timeout=timeout)
        if verification.get('status') != 'PASS':
            raise ValueError('FROZEN_PLAN_VERIFICATION_NOT_PASS')
        rows = check['plan']['post_change_semantic']
        if rows:
            current = inspect_project_scope(root, verify_cached_content=True)
            hashes = {row['path']:row['sha256'] for row in current.get('files',[])}
            semantic_before = load_semantic_map(root)
            semantic_files = (semantic_map_path(root), semantic_map_markdown_path(root))
            for metadata in semantic_files:
                reject_reparse_components(metadata, root)
                if authorize_target:
                    authorize_target(metadata)
                # Existing predictable temps must never be followed by the old writer.
                if os.path.lexists(metadata.with_name(metadata.name+'.tmp')):
                    raise ValueError('SEMANTIC_TEMP_ALREADY_EXISTS')
                originals[metadata] = metadata.read_bytes() if metadata.exists() else None
            uncertain_metadata = list(semantic_files)
            semantic_result = update_semantic_map(root, {
                'base_snapshot_id':current['snapshot_id'], 'required_paths':[row['path'] for row in rows],
                'project_summary':semantic_before.get('project_summary',''), 'flows':semantic_before.get('flows',[]),
                'files':[{**row,'source_sha256':hashes[row['path']]} for row in rows],
            })
            for metadata in semantic_files:
                expected[metadata] = metadata.read_bytes() if metadata.exists() else None
            uncertain_metadata = []
            if semantic_result.get('status') != 'UPDATED':
                raise ValueError('FROZEN_PLAN_SEMANTIC_UPDATE_NOT_PASS')
        completed = {**plan, 'state':'COMPLETED', 'verification_status':'PASS'}
        originals[path] = plan_bytes
        expected[path] = json.dumps(completed, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
        if authorize_target:
            authorize_target(path)
        _write_atomic(root, path, expected[path], expected=plan_bytes)
        return {'status':'PASS','plan_id':plan_id,'applied':applied.get('applied',[]),
                'apply':applied,'verification':verification,'semantic_update':semantic_result,
                'executed':True,'rolled_back':False}
    except Exception as exc:
        errors = _restore_frozen_changes(root, originals, expected, directories, authorize_target)
        errors.extend({'path':p.relative_to(root).as_posix(), 'error':'SEMANTIC_PUBLICATION_UNCONFIRMED'}
                      for p in uncertain_metadata)
        return {'status':'ROLLBACK_INCOMPLETE' if errors else 'REPAIR_REQUIRED',
                'plan_id':plan_id,'error':type(exc).__name__+':'+str(exc),
                'apply':applied,'verification':verification,'semantic_update':semantic_result,
                'executed':True,'rolled_back':not errors,'rollback_errors':errors,
                'possible_partial_execution':bool(errors)}
