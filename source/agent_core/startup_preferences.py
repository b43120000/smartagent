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
) -> dict[str, Any]:
    selected = {
        "workspace": str(workspace),
        "workspace_rel": _workspace_relative_to_project(str(workspace)),
        "workspace_rel_base": "AGENT_PROJECT_ROOT",
        "gpt_url": str(gpt_url),
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
