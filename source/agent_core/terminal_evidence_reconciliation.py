"""Bounded pre/post content-preservation evidence for deployment target gates.

Never authorizes mutation; does not validate the deployed *new* payload.
The only claim is: every pre-existing file/dir was retained with its original
identity and each pre-existing file retained its original bytes. New siblings
and new children are allowed without treating directory mtime as corruption.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping

SCHEMA = "SMARTAGENT_EXISTING_TREE_PRESERVATION_V1"
_MAX_NODES = 512
_MAX_BYTES = 64 * 1024 * 1024
_MAX_DEPTH = 10
_CHUNK = 1024 * 1024


class _Unavailable(Exception):
    pass


def _reparse(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & 0x400
    )


def _identity(st) -> tuple[int, int]:
    return int(st.st_dev), int(st.st_ino)


def _checked_stat(path: Path, scope):
    # Reauthorize each child: read permission for the parent is NOT a grant
    # to read outside the configured roots after symlink/junction changes.
    scope.require_read(str(path), require_absolute=True)
    st = path.lstat()
    if _reparse(st):
        raise _Unavailable("REPARSE_OR_SYMLINK_NOT_ATTESTABLE")
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        raise _Unavailable("SPECIAL_FILE_NOT_ATTESTABLE")
    return st


def _hash_file(path: Path, scope, limit: int) -> tuple[str, int]:
    before = _checked_stat(path, scope)
    if not stat.S_ISREG(before.st_mode):
        raise _Unavailable("EXPECTED_REGULAR_FILE")
    if before.st_size > limit:
        raise _Unavailable("FILE_BYTES_BUDGET_EXCEEDED")
    digest = hashlib.sha256()
    opened = 0
    fd = os.open(
        str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode) or _identity(before) != _identity(fd_stat):
            raise _Unavailable("FILE_IDENTITY_CHANGED_DURING_READ")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while True:
                chunk = stream.read(min(_CHUNK, limit - opened + 1))
                if not chunk:
                    break
                opened += len(chunk)
                if opened > limit:
                    raise _Unavailable("FILE_BYTES_BUDGET_EXCEEDED")
                digest.update(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    after = _checked_stat(path, scope)
    if (_identity(before) != _identity(after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or opened != after.st_size):
        raise _Unavailable("UNSTABLE_FILE_DURING_READ")
    return digest.hexdigest(), opened


def _entry(st) -> dict:
    return {
        "kind": "DIRECTORY" if stat.S_ISDIR(st.st_mode) else "FILE",
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
    }


def capture_existing_tree(root: Path, scope, *, max_nodes: int = _MAX_NODES,
                          max_bytes: int = _MAX_BYTES,
                          max_depth: int = _MAX_DEPTH) -> dict:
    """Capture only *existing* paths before the first mutation action."""
    nodes: dict[str, dict] = {}
    used = 0

    def walk(path: Path, rel: str, depth: int):
        nonlocal used
        if depth > max_depth or len(nodes) >= max_nodes:
            raise _Unavailable("SNAPSHOT_BOUNDS_EXCEEDED")
        info = _checked_stat(path, scope)
        row = _entry(info)
        if row["kind"] == "FILE":
            sha, size = _hash_file(path, scope, max_bytes - used)
            used += size
            row["sha256"] = sha
            row["bytes"] = size
        nodes[rel] = row
        if row["kind"] == "DIRECTORY":
            # scandir is not a write or an additional authorization.
            # Capture every existing child. Failing to enumerate even one
            # makes the entire baseline unusable rather than partial proof.
            with os.scandir(path) as scan:
                names = sorted(item.name for item in scan)
            if len(names) + len(nodes) > max_nodes:
                raise _Unavailable("SNAPSHOT_BOUNDS_EXCEEDED")
            for name in names:
                if name in {"", ".", ".."} or "/" in name or "\\" in name:
                    raise _Unavailable("UNSAFE_ENTRY_NAME")
                child_rel = (rel + "/" + name) if rel else name
                walk(path / name, child_rel, depth + 1)

    try:
        walk(root, "", 0)
    except _Unavailable as exc:
        return {"schema": SCHEMA, "status": "UNVERIFIED", "reason": str(exc),
                "nodes": {}, "node_count": 0, "bytes_hashed": 0}
    except (OSError, ValueError, PermissionError, TypeError) as exc:
        return {"schema": SCHEMA, "status": "UNVERIFIED",
                "reason": "READ_" + type(exc).__name__,
                "nodes": {}, "node_count": 0, "bytes_hashed": 0}
    return {
        "schema": SCHEMA, "status": "READY", "reason": "PREMUTATION_ATTESTED",
        "nodes": nodes, "node_count": len(nodes), "bytes_hashed": used,
        "snapshot_sha256": hashlib.sha256(json.dumps(
            nodes, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest(),
    }


def reconcile_existing_tree(root: Path, scope, baseline: Mapping | None) -> dict:
    """Prove all prior entries still exist unmodified, ignoring directory mtime."""
    result = {"schema": SCHEMA, "status": "UNVERIFIED",
              "reason": "BASELINE_UNAVAILABLE", "verified_nodes": 0,
              "verified_bytes": 0, "authorization_granted": False}
    if (not isinstance(baseline, Mapping)
            or baseline.get("schema") != SCHEMA
            or baseline.get("status") != "READY"):
        result["reason"] = "BASELINE_UNAVAILABLE"
        return result
    nodes = baseline.get("nodes")
    if not isinstance(nodes, dict) or not nodes or len(nodes) > _MAX_NODES:
        result["reason"] = "BASELINE_INVALID"
        return result
    total = 0
    for rel, old in sorted(nodes.items()):
        if (not isinstance(rel, str) or not isinstance(old, dict)
                or (rel and (not PurePosixPath(rel).parts or
                   any(p in {".", "..", ""} for p in rel.split("/"))
                   or "\\" in rel or rel.startswith("/")))):
            result["reason"] = "BASELINE_INVALID"
            return result
        path = root.joinpath(*rel.split("/")) if rel else root
        try:
            now = _checked_stat(path, scope)
        except FileNotFoundError:
            result["reason"] = "PREEXISTING_ENTRY_REMOVED"
            return result
        except _Unavailable as exc:
            result["reason"] = str(exc)
            return result
        except (OSError, ValueError, PermissionError, TypeError) as exc:
            result["reason"] = "READ_" + type(exc).__name__
            return result
        entry = _entry(now)
        if old.get("kind") != entry["kind"]:
            result["reason"] = "PREEXISTING_ENTRY_TYPE_CHANGED"
            return result
        if (old.get("dev"), old.get("ino")) != (entry["dev"], entry["ino"]):
            result["reason"] = "PREEXISTING_ENTRY_REPLACED"
            return result
        if entry["kind"] == "FILE":
            if (type(old.get("bytes")) is not int
                    or old["bytes"] < 0 or old["bytes"] > _MAX_BYTES
                    or not isinstance(old.get("sha256"), str)):
                result["reason"] = "BASELINE_INVALID"
                return result
            if now.st_size != old["bytes"]:
                result["reason"] = "PREEXISTING_FILE_CONTENT_CHANGED"
                return result
            if total + old["bytes"] > _MAX_BYTES:
                result["reason"] = "RECONCILIATION_BYTES_BUDGET_EXCEEDED"
                return result
            try:
                digest, length = _hash_file(path, scope, _MAX_BYTES - total)
            except (_Unavailable, OSError, ValueError, PermissionError) as exc:
                result["reason"] = "READ_" + type(exc).__name__
                return result
            if digest != old["sha256"]:
                result["reason"] = "PREEXISTING_FILE_CONTENT_CHANGED"
                return result
            total += length
        result["verified_nodes"] += 1
    result.update(status="VERIFIED", reason="PREEXISTING_CONTENTS_PRESERVED",
                  verified_bytes=total)
    return result


__all__ = ["SCHEMA", "capture_existing_tree", "reconcile_existing_tree"]
