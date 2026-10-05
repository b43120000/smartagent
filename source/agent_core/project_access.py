#!/usr/bin/env python3
"""Snapshot-bound, attachment-free project access for WebGPT planners."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Mapping

from .project_artifact_policy import SOURCE_CODE, TEXT_DATA_CONFIG
from .project_sync import (
    inspect_project_scope,
    load_current_project_snapshot,
    load_project_snapshot,
    save_project_snapshot,
)


CAPSULE_SCHEMA = "PROJECT_ACCESS_CAPSULE_V1"
QUERY_SCHEMA = "PROJECT_ACCESS_QUERY_RESULT_V1"
DEFAULT_QUERY_MAX_BYTES = 24 * 1024
HARD_QUERY_MAX_BYTES = 28 * 1024
MAX_QUERY_COUNT = 8
MAX_FILE_READ_BYTES = 10 * 1024 * 1024
MAX_SEARCH_SCAN_BYTES = 64 * 1024 * 1024
SUPPORTED_QUERY_OPERATIONS = {
    "list_tree", "search_text", "read_range", "read_symbol",
    "find_references", "get_build_configuration", "get_file_metadata",
}
VENDOR_PARTS = {"third_party", "third-party", "vendor", "vendors", "external", "externals"}
IMPORTANT_NAMES = {
    "readme.md", "readme.txt", "cmakelists.txt", "makefile", "dockerfile",
    "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts",
    "package.json", "pyproject.toml", "cargo.toml", "go.mod", "androidmanifest.xml",
}


class ProjectAccessError(ValueError):
    pass


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_handle(root: Path) -> str:
    return "PROJECT-" + hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:20].upper()


def _state_root(root: Path) -> Path:
    return root / ".agents" / "project_access"


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _top_level_tree(records: list[dict], limit: int = 160) -> tuple[list[str], bool]:
    values: set[str] = set()
    for record in records:
        parts = PurePosixPath(str(record.get("path", ""))).parts
        if not parts:
            continue
        values.add(parts[0] + ("/" if len(parts) > 1 else ""))
        if len(parts) > 1:
            values.add("/".join(parts[:2]) + ("/" if len(parts) > 2 else ""))
    ordered = sorted(values)
    return ordered[:limit], len(ordered) > limit


def _important_records(snapshot: Mapping[str, object], limit: int = 80) -> list[dict]:
    build = set(str(value) for value in snapshot.get("build_files", []) or [])
    changed = {
        str(value)[3:].strip().replace("\\", "/")
        for value in snapshot.get("git_status", []) or []
        if len(str(value)) >= 4
    }
    selected = []
    for record in snapshot.get("files", []) or []:
        path = str(record.get("path", ""))
        name = PurePosixPath(path).name.casefold()
        if path in build or path in changed or name in IMPORTANT_NAMES:
            selected.append({
                "path": path,
                "language": record.get("language", ""),
                "size_bytes": int(record.get("size_bytes", 0) or 0),
                "sha256": str(record.get("sha256", "")),
                "reason": "build" if path in build else ("changed" if path in changed else "entry"),
            })
    return selected[:limit]


def build_project_capsule(workspace: str | Path) -> dict:
    """Build a local index and return only a compact model-facing capsule."""
    root = Path(workspace).expanduser().resolve()
    snapshot = inspect_project_scope(root)
    snapshot_path = save_project_snapshot(snapshot)
    records = list(snapshot.get("files", []) or [])
    language_counts = Counter(str(item.get("language", "unknown")) for item in records)
    class_counts = Counter(str(item.get("artifact_class", "unknown")) for item in records)
    tree, tree_truncated = _top_level_tree(records)
    capsule = {
        "schema": CAPSULE_SCHEMA,
        "status": "READY",
        "strategy": "INDEX_ONLY",
        "project_handle": _project_handle(root),
        "snapshot_id": snapshot["snapshot_id"],
        "project_root": str(root),
        "git_head": snapshot.get("git_head", ""),
        "git_dirty": bool(snapshot.get("git_dirty")),
        "git_status": list(snapshot.get("git_status", []) or [])[:80],
        "file_count": int(snapshot.get("file_count", 0) or 0),
        "directory_count": int(snapshot.get("directory_count", 0) or 0),
        "total_bytes": int(snapshot.get("total_bytes", 0) or 0),
        "language_counts": dict(sorted(language_counts.items())),
        "artifact_class_counts": dict(sorted(class_counts.items())),
        "build_files": list(snapshot.get("build_files", []) or [])[:80],
        "important_files": _important_records(snapshot),
        "top_level_tree": tree,
        "tree_truncated": tree_truncated,
        "query_contract": {
            "tool": "query_project",
            "project_root": str(root),
            "project_handle": _project_handle(root),
            "snapshot_id": snapshot["snapshot_id"],
            "max_queries_per_call": MAX_QUERY_COUNT,
            "default_response_bytes": DEFAULT_QUERY_MAX_BYTES,
            "operations": [
                "list_tree", "search_text", "read_range", "read_symbol",
                "find_references", "get_build_configuration", "get_file_metadata",
            ],
        },
        "transport": "INLINE_QUERY_ONLY",
        "attachments_uploaded": 0,
    }
    state = _state_root(root)
    _atomic_json(state / "capsules" / f"{snapshot['snapshot_id']}.json", capsule)
    _atomic_json(state / "current.json", {
        "schema": "PROJECT_ACCESS_CURRENT_V1",
        "project_handle": capsule["project_handle"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_path": str(snapshot_path),
        "capsule_path": str(state / "capsules" / f"{snapshot['snapshot_id']}.json"),
        "updated_at": time.time(),
    })
    return capsule


def _relative_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    pure = PurePosixPath(text or ".")
    if pure.is_absolute() or ".." in pure.parts:
        raise ProjectAccessError(f"project_query_path_invalid:{text}")
    return "" if text in {"", "."} else pure.as_posix().strip("/")


def _snapshot_for_query(root: Path, snapshot_id: str) -> dict:
    snapshot = load_project_snapshot(root, snapshot_id) if snapshot_id else load_current_project_snapshot(root)
    if not snapshot:
        raise ProjectAccessError("project_access_index_missing:run_project_sync_INDEX_ONLY")
    if Path(str(snapshot.get("workspace_root", ""))).resolve() != root:
        raise ProjectAccessError("project_access_snapshot_root_mismatch")
    return snapshot


def normalize_project_queries(queries: object) -> tuple[list[dict], int]:
    """Normalize deterministic query shorthand before any index work."""
    if not isinstance(queries, list) or not queries or len(queries) > MAX_QUERY_COUNT:
        raise ProjectAccessError(f"project_query_count_invalid:1..{MAX_QUERY_COUNT}")
    normalized: list[dict] = []
    shorthand_count = 0
    for index, query in enumerate(queries, 1):
        if isinstance(query, str):
            value = query.strip()
            if not value or len(value) > 256:
                raise ProjectAccessError(f"project_query_invalid:{index}")
            normalized.append({"operation": "search_text", "query": value})
            shorthand_count += 1
            continue
        if not isinstance(query, Mapping):
            raise ProjectAccessError(f"project_query_invalid:{index}")
        item = dict(query)
        operation = str(item.get("operation", "") or "").strip().lower()
        if operation not in SUPPORTED_QUERY_OPERATIONS:
            raise ProjectAccessError(
                f"project_query_operation_unsupported:{operation or '<missing>'}"
            )
        item["operation"] = operation
        normalized.append(item)
    return normalized, shorthand_count


def _recoverable_snapshot_result(payload: Mapping[str, object]) -> bool:
    prefixes = (
        "project_snapshot_stale:",
        "project_query_file_missing:",
        "project_query_file_not_indexed:",
    )
    for result in payload.get("results", []) or []:
        if not isinstance(result, Mapping):
            continue
        error = str(result.get("error", "") or "")
        if any(error.startswith(prefix) for prefix in prefixes):
            return True
    return False


def _records_by_path(snapshot: Mapping[str, object]) -> dict[str, dict]:
    return {str(item.get("path", "")): dict(item) for item in snapshot.get("files", []) or []}


def _require_indexed_file(root: Path, snapshot: Mapping[str, object], relative: object) -> tuple[Path, dict]:
    rel = _relative_path(relative)
    record = _records_by_path(snapshot).get(rel)
    if not record:
        raise ProjectAccessError(f"project_query_file_not_indexed:{rel}")
    if record.get("artifact_class") not in {SOURCE_CODE, TEXT_DATA_CONFIG}:
        raise ProjectAccessError(f"project_query_file_not_text:{rel}")
    if int(record.get("size_bytes", 0) or 0) > MAX_FILE_READ_BYTES:
        raise ProjectAccessError(f"project_query_file_too_large:{rel}")
    path = (root / Path(*PurePosixPath(rel).parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProjectAccessError(f"project_query_path_escape:{rel}") from exc
    if not path.is_file():
        raise ProjectAccessError(f"project_query_file_missing:{rel}")
    actual_sha = _sha256_file(path)
    if actual_sha != str(record.get("sha256", "")):
        raise ProjectAccessError(f"project_snapshot_stale:{rel}")
    return path, record


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _range_result(root: Path, snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    path, record = _require_indexed_file(root, snapshot, query.get("path", ""))
    lines = _read_lines(path)
    start = max(1, int(query.get("start_line", 1) or 1))
    requested_end = int(query.get("end_line", start + 119) or (start + 119))
    end = min(len(lines), max(start, min(requested_end, start + 239)))
    content = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
    return {
        "operation": "read_range", "status": "OK", "path": record["path"],
        "start_line": start, "end_line": end, "total_lines": len(lines),
        "file_sha256": record["sha256"], "content": content,
        "truncated": end < requested_end or end < len(lines),
        "next_cursor": end + 1 if end < len(lines) else None,
    }


def _list_tree(snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    prefix = _relative_path(query.get("path", ""))
    depth = max(1, min(int(query.get("depth", 2) or 2), 6))
    limit = max(1, min(int(query.get("limit", 120) or 120), 240))
    cursor = max(0, int(query.get("cursor", 0) or 0))
    entries: dict[str, dict] = {}
    prefix_parts = PurePosixPath(prefix).parts if prefix else ()
    for record in snapshot.get("files", []) or []:
        rel = str(record.get("path", ""))
        parts = PurePosixPath(rel).parts
        if prefix_parts and parts[:len(prefix_parts)] != prefix_parts:
            continue
        remainder = parts[len(prefix_parts):]
        if not remainder:
            continue
        shown = remainder[:depth]
        entry_path = "/".join((*prefix_parts, *shown))
        is_dir = len(remainder) > len(shown) or len(shown) < len(remainder)
        if len(remainder) > depth:
            is_dir = True
        if entry_path not in entries:
            entries[entry_path] = {"path": entry_path, "kind": "directory" if is_dir else "file"}
            if not is_dir:
                entries[entry_path].update({
                    "size_bytes": int(record.get("size_bytes", 0) or 0),
                    "language": record.get("language", ""),
                    "sha256": record.get("sha256", ""),
                })
    ordered = [entries[key] for key in sorted(entries)]
    page = ordered[cursor:cursor + limit]
    return {
        "operation": "list_tree", "status": "OK", "path": prefix or ".",
        "page_cursor": cursor,
        "items": page, "truncated": cursor + len(page) < len(ordered),
        "next_cursor": cursor + len(page) if cursor + len(page) < len(ordered) else None,
    }


def _vendor_path(relative: str) -> bool:
    return any(part.casefold() in VENDOR_PARTS for part in PurePosixPath(relative).parts)


def _search(snapshot: Mapping[str, object], root: Path, query: Mapping[str, object], *, references: bool = False) -> dict:
    needle = str(query.get("query", query.get("symbol", "")) or "")
    if not needle or len(needle) > 256:
        raise ProjectAccessError("project_query_search_term_invalid")
    prefix = _relative_path(query.get("path", ""))
    case_sensitive = bool(query.get("case_sensitive", False))
    include_vendor = bool(query.get("include_vendor", False))
    limit = max(1, min(int(query.get("limit", 40) or 40), 100))
    cursor = max(0, int(query.get("cursor", 0) or 0))
    expression = re.compile(rf"\b{re.escape(needle)}\b" if references else re.escape(needle), 0 if case_sensitive else re.IGNORECASE)
    matches = []
    scanned_bytes = 0
    scan_truncated = False
    for record in snapshot.get("files", []) or []:
        rel = str(record.get("path", ""))
        if prefix and not (rel == prefix or rel.startswith(prefix + "/")):
            continue
        if not include_vendor and _vendor_path(rel):
            continue
        if record.get("artifact_class") not in {SOURCE_CODE, TEXT_DATA_CONFIG}:
            continue
        size = int(record.get("size_bytes", 0) or 0)
        if size > MAX_FILE_READ_BYTES:
            continue
        if scanned_bytes + size > MAX_SEARCH_SCAN_BYTES:
            scan_truncated = True
            break
        path, checked = _require_indexed_file(root, snapshot, rel)
        scanned_bytes += size
        for line_number, line in enumerate(_read_lines(path), 1):
            if expression.search(line):
                matches.append({
                    "path": rel, "line": line_number, "text": line[:500],
                    "file_sha256": checked["sha256"],
                })
    page = matches[cursor:cursor + limit]
    more_known = cursor + len(page) < len(matches)
    return {
        "operation": "find_references" if references else "search_text",
        "status": "OK", "query": needle, "matches": page,
        "page_cursor": cursor,
        "scanned_bytes": scanned_bytes,
        "truncated": bool(more_known or scan_truncated),
        "next_cursor": cursor + len(page) if more_known else None,
        "scan_budget_exhausted": scan_truncated,
        "refine_required": bool(scan_truncated and not more_known),
    }


def _read_symbol(root: Path, snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    symbol = str(query.get("symbol", "") or "").strip()
    if not symbol or len(symbol) > 256:
        raise ProjectAccessError("project_query_symbol_invalid")
    search_query = dict(query)
    search_query["query"] = symbol
    search_query["limit"] = min(int(query.get("limit", 8) or 8), 20)
    found = _search(snapshot, root, search_query, references=True)
    matches = list(found.get("matches", []) or [])
    if not matches:
        return {"operation": "read_symbol", "status": "NOT_FOUND", "symbol": symbol, "matches": []}
    selected = matches[0]
    start = max(1, int(selected["line"]) - 5)
    ranged = _range_result(root, snapshot, {
        "path": selected["path"], "start_line": start,
        "end_line": start + min(int(query.get("context_lines", 120) or 120), 200) - 1,
    })
    ranged.update({"operation": "read_symbol", "symbol": symbol, "candidate_count": len(matches)})
    return ranged


def _build_configuration(root: Path, snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    limit = max(1, min(int(query.get("limit", 8) or 8), 16))
    items = []
    for relative in list(snapshot.get("build_files", []) or [])[:limit]:
        ranged = _range_result(root, snapshot, {"path": relative, "start_line": 1, "end_line": 100})
        items.append(ranged)
    return {"operation": "get_build_configuration", "status": "OK", "items": items, "truncated": len(snapshot.get("build_files", []) or []) > limit}


def _metadata(snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    rel = _relative_path(query.get("path", ""))
    record = _records_by_path(snapshot).get(rel)
    if not record:
        raise ProjectAccessError(f"project_query_file_not_indexed:{rel}")
    return {"operation": "get_file_metadata", "status": "OK", "file": record}


def _execute_query(root: Path, snapshot: Mapping[str, object], query: Mapping[str, object]) -> dict:
    operation = str(query.get("operation", "") or "").strip().lower()
    if operation == "list_tree":
        return _list_tree(snapshot, query)
    if operation == "search_text":
        return _search(snapshot, root, query)
    if operation == "find_references":
        return _search(snapshot, root, query, references=True)
    if operation == "read_range":
        return _range_result(root, snapshot, query)
    if operation == "read_symbol":
        return _read_symbol(root, snapshot, query)
    if operation == "get_build_configuration":
        return _build_configuration(root, snapshot, query)
    if operation == "get_file_metadata":
        return _metadata(snapshot, query)
    raise ProjectAccessError(f"project_query_operation_unsupported:{operation}")


def _fit_payload(payload: dict, limit_bytes: int) -> dict:
    payload["response_truncated"] = False
    while _json_bytes(payload) > limit_bytes:
        changed = False
        for result in reversed(payload.get("results", [])):
            content = result.get("content")
            if isinstance(content, str) and len(content) > 512:
                lines = content.splitlines()
                if len(lines) > 1:
                    kept = lines[:max(1, len(lines) // 2)]
                    result["content"] = "\n".join(kept)
                    start_line = int(result.get("start_line", 1) or 1)
                    result["end_line"] = start_line + len(kept) - 1
                    result["next_cursor"] = result["end_line"] + 1
                else:
                    result["content"] = content[:max(512, len(content) // 2)]
                    result["content_character_truncated"] = True
                result["truncated"] = True
                payload["response_truncated"] = changed = True
                break
            for key in ("items", "matches"):
                values = result.get(key)
                if isinstance(values, list) and values:
                    values.pop()
                    result["truncated"] = True
                    if "page_cursor" in result:
                        result["next_cursor"] = int(result["page_cursor"]) + len(values)
                    payload["response_truncated"] = changed = True
                    break
            if changed:
                break
        if changed:
            continue
        if payload.get("results"):
            payload["results"].pop()
            payload["result_count"] = len(payload["results"])
            payload["response_truncated"] = True
            continue
        raise ProjectAccessError("project_query_budget_too_small")
    return payload


def query_project(
    workspace: str | Path,
    *,
    snapshot_id: str,
    project_handle: str,
    queries: list[Mapping[str, object]],
    max_bytes: int = DEFAULT_QUERY_MAX_BYTES,
    request_id: str = "",
    action_id: str = "",
) -> dict:
    root = Path(workspace).expanduser().resolve()
    expected_handle = _project_handle(root)
    if project_handle and str(project_handle) != expected_handle:
        raise ProjectAccessError("project_access_handle_mismatch")
    if not isinstance(queries, list) or not queries or len(queries) > MAX_QUERY_COUNT:
        raise ProjectAccessError(f"project_query_count_invalid:1..{MAX_QUERY_COUNT}")
    snapshot = _snapshot_for_query(root, str(snapshot_id or ""))
    actual_snapshot_id = str(snapshot.get("snapshot_id", ""))
    if snapshot_id and str(snapshot_id) != actual_snapshot_id:
        raise ProjectAccessError("project_access_snapshot_mismatch")
    query_id = "PQUERY-" + uuid.uuid4().hex[:20].upper()
    results = []
    for index, query in enumerate(queries, 1):
        if not isinstance(query, Mapping):
            raise ProjectAccessError(f"project_query_invalid:{index}")
        try:
            result = _execute_query(root, snapshot, query)
        except ProjectAccessError as exc:
            result = {
                "operation": str(query.get("operation", "") or ""),
                "status": "ERROR", "error": str(exc),
            }
        result["query_index"] = index
        results.append(result)
    limit = max(4096, min(int(max_bytes or DEFAULT_QUERY_MAX_BYTES), HARD_QUERY_MAX_BYTES))
    payload = {
        "schema": QUERY_SCHEMA,
        "status": "READY",
        "project_handle": expected_handle,
        "snapshot_id": actual_snapshot_id,
        "query_id": query_id,
        "result_count": len(results),
        "results": results,
        "attachments_uploaded": 0,
        "response_budget_bytes": limit,
    }
    payload = _fit_payload(payload, limit)
    ledger = {
        **payload,
        "request_id": str(request_id or ""),
        "action_id": str(action_id or ""),
        "created_at": time.time(),
    }
    _atomic_json(_state_root(root) / "query_ledger" / f"{query_id}.json", ledger)
    return payload


def query_project_with_runtime_index(
    workspace: str | Path,
    *,
    queries: object,
    requested_snapshot_id: str = "",
    requested_project_handle: str = "",
    max_bytes: int = DEFAULT_QUERY_MAX_BYTES,
    request_id: str = "",
    action_id: str = "",
) -> dict:
    """Execute against an exact-root, Runtime-owned INDEX_ONLY context."""
    root = Path(workspace).expanduser().resolve()
    normalized, shorthand_count = normalize_project_queries(queries)
    requested_snapshot = str(requested_snapshot_id or "")
    requested_handle = str(requested_project_handle or "")
    current = load_current_project_snapshot(root)
    recovery = "REUSED_CURRENT_INDEX"
    if not current:
        capsule = build_project_capsule(root)
        current = load_project_snapshot(root, str(capsule["snapshot_id"]))
        recovery = "INDEX_BOOTSTRAP"
    effective_snapshot = str(current.get("snapshot_id", "") or "")
    if not effective_snapshot:
        raise ProjectAccessError("project_access_index_bootstrap_failed")

    payload = query_project(
        root,
        snapshot_id=effective_snapshot,
        project_handle=_project_handle(root),
        queries=normalized,
        max_bytes=max_bytes,
        request_id=request_id,
        action_id=action_id,
    )
    if _recoverable_snapshot_result(payload):
        capsule = build_project_capsule(root)
        effective_snapshot = str(capsule["snapshot_id"])
        recovery = "INDEX_REBUILT_ONCE"
        payload = query_project(
            root,
            snapshot_id=effective_snapshot,
            project_handle=_project_handle(root),
            queries=normalized,
            max_bytes=max_bytes,
            request_id=request_id,
            action_id=action_id,
        )
        if _recoverable_snapshot_result(payload):
            payload["status"] = "INCOMPLETE"
            payload["error"] = "project_access_index_rebuild_exhausted"
            recovery = "INDEX_REBUILD_EXHAUSTED"

    payload["project_root"] = str(root)
    payload["index_context"] = {
        "requested_root": str(root),
        "effective_root": str(root),
        "requested_snapshot_id": requested_snapshot,
        "effective_snapshot_id": effective_snapshot,
        "requested_project_handle": requested_handle,
        "effective_project_handle": _project_handle(root),
        "model_identity_authoritative": False,
        "recovery_action": recovery,
        "normalized_query_count": shorthand_count,
    }
    limit = max(4096, min(int(max_bytes or DEFAULT_QUERY_MAX_BYTES), HARD_QUERY_MAX_BYTES))
    payload = _fit_payload(payload, limit)
    ledger = {
        **payload,
        "request_id": str(request_id or ""),
        "action_id": str(action_id or ""),
        "created_at": time.time(),
    }
    _atomic_json(
        _state_root(root) / "query_ledger" / f"{payload['query_id']}.json",
        ledger,
    )
    return payload


__all__ = [
    "CAPSULE_SCHEMA", "QUERY_SCHEMA", "ProjectAccessError",
    "build_project_capsule", "normalize_project_queries", "query_project",
    "query_project_with_runtime_index",
]
