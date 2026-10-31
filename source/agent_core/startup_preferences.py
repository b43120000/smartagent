#!/usr/bin/env python3
"""Shared Agent0/Agent1 last-startup selection persistence."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .paths import startup_preferences_path


STARTUP_PREFERENCES = startup_preferences_path()


def _workspace_relative_to_project(workspace: str, root: str | Path = AGENT_PROJECT_ROOT) -> str:
    """Return a relocatable workspace path relative to the current SmartAgent root.

    The absolute workspace is retained for backward compatibility, while this
    relative form lets a copied release reconstruct the equivalent local path
    on another PC. Different-drive workspaces cannot be represented and return
    an empty string.
    """
    try:
        base = Path(root).expanduser().resolve()
        target = Path(str(workspace)).expanduser().resolve()
        return os.path.relpath(str(target), str(base))
    except (OSError, ValueError):
        return ""


def _resolve_saved_workspace(selected: dict[str, Any], root: str | Path = AGENT_PROJECT_ROOT) -> dict[str, Any]:
    result = dict(selected)
    relative = str(result.get("workspace_rel", "") or "").strip()
    if relative:
        rel_path = Path(relative)
        if not rel_path.is_absolute():
            try:
                result["workspace"] = str((Path(root).expanduser().resolve() / rel_path).resolve())
            except (OSError, ValueError):
                pass
    return result


def validate_gar_webgpt2_url(webgpt1_url: str, webgpt2_url: str) -> str:
    """Validate a distinct, existing ChatGPT conversation; blank disables L4 binding."""
    candidate = str(webgpt2_url or "").strip()
    if not candidate:
        return ""
    from .workspace import normalize_chatgpt_url
    from .conversation_identity import conversation_id, same_conversation

    primary = normalize_chatgpt_url(webgpt1_url)
    fresh = normalize_chatgpt_url(candidate)
    if not conversation_id(primary) or not conversation_id(fresh):
        raise ValueError("gar_webgpt2_conversation_url_required")
    if same_conversation(primary, fresh):
        raise ValueError("gar_webgpt2_must_differ_from_webgpt1")
    return fresh


def load_startup_preferences(path: str | Path = STARTUP_PREFERENCES) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or int(payload.get("version", 0) or 0) != 1:
        return {}
    selected = payload.get("selected")
    return _resolve_saved_workspace(selected) if isinstance(selected, dict) else {}


def save_startup_preferences(
    *, workspace: str, gpt_url: str, planner_key: str, executor_key: str,
    operator_key: str = "cloud_gptoss",
    path: str | Path = STARTUP_PREFERENCES,
    gar_webgpt2_url: str | None = None,
) -> dict[str, Any]:
    # None means preserve the stored URL when an existing caller updates WebGPT1
    # or its workspace; an explicit empty string clears the GAR binding.
    previous = load_startup_preferences(path) if gar_webgpt2_url is None else {}
    requested_gar_url = (
        previous.get("gar_webgpt2_url", "") if gar_webgpt2_url is None
        else gar_webgpt2_url
    )
    # Legacy setting is retained only as migration fallback. It must no longer
    # be tied to one agent's primary URL when a worker saves its startup state.
    fresh_url = str(requested_gar_url or "").strip()
    selected = {
        "workspace": str(workspace),
        "workspace_rel": _workspace_relative_to_project(str(workspace)),
        "workspace_rel_base": "AGENT_PROJECT_ROOT",
        "gpt_url": str(gpt_url),
        "gar_webgpt2_url": fresh_url,
        "planner_key": str(planner_key),
        "executor_key": str(executor_key),
        "operator_key": str(operator_key or "cloud_gptoss"),
    }
    payload = {"version": 1, "selected": selected, "updated_at": time.time()}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return selected
