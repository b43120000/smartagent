"""Git62: deterministic, policy-owned routing for multiple Native Turbo Roots.

A nominated root is NOT a permission grant. This module chooses exactly one
target boundary; the Windows Broker must still verify its protected journal,
unique AppContainer SID, executable hash, NTFS ACL and kernel denial each time.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .path_security import is_reparse_path, is_within, reject_reparse_components


class TurboRootRoutingError(RuntimeError):
    """No unambiguous, safely validated policy boundary exists."""


def canonical_key(path: str | Path) -> str:
    # Match PowerShell's GetFullPath(...).TrimEnd('\\').ToUpperInvariant().
    # The key is an index only; never a credential or execution grant.
    raw = str(Path(path).expanduser().absolute())
    if os.name == "nt":
        import ntpath
        raw = ntpath.normpath(raw).rstrip("\\")
    else:
        raw = os.path.normpath(raw)
    return hashlib.sha256(raw.upper().encode("utf-8")).hexdigest()


def journal_path(secure_directory: str | Path, root: str | Path) -> Path:
    return Path(secure_directory) / "turbo_root_activations" / (
        "activation-" + canonical_key(root) + ".json"
    )


def import_journal_path(secure_directory: str | Path, root: str | Path) -> Path:
    return Path(secure_directory) / "turbo_root_activations" / (
        "import-" + canonical_key(root) + ".json"
    )


def matching_roots(path: str | Path, policy: dict,
                   *, allow_workspace_parent: bool = False) -> tuple[Path, ...]:
    """Determine the root for a *task path*, not a Planner-supplied SID.

    A Workspace parent with multiple roots is NOT a candidate for automatic
    root choice; the task must provide a CWD inside one registered root.
    """
    if path is None:
        return ()
    target = Path(path).expanduser().absolute()
    if not target.is_dir() or is_reparse_path(target):
        return ()
    roots: list[Path] = []
    for raw in policy.get("turbo_roots") or ():
        root = Path(raw).expanduser().absolute()
        if not root.is_dir() or is_reparse_path(root):
            continue
        if is_within(target, root):
            try:
                reject_reparse_components(target, root)
            except (OSError, RuntimeError, ValueError):
                continue
            roots.append(root)
        elif allow_workspace_parent and is_within(root, target):
            roots.append(root)
    return tuple(roots)


def resolve_root(path: str | Path, policy: dict,
                 *, require_cwd_inside: bool = True) -> Path:
    """Fail closed on zero, nested or ambiguous matches."""
    roots = matching_roots(path, policy,
                           allow_workspace_parent=not require_cwd_inside)
    if len(roots) != 1:
        raise TurboRootRoutingError(
            "TURBO_MULTI_ROOT_NOT_BOUND" if not roots
            else "TURBO_MULTI_ROOT_AMBIGUOUS"
        )
    root = roots[0]
    # Different policy entries may overlap; never choose 'first' or 'longest'
    # and never silently widen a process's OS authorization boundary.
    registered = tuple(Path(r).expanduser().absolute()
                       for r in policy.get("turbo_roots") or ())
    if any(other != root and (is_within(other, root) or is_within(root, other))
           for other in registered):
        raise TurboRootRoutingError("TURBO_MULTI_ROOT_OVERLAP")
    return root


def owning_workspace(root: str | Path, policy: dict) -> Path:
    boundary = Path(root).expanduser().absolute()
    candidates = []
    for item in policy.get("writable_workspaces") or ():
        ws = Path(item).expanduser().absolute()
        if boundary != ws and is_within(boundary, ws):
            candidates.append(ws)
    if len(candidates) != 1:
        raise TurboRootRoutingError("TURBO_MULTI_WORKSPACE_NOT_UNIQUE")
    return candidates[0]


def validate_root_set(roots: tuple[str, ...] | list[str]) -> None:
    ordered = [Path(r).expanduser().absolute() for r in roots]
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            if is_within(left, right) or is_within(right, left):
                raise ValueError("turbo_roots_overlap:" + str(left) + ":" + str(right))


__all__ = ["TurboRootRoutingError", "canonical_key", "journal_path",
           "import_journal_path", "resolve_root", "owning_workspace",
           "matching_roots", "validate_root_set"]
