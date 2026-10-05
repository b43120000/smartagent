#!/usr/bin/env python3
"""Typed filesystem mutations whose targets are fixed before approval."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .path_security import PathSecurityError, is_filesystem_root, reject_reparse_components
from .security_context import SecurityContext


class SafeFileOperationError(ValueError):
    pass


def build_delete_manifest(
    context: SecurityContext,
    path: str,
    *,
    recursive: bool = False,
    max_entries: int = 10000,
) -> dict:
    try:
        target = context.require_write(
            path,
            require_absolute=True,
            allow_root=False,
            forbid_glob=True,
        )
    except PathSecurityError as exc:
        raise SafeFileOperationError(str(exc)) from exc
    if is_filesystem_root(target):
        raise SafeFileOperationError(f"filesystem_root_delete_forbidden:{target}")
    if target == context.workspace_root or target == context.config.runtime_write_root:
        raise SafeFileOperationError(f"authorized_root_delete_forbidden:{target}")
    if not target.exists():
        raise SafeFileOperationError(f"delete_target_not_found:{target}")
    if target.is_dir() and not recursive:
        try:
            next(target.iterdir())
        except StopIteration:
            pass
        else:
            raise SafeFileOperationError(f"non_empty_directory_requires_recursive:true:{target}")

    entries: list[dict] = []
    if target.is_dir():
        for item in target.rglob("*"):
            if len(entries) >= max_entries:
                raise SafeFileOperationError(f"delete_manifest_too_large:{max_entries}")
            try:
                reject_reparse_components(item, target)
            except PathSecurityError as exc:
                raise SafeFileOperationError(f"delete_tree_contains_reparse:{exc}") from exc
            entries.append({
                "path": str(item),
                "kind": "directory" if item.is_dir() else "file",
                "size_bytes": item.stat().st_size if item.is_file() else 0,
            })
    else:
        entries.append({"path": str(target), "kind": "file", "size_bytes": target.stat().st_size})
    entries.sort(key=lambda row: row["path"].casefold())
    stable = {
        "target": str(target),
        "recursive": bool(recursive),
        "entries": entries,
    }
    digest = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "DELETE_MANIFEST_V1",
        **stable,
        "entry_count": len(entries),
        "total_bytes": sum(int(row["size_bytes"]) for row in entries),
        "manifest_digest": digest,
        "approval_required": True,
    }


__all__ = ["SafeFileOperationError", "build_delete_manifest"]


def execute_delete_manifest(context: SecurityContext, manifest: dict) -> dict:
    fresh = build_delete_manifest(
        context, str(manifest.get("target", "")),
        recursive=bool(manifest.get("recursive", False)),
    )
    if fresh["manifest_digest"] != str(manifest.get("manifest_digest", "")):
        raise SafeFileOperationError("delete_manifest_changed_since_approval")
    target = Path(fresh["target"])
    if target.is_dir():
        if fresh["recursive"]:
            shutil.rmtree(target)
        else:
            target.rmdir()
    else:
        target.unlink()
    return {
        "schema": "DELETE_RESULT_V1", "status": "DELETED",
        "target": str(target), "manifest_digest": fresh["manifest_digest"],
        "entry_count": fresh["entry_count"], "total_bytes": fresh["total_bytes"],
    }


__all__.append("execute_delete_manifest")
