"""Version-bound opaque references for lazy project context retrieval.

References are locators, never authorization. Callers must enforce project root
authorization and verify source bytes against the indexed snapshot before use.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import PurePosixPath

PREFIX = "ctx:v1"


class ContextReferenceError(ValueError):
    """Invalid or stale version-bound project context reference."""


def _check_digest(value: str, name: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContextReferenceError(f"context_ref_invalid_{name}")
    return value


def make_context_ref(project_handle: str, snapshot_id: str, path: str, file_sha256: str) -> str:
    """Encode an immutable snapshot/file identity without embedding source bytes."""
    if not project_handle.startswith("PROJECT-"):
        raise ContextReferenceError("context_ref_invalid_project")
    _check_digest(snapshot_id, "snapshot")
    _check_digest(file_sha256, "hash")
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise ContextReferenceError("context_ref_invalid_path")
    encoded = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{PREFIX}:{project_handle}:{snapshot_id}:{file_sha256}:{encoded}"


def parse_context_ref(reference: str, project_handle: str, snapshot_id: str) -> tuple[str, str]:
    """Return (relative path, expected sha256), rejecting cross-project/snapshot refs."""
    parts = reference.split(":")
    if len(parts) != 6 or parts[:2] != ["ctx", "v1"]:
        raise ContextReferenceError("context_ref_invalid_format")
    _, _, handle, snap, digest, encoded = parts
    if handle != project_handle:
        raise ContextReferenceError("context_ref_project_mismatch")
    if snap != snapshot_id:
        raise ContextReferenceError("context_ref_snapshot_mismatch")
    _check_digest(snap, "snapshot")
    _check_digest(digest, "hash")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        path = raw.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ContextReferenceError("context_ref_invalid_path_encoding") from exc
    # Enforce canonical encoding to avoid alternate representations.
    if make_context_ref(handle, snap, path, digest) != reference:
        raise ContextReferenceError("context_ref_noncanonical")
    return path, digest
