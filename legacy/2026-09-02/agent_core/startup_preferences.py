#!/usr/bin/env python3
"""Shared Agent0/Agent1 last-startup selection persistence."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT


STARTUP_PREFERENCES = AGENT_PROJECT_ROOT / ".agents" / "startup_preferences.json"


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
    return dict(selected) if isinstance(selected, dict) else {}


def save_startup_preferences(
    *, workspace: str, gpt_url: str, planner_key: str, executor_key: str,
    operator_key: str = "cloud_gptoss",
    path: str | Path = STARTUP_PREFERENCES,
) -> dict[str, Any]:
    selected = {
        "workspace": str(workspace),
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
