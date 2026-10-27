"""Cross-install inventory classification for Direct Git deployments.

This module filters COMPARISON only. It does not rewrite Git's tracked-file
index, replace the canonical manifest, or authorize update/deployment.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Mapping

LOCAL_EXACT = frozenset({
    "config/debug_config.json",
    "config/browser_session.json",
})
LOCAL_COMPONENTS = frozenset({
    "localdata", ".agents", ".aws", ".ssh", ".codex", "__pycache__",
    "browser_profile", "dom_config.history", "defaultworkspace",
})
CANONICAL_MANIFESTS = frozenset({
    "config/protocol_manifest.json", "config/update_manifest.json",
    "config/source_build_provenance.json",
})


def classify_comparison_path(relative: str) -> str:
    """Return LOCAL_ONLY, CANONICAL_BUILD_METADATA, or VERSIONED_PRODUCT.

    The latter two remain Git-managed where already tracked; this function
    never overrides a tracked-file conflict or the strict manifest gate.
    """
    if not isinstance(relative, str):
        raise ValueError("comparison_path_invalid")
    normalized = relative.replace("\\", "/").strip()
    parts = PurePosixPath(normalized).parts
    if (not normalized or normalized.startswith("/") or
            any(part in ("", ".", "..") for part in normalized.split("/")) or
            ":" in normalized or "\x00" in normalized):
        raise ValueError("comparison_path_invalid")
    lower = tuple(part.casefold() for part in parts)
    key = "/".join(lower)
    if (any(p in LOCAL_COMPONENTS for p in lower) or
            key in LOCAL_EXACT or
            any(p.startswith(".env") for p in lower) or
            lower[-1] == "dom_config.json" or
            lower[-1].endswith(".bak")):
        return "LOCAL_ONLY"
    if key in CANONICAL_MANIFESTS:
        return "CANONICAL_BUILD_METADATA"
    return "VERSIONED_PRODUCT"


def summarize_cross_install_hashes(
    candidate_sha256: Mapping[str, str | None],
    installed_sha256: Mapping[str, str | None],
    *,
    dirty_tracked_paths: frozenset[str] = frozenset(),
) -> dict:
    """Summarize safely, without treating host markers as product hash diffs.

    Source conflicts stay visible. Canonical manifests are exempt ONLY from
    cross-install exact equality, NOT from dirty Git tracking and strict
    same-tree verification.
    """
    summary: dict[str, object] = {
        "versioned_match": [], "versioned_difference": [],
        "versioned_missing": [], "local_exempt_count": 0,
        "canonical_cross_install_exempt": [],
        "tracked_conflicts": [],
        "strict_same_tree_manifest_required": True,
        "loaded_worker_sha_attested": False,
    }
    for path in sorted(set(candidate_sha256) | set(installed_sha256)):
        kind = classify_comparison_path(path)
        if path in dirty_tracked_paths:
            summary["tracked_conflicts"].append(path)
        if kind == "LOCAL_ONLY":
            summary["local_exempt_count"] += 1
        elif kind == "CANONICAL_BUILD_METADATA":
            summary["canonical_cross_install_exempt"].append(path)
        elif path not in installed_sha256 or installed_sha256[path] is None:
            summary["versioned_missing"].append(path)
        elif (path in candidate_sha256 and
              candidate_sha256[path] == installed_sha256[path]):
            summary["versioned_match"].append(path)
        else:
            summary["versioned_difference"].append(path)
    return summary
