#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .project_ledger import append_project_event
from .project_sync import inspect_project_scope, is_ignored_project_path
from .path_security import canonicalize_path, PathSecurityError

SEMANTIC_MAP_SCHEMA = "PROJECT_SEMANTIC_MAP_V1"
_SOURCE_LANGUAGES = {
    "c", "c-cpp-header", "cpp", "cpp-header", "java", "kotlin", "python",
    "javascript", "typescript", "rust", "go", "cmake", "gradle",
}
SEMANTIC_PATCH_FILE_MAX_BYTES = 64 * 1024 * 1024


def _snapshot_v5_identity(snapshot: dict, files: list, git_status: list) -> str:
    """Exact v5 identity; schema upgrades fail closed at the caller."""
    head = snapshot.get('git_head', '')
    basis = {'schema_version':5,
        'files':[{'path':r['path'], 'size_bytes':r['size_bytes'],
                  'content_identity':r['content_identity']} for r in files],
        'git_head':head, 'git_status':git_status}
    manifest = hashlib.sha256(json.dumps(basis,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
    return hashlib.sha256((f'5\n{manifest}\n{head}\n'+'\n'.join(git_status)).encode()).hexdigest()


def _patch_only_snapshot(workspace: Path, source: Path, digest: str, base_id: str) -> dict | None:
    """Prove the old snapshot equals current state minus this one JSON input.

    Nothing is excluded from normal indexing. This is a file-import proof,
    never an allowance for source changes or arbitrary JSON files.
    """
    if source.suffix.lower() != '.json':
        return None
    current = inspect_project_scope(workspace, verify_cached_content=True)
    status = current.get('git_status', [])
    # Public snapshots truncate status at 200; never reason from a partial list.
    if current.get('schema_version') != 5 or len(status) >= 200:
        return None
    files = current['files']
    if _snapshot_v5_identity(current, files, status) != current['snapshot_id']:
        return None
    relative = source.relative_to(workspace).as_posix()
    matches = [r for r in files if r['path'] == relative]
    if len(matches) != 1 or matches[0].get('sha256') != digest:
        return None
    filtered_status = []
    # Git core.quotePath may emit UTF-8 bytes as C-style octal escapes.
    # Compare only encodings of this exact known path, never decode arbitrary
    # status text into a broader path match.
    quoted = '"' + ''.join(f'\\{byte:03o}' if byte >= 128 else chr(byte)
                           for byte in relative.encode('utf-8')) + '"'
    artifact_names = {relative, json.dumps(relative, ensure_ascii=False), quoted}
    for line in status:
        if line.startswith('?? ') and line[3:] in artifact_names:
            continue
        filtered_status.append(line)
    remaining = [r for r in files if r['path'] != relative]
    if _snapshot_v5_identity(current, remaining, filtered_status) != base_id:
        return None
    return {'original_base_snapshot_id':base_id,
            'verified_current_snapshot_id':current['snapshot_id'],
            'artifact_path':relative, 'artifact_sha256':digest,
            'rule':'EXACT_SNAPSHOT_WITHOUT_SINGLE_PATCH_FILE'}


def semantic_map_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".agents" / "project_context"


def semantic_map_path(workspace: str | Path) -> Path:
    return semantic_map_root(workspace) / "semantic_map.json"


def semantic_map_markdown_path(workspace: str | Path) -> Path:
    return semantic_map_root(workspace) / "PROJECT_SEMANTIC_MAP.md"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load(workspace: str | Path) -> dict:
    try:
        value = json.loads(semantic_map_path(workspace).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if (not isinstance(value, dict) or value.get('schema') != SEMANTIC_MAP_SCHEMA
        or not isinstance(value.get('files'), list)
        or not all(isinstance(row, dict) and isinstance(row.get('path'), str)
                   and isinstance(row.get('source_sha256'), str) for row in value['files'])):
        raise ValueError('semantic_map_invalid_stored_map')
    return value


def _normalized_required_paths(snapshot: dict, required_paths: object) -> set[str] | None:
    if required_paths is None:
        return None
    if not isinstance(required_paths, (list, tuple, set)):
        raise ValueError("semantic_map_invalid_required_paths")
    root = Path(str(snapshot.get("workspace_root", "") or ".")).expanduser().resolve()
    normalized: set[str] = set()
    for raw in required_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"semantic_map_path_outside_workspace:{text}") from exc
        else:
            parts = Path(text.replace("\\", "/")).parts
            if ".." in parts:
                raise ValueError(f"semantic_map_path_outside_workspace:{text}")
            text = Path(*parts).as_posix().lstrip("/")
        if text:
            normalized.add(text)
    return normalized


def _targets(snapshot: dict, required_paths: object = None) -> dict[str, dict]:
    build = set(snapshot.get("build_files", []))
    required = _normalized_required_paths(snapshot, required_paths)
    targets = {
        str(row["path"]): row
        for row in snapshot.get("files", [])
        if not is_ignored_project_path(str(row.get("path", "")))
        and (row.get("language") in _SOURCE_LANGUAGES or row.get("path") in build)
        and (required is None or str(row.get("path", "")) in required)
    }
    unknown = (required or set()) - {str(row.get('path', '')) for row in snapshot.get('files', [])}
    if unknown:
        raise ValueError('semantic_map_scope_mismatch:unknown_requested_paths:' + ','.join(sorted(unknown)))
    if required and not targets:
        raise ValueError(
            "semantic_map_scope_mismatch:no_requested_path_resolved:"
            + ",".join(sorted(required))
        )
    return targets


def semantic_target_paths(
    workspace: str | Path, snapshot: dict | None = None,
    required_paths: object = None,
) -> set[str]:
    return set(_targets(snapshot or inspect_project_scope(workspace), required_paths))


def _revision(value: dict) -> str:
    basis = {k: value.get(k) for k in ("schema", "snapshot_id", "project_summary", "flows", "files")}
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def inspect_semantic_map(workspace: str | Path, required_paths: object = None) -> dict:
    snapshot = inspect_project_scope(workspace, verify_cached_content=True)
    targets = _targets(snapshot, required_paths)
    current = _load(workspace)
    records = {
        str(row.get("path", "")): row
        for row in current.get("files", [])
        if isinstance(row, dict) and str(row.get("path", ""))
    }
    missing = sorted(path for path in targets if path not in records)
    stale = sorted(
        path for path, source in targets.items()
        if path in records and records[path].get("source_sha256") != source.get("sha256")
    )
    # A plan-scoped inspection asks whether its own inputs are covered.  Extra
    # records from other plans are valid shared knowledge, not deletions.
    deleted = (
        [] if required_paths is not None
        else sorted(path for path in records if path not in targets)
    )
    valid = bool(current) and current.get("schema") == SEMANTIC_MAP_SCHEMA
    status = "MISSING" if not valid else ("FRESH" if not missing and not stale and not deleted else "STALE")
    return {
        "schema": "PROJECT_SEMANTIC_MAP_STATUS_V1",
        "status": status,
        "snapshot_id": snapshot["snapshot_id"],
        "semantic_map_revision": _revision(current) if valid else "",
        "semantic_file_count": len(records),
        "target_file_count": len(targets),
        "needs_analysis_count": len(set(missing + stale)),
        "needs_analysis": sorted(set(missing + stale)),
        "deleted_paths": deleted,
        "scope": "PLAN" if required_paths is not None else "PROJECT",
        "required_paths": sorted(targets),
        "json_path": str(semantic_map_path(workspace)),
        "markdown_path": str(semantic_map_markdown_path(workspace)),
    }


def _string_list(value: object, field: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or not all(isinstance(item, str) and len(item) <= 4000 for item in value):
        raise ValueError(f"semantic_map_invalid_{field}")
    return [item.strip() for item in value if item.strip()]


def _placeholder(value: object) -> bool:
    text = str(value or "").strip()
    return len(text) >= 2 and text.startswith("<") and text.endswith(">")


def _render_markdown(value: dict) -> str:
    lines = [
        "# Project Semantic Map",
        "",
        f"Snapshot: `{value['snapshot_id']}`",
        f"Revision: `{value['semantic_map_revision']}`",
        "",
        "## Project summary",
        "",
        str(value.get("project_summary", "") or "(not provided)"),
        "",
        "## Flows",
        "",
    ]
    flows = value.get("flows", [])
    lines.extend(f"- {flow}" for flow in flows)
    if not flows:
        lines.append("- (not provided)")
    for row in value.get("files", []):
        lines.extend(["", f"## `{row['path']}`", "", row["responsibility"]])
        for label, key in (("Symbols", "public_symbols"), ("Dependencies", "dependencies"), ("Flows", "flows"), ("Invariants", "invariants"), ("Tests", "tests")):
            values = row.get(key, [])
            if values:
                lines.extend(["", f"{label}:", *[f"- {item}" for item in values]])
    return "\n".join(lines).rstrip() + "\n"


def update_semantic_map(workspace: str | Path, patch: dict) -> dict:
    if not isinstance(patch, dict):
        return {"status": "INVALID", "reason": "PATCH_NOT_OBJECT"}
    snapshot = inspect_project_scope(workspace, verify_cached_content=True)
    if str(patch.get("base_snapshot_id", "")) != snapshot["snapshot_id"]:
        return {"status": "SNAPSHOT_MISMATCH", "current_snapshot_id": snapshot["snapshot_id"]}
    supplied = patch.get("files", [])
    if not isinstance(supplied, list):
        return {"status": "INVALID", "reason": "FILES_NOT_LIST"}
    required_paths = patch.get("required_paths")
    try:
        targets = _targets(snapshot, required_paths)
    except ValueError as exc:
        return {"status": "INVALID", "reason": str(exc)}
    old = _load(workspace)
    all_targets = _targets(snapshot)
    records = {
        str(row.get("path", "")): dict(row)
        for row in old.get("files", [])
        if isinstance(row, dict) and str(row.get("path", "")) in all_targets
        and (str(row.get("path", "")) not in targets
             or row.get("source_sha256") == targets[str(row.get("path"))].get("sha256"))
    }
    try:
        seen_paths = set()
        for item in supplied:
            if not isinstance(item, dict):
                raise ValueError("semantic_map_invalid_file_entry")
            path = str(item.get("path", "")).replace("\\", "/").lstrip("/")
            if path in seen_paths:
                raise ValueError(f"semantic_map_duplicate_path:{path}")
            seen_paths.add(path)
            if path not in targets:
                raise ValueError(f"semantic_map_unknown_path:{path}")
            if str(item.get("source_sha256", "")) != targets[path].get("sha256"):
                raise ValueError(f"semantic_map_source_hash_mismatch:{path}")
            responsibility = str(item.get("responsibility", "")).strip()
            if not responsibility or len(responsibility) > 16000 or _placeholder(responsibility):
                raise ValueError(f"semantic_map_missing_responsibility:{path}")
            records[path] = {
                "path": path,
                "source_sha256": targets[path]["sha256"],
                "responsibility": responsibility,
                "public_symbols": _string_list(item.get("public_symbols", []), "public_symbols"),
                "dependencies": _string_list(item.get("dependencies", []), "dependencies"),
                "flows": _string_list(item.get("flows", []), "flows"),
                "invariants": _string_list(item.get("invariants", []), "invariants"),
                "tests": _string_list(item.get("tests", []), "tests"),
            }
        preserve_global = required_paths is not None and bool(old)
        project_summary = str(old.get("project_summary", "") if preserve_global else patch.get("project_summary", old.get("project_summary", ""))).strip()[:65536]
        if _placeholder(project_summary):
            raise ValueError("semantic_map_placeholder_project_summary")
        value = {
            "schema": SEMANTIC_MAP_SCHEMA,
            "snapshot_id": snapshot["snapshot_id"],
            "project_summary": project_summary,
            "flows": _string_list(old.get("flows", []) if preserve_global else patch.get("flows", old.get("flows", [])), "flows", maximum=128),
            "files": [records[path] for path in sorted(records)],
        }
        value["semantic_map_revision"] = _revision(value)
        _atomic_json(semantic_map_path(workspace), value)
        md = semantic_map_markdown_path(workspace)
        tmp = md.with_name(md.name + ".tmp")
        tmp.write_text(_render_markdown(value), encoding="utf-8")
        tmp.replace(md)
    except ValueError as exc:
        return {"status": "INVALID", "reason": str(exc)}
    status = inspect_semantic_map(workspace, required_paths)
    if status['snapshot_id'] != snapshot['snapshot_id']:
        return {**status, 'status':'PARTIAL', 'reason':'SOURCE_CHANGED_DURING_UPDATE',
                'accepted_snapshot_id':snapshot['snapshot_id'],
                'verification_status':'FAIL', 'mutation_applied':True}
    # Verification is performed against saved bytes, not the in-memory value.
    persisted = _load(workspace)
    verified = persisted == value and semantic_map_markdown_path(workspace).read_text(encoding='utf-8') == _render_markdown(value)
    if not verified:
        return {**status, 'status':'PARTIAL', 'reason':'PERSISTED_MAP_VERIFICATION_FAILED',
                'verification_status':'FAIL', 'mutation_applied':True}
    result_status = "UPDATED" if status["status"] == "FRESH" else "PARTIAL"
    ledger_event = append_project_event(
        workspace,
        "semantic_map_updated",
        snapshot_id=str(snapshot["snapshot_id"]),
        changed_files=[str(item.get("path", "")) for item in supplied if isinstance(item, dict)],
        semantic_map_revision=str(status.get("semantic_map_revision", "") or ""),
        operation_result=result_status,
        actor="runtime",
        source="semantic_map.update_semantic_map",
        notes="Snapshot-bound semantic map revision published.",
        details={
            "updated_file_count": len(supplied),
            "coverage": status.get("coverage", {}),
        },
        event_key=(
            f"semantic_map_updated:{snapshot['snapshot_id']}:"
            f"{status.get('semantic_map_revision', '')}"
        ),
    )
    return {
        **status,
        "status": result_status,
        "updated_file_count": len(supplied),
        "ledger_event_id": ledger_event["event_id"],
        "verification_status": "PASS",
        "mutation_applied": True,
        "verification_evidence": {"json_readback_equal":True,"markdown_readback_equal":True},
        "preserved_project_fields": ["project_summary", "flows"] if preserve_global else [],
    }


def update_semantic_map_file(workspace: str | Path, path: str, expected_sha256: str = "") -> dict:
    root = Path(workspace).expanduser().resolve()
    try:
        source = canonicalize_path(path, base=root, forbid_glob=True)
    except PathSecurityError as exc:
        return {"status":"INVALID", "reason":str(exc)}
    try:
        source.relative_to(root)
    except ValueError:
        return {"status": "INVALID", "reason": "PATH_OUTSIDE_WORKSPACE"}
    if not source.is_file():
        return {"status": "INVALID", "reason": "PATCH_FILE_MISSING"}
    with source.open('rb') as stream:
        raw = stream.read(SEMANTIC_PATCH_FILE_MAX_BYTES + 1)
    if len(raw) > SEMANTIC_PATCH_FILE_MAX_BYTES:
        return {"status": "INVALID", "reason": "PATCH_FILE_TOO_LARGE", "max_bytes": SEMANTIC_PATCH_FILE_MAX_BYTES}
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        return {"status": "INVALID", "reason": "SHA256_MISMATCH", "actual_sha256": digest}
    try:
        patch = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"status": "INVALID", "reason": f"JSON_DECODE:{type(exc).__name__}"}
    proof = None
    if isinstance(patch, dict):
        proof = _patch_only_snapshot(root, source, digest, str(patch.get('base_snapshot_id', '')))
        if proof:
            patch = {**patch, 'base_snapshot_id':proof['verified_current_snapshot_id']}
    # Normal update takes another fresh snapshot, detecting changes after proof.
    result = update_semantic_map(root, patch)
    if proof:
        result['patch_snapshot_evidence'] = proof
    return result


def compact_semantic_map_context(workspace: str | Path, *, path_limit: int = 30) -> str:
    status = inspect_semantic_map(workspace)
    compact = {
        "status": status["status"],
        "snapshot_id": status["snapshot_id"],
        "semantic_map_revision": status["semantic_map_revision"],
        "target_file_count": status["target_file_count"],
        "needs_analysis_count": status["needs_analysis_count"],
        "needs_analysis_sample": status["needs_analysis"][:max(0, int(path_limit))],
    }
    return "[SMARTAGENT_V8_CONTEXT_STATUS]\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n[/SMARTAGENT_V8_CONTEXT_STATUS]"


def load_semantic_map(workspace: str | Path) -> dict:
    return _load(workspace)
