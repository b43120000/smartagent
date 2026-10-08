#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .project_ledger import append_project_event
from .project_sync import inspect_project_scope, is_ignored_project_path

SEMANTIC_MAP_SCHEMA = "PROJECT_SEMANTIC_MAP_V1"
_SOURCE_LANGUAGES = {
    "c", "c-cpp-header", "cpp", "cpp-header", "java", "kotlin", "python",
    "javascript", "typescript", "rust", "go", "cmake", "gradle",
}
SEMANTIC_PATCH_FILE_MAX_BYTES = 64 * 1024 * 1024


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
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _targets(snapshot: dict, required_paths: object = None) -> dict[str, dict]:
    build = set(snapshot.get("build_files", []))
    required = None
    if required_paths is not None:
        if not isinstance(required_paths, (list, tuple, set)):
            raise ValueError("semantic_map_invalid_required_paths")
        required = {
            str(path).replace("\\", "/").lstrip("/")
            for path in required_paths if str(path).strip()
        }
    return {
        str(row["path"]): row
        for row in snapshot.get("files", [])
        if not is_ignored_project_path(str(row.get("path", "")))
        and (row.get("language") in _SOURCE_LANGUAGES or row.get("path") in build)
        and (required is None or str(row.get("path", "")) in required)
    }


def semantic_target_paths(
    workspace: str | Path, snapshot: dict | None = None,
    required_paths: object = None,
) -> set[str]:
    return set(_targets(snapshot or inspect_project_scope(workspace), required_paths))


def _revision(value: dict) -> str:
    basis = {k: value.get(k) for k in ("schema", "snapshot_id", "project_summary", "flows", "files")}
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def inspect_semantic_map(workspace: str | Path, required_paths: object = None) -> dict:
    snapshot = inspect_project_scope(workspace)
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
    snapshot = inspect_project_scope(workspace)
    if str(patch.get("base_snapshot_id", "")) != snapshot["snapshot_id"]:
        return {"status": "SNAPSHOT_MISMATCH", "current_snapshot_id": snapshot["snapshot_id"]}
    supplied = patch.get("files", [])
    if not isinstance(supplied, list):
        return {"status": "INVALID", "reason": "FILES_NOT_LIST"}
    targets = _targets(snapshot)
    old = _load(workspace)
    records = {
        str(row.get("path", "")): dict(row)
        for row in old.get("files", [])
        if isinstance(row, dict) and str(row.get("path", "")) in targets
        and row.get("source_sha256") == targets[str(row.get("path"))].get("sha256")
    }
    try:
        for item in supplied:
            if not isinstance(item, dict):
                raise ValueError("semantic_map_invalid_file_entry")
            path = str(item.get("path", "")).replace("\\", "/").lstrip("/")
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
        project_summary = str(patch.get("project_summary", old.get("project_summary", ""))).strip()[:65536]
        if _placeholder(project_summary):
            raise ValueError("semantic_map_placeholder_project_summary")
        value = {
            "schema": SEMANTIC_MAP_SCHEMA,
            "snapshot_id": snapshot["snapshot_id"],
            "project_summary": project_summary,
            "flows": _string_list(patch.get("flows", old.get("flows", [])), "flows", maximum=128),
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
    status = inspect_semantic_map(workspace)
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
    }


def update_semantic_map_file(workspace: str | Path, path: str, expected_sha256: str = "") -> dict:
    root = Path(workspace).expanduser().resolve()
    source = Path(path).expanduser().resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return {"status": "INVALID", "reason": "PATH_OUTSIDE_WORKSPACE"}
    if not source.is_file():
        return {"status": "INVALID", "reason": "PATCH_FILE_MISSING"}
    raw = source.read_bytes()
    if len(raw) > SEMANTIC_PATCH_FILE_MAX_BYTES:
        return {"status": "INVALID", "reason": "PATCH_FILE_TOO_LARGE", "max_bytes": SEMANTIC_PATCH_FILE_MAX_BYTES}
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        return {"status": "INVALID", "reason": "SHA256_MISMATCH", "actual_sha256": digest}
    try:
        patch = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"status": "INVALID", "reason": f"JSON_DECODE:{type(exc).__name__}"}
    return update_semantic_map(root, patch)


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
