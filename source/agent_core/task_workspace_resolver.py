"""Resolve task working directories without treating WebGPT bindings as authority.

Resolution order: explicit current request, explicit Task continuation, at most
three trusted completed/recoverable tasks in the same conversation, then the
configured primary writable root. There is intentionally no time-based TTL.

A request path selects a working directory; it NEVER grants filesystem access.
All returned directories must already reside inside the writable policy.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .path_security import is_within, reject_reparse_components
from .workspace_access import access_snapshot

MAX_RECENT_TASKS = 3
# Conservative shell-path recognizer. In commands, the first whitespace ends
# an unquoted executable path: "collect.bat TASK-..." must not become one path.
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/])(?:[^\s'\"，,；;。\r\n]*)")
_QUOTED_PATH = re.compile(r"""(["'])([A-Za-z]:[\\/][^"'\\r\\n]+)\1""")
_RECOVERABLE = frozenset({"COMPLETED", "FAILED", "PAUSED", "INTERRUPTED"})


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve())).casefold()


def _policy_roots() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    policy = access_snapshot()
    writable = tuple(Path(v).resolve() for v in policy.get("writable_workspaces", ()))
    readable = tuple(Path(v).resolve() for v in policy.get("read_only_roots", ()))
    if not writable:
        raise ValueError("workspace_writable_roots_not_configured")
    return writable, readable


def _authorized(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        if is_within(path, root):
            reject_reparse_components(path, root)
            return True
    return False


def request_paths(text: str) -> list[str]:
    """Identify explicit Windows paths; never consume trailing shell arguments.

    Unquoted paths containing spaces are ambiguous. Users can quote them.
    Exclude any token already covered by a quoted path; de-duplicate safely.
    """
    value = str(text or "")
    candidates: list[tuple[int, int, str]] = []
    quoted = []
    for match in _QUOTED_PATH.finditer(value):
        quoted.append((match.start(2), match.end(2)))
        candidates.append((match.start(2), match.end(2), match.group(2)))
    for match in _WINDOWS_PATH.finditer(value):
        if any(start <= match.start() < end for start, end in quoted):
            continue
        candidates.append((match.start(), match.end(), match.group().rstrip(".,;，；。")))
    candidates.sort(key=lambda row: row[0])
    result = []
    for _, _, path in candidates:
        if path and path not in result:
            result.append(path)
    return result


def _directory_for_path(raw: str, writable: tuple[Path, ...], readable: tuple[Path, ...]) -> Path | None:
    requested = Path(raw).expanduser().resolve(strict=False)
    if not _authorized(requested, writable):
        if _authorized(requested, readable):
            return None  # Read-only target is not a working directory.
        raise ValueError("request_path_outside_authorized_roots:" + str(requested))
    directory = requested if requested.is_dir() else requested.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    if not directory.is_dir() or not _authorized(directory, writable):
        raise ValueError("request_working_directory_unavailable:" + str(requested))
    return directory


def _recent_candidates(store, *, conversation_key: str, transport: str, sender_id: str) -> list[dict]:
    # TaskStore is cross-process: refresh under its durable state lock.
    with store.process_lock():
        store.load()
        records = list(store.all())
    result: list[dict] = []
    for task in sorted(records, key=lambda item: float(getattr(item, "created_at", 0)), reverse=True):
        if str(task.conversation_url) != conversation_key:
            continue
        meta = task.metadata or {}
        if str(meta.get("transport", "")).upper() != transport.upper():
            continue
        if sender_id and str(meta.get("sender_id", "")) != sender_id:
            continue
        if str(task.state).upper() not in _RECOVERABLE:
            continue
        if not str(task.workspace or ""):
            continue
        result.append({"task_id": task.task_id, "workspace": str(task.workspace)})
        if len(result) >= MAX_RECENT_TASKS:
            break
    return result


def resolve_task_workspace(
    request: str,
    *,
    conversation_key: str,
    transport: str,
    sender_id: str,
    store,
) -> dict[str, Any]:
    """Return a request-owned working directory with auditable provenance.

    The selected directory is never taken from user-supplied tool metadata.
    Explicit unknown paths fail closed instead of silently choosing a default.
    """
    writable, readable = _policy_roots()
    paths = request_paths(request)
    selected: Path | None = None
    source = ""
    source_task_id = ""
    for path in paths:
        candidate = _directory_for_path(path, writable, readable)
        if candidate is not None and selected is None:
            selected = candidate
            source = "CURRENT_REQUEST"
    if selected is not None:
        return {"workspace": str(selected), "source": source,
                "source_task_id": "", "context_candidates": [],
                "explicit_paths": paths}

    # Only a signed task journal can give a historical Task ID authority;
    # matching the text alone does not authorize a new filesystem root.
    from .task_continuation import parse_continuation
    continuation = parse_continuation(request)
    recent = _recent_candidates(
        store, conversation_key=conversation_key,
        transport=transport, sender_id=sender_id,
    )
    if continuation and continuation.get("ids"):
        ids = {str(item).upper() for item in continuation["ids"]}
        with store.process_lock():
            store.load()
            records = list(store.all())
        matches = [
            task for task in records
            if (str(task.task_id).upper() in ids or str(task.request_id).upper() in ids)
            and str(task.conversation_url) == conversation_key
            and str((task.metadata or {}).get("transport", "")).upper() == transport.upper()
            and (not sender_id or str((task.metadata or {}).get("sender_id", "")) == sender_id)
        ]
        if len(matches) != 1:
            raise ValueError("continuation_workspace_reference_not_found_or_ambiguous")
        task = matches[0]
        source_task_id = task.task_id
        selected = Path(task.workspace).resolve()
        source = "EXPLICIT_CONTINUATION"
        if not selected.is_dir() or not _authorized(selected, writable):
            raise ValueError("continuation_workspace_no_longer_writable")

    filtered = []
    for row in recent:
        candidate = Path(row["workspace"]).resolve()
        if candidate.is_dir() and _authorized(candidate, writable):
            filtered.append(row)
    if selected is None and filtered:
        unique = {_key(row["workspace"]) for row in filtered}
        if len(unique) == 1:
            selected = Path(filtered[0]["workspace"]).resolve()
            source, source_task_id = "RECENT_TASK_UNIQUE", filtered[0]["task_id"]
        else:
            # Only a textual exact project-directory name can disambiguate
            # without letting an LLM fabricate a path or permissions.
            matching = [
                row for row in filtered
                if len(Path(row["workspace"]).name) >= 3
                and re.search(r"(?<![A-Za-z0-9_])" + re.escape(Path(row["workspace"]).name)
                              + r"(?![A-Za-z0-9_])", request, re.I)
            ]
            if len({_key(row["workspace"]) for row in matching}) == 1 and matching:
                selected = Path(matching[0]["workspace"]).resolve()
                source, source_task_id = "RECENT_TASK_NAMED", matching[0]["task_id"]

    if selected is None:
        # The primary writable root is configured as the first WorkspaceContainer
        # in Edit_workspace Security setup. It is not the old WebGPT binding.
        selected = writable[0]
        source = "WRITABLE_PRIMARY_DEFAULT"
    if not selected.is_dir() or not _authorized(selected, writable):
        raise ValueError("resolved_workspace_no_longer_writable")
    return {"workspace": str(selected), "source": source,
            "source_task_id": source_task_id,
            "context_candidates": filtered,
            "explicit_paths": paths}
