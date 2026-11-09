"""Explicit human Turbo Mode controls; never inferred from Planner output.

Both Telegram and WebGPT user text must be an exact, standalone setting.
The single shared Workspace Access policy is the source of truth.
"""
from __future__ import annotations

import re
from pathlib import Path
from .workspace_access import load_workspace_access_policy, set_turbo_root

_COMMAND = re.compile(r"(?is)^\s*turbomode\s*=\s*(.+?)\s*$")


def parse_turbo_control(user_text: str) -> str | None:
    if not isinstance(user_text, str) or "\n" in user_text or "\r" in user_text:
        return None
    match = _COMMAND.fullmatch(user_text)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith(("'", '"')) and value.endswith(value[:1]):
        value = value[1:-1]
    return value if value else None


def apply_turbo_control(user_text: str, *, workspace: str | Path) -> dict | None:
    raw = parse_turbo_control(user_text)
    if raw is None:
        return None
    parent = Path(workspace).expanduser().resolve()
    if raw.casefold() in {"off", "disable", "disabled"}:
        policy = load_workspace_access_policy()
        # Only remove roots under this exact workspace. Do not disable
        # an unrelated Workspace configured by another authorized channel.
        roots = []
        for item in policy.get("turbo_roots", []):
            candidate = Path(item).expanduser().resolve()
            if candidate != parent and parent not in candidate.parents:
                roots.append(item)
        from .workspace_access import save_turbo_roots
        updated = save_turbo_roots(roots)
        return {"status": "DISABLED", "workspace": str(parent),
                "revision": updated["revision"], "turbo_roots": updated["turbo_roots"]}
    # An explicit absolute path is essential: never interpret relative
    # strings in an operator authorization command.
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ValueError("turbo_absolute_path_required")
    updated = set_turbo_root(target, workspace=parent, enabled=True)
    return {"status": "ENABLED", "workspace": str(parent),
            "path": str(target.resolve()), "revision": updated["revision"],
            "turbo_roots": updated["turbo_roots"]}


def turbo_control_reply(result: dict) -> str:
    state = str(result.get("status") or "UNKNOWN")
    roots = result.get("turbo_roots") or []
    return (
        "⚡ Turbo Mode " + state + "\n"
        + "Workspace: " + str(result.get("workspace") or "") + "\n"
        + ("Turbo path: " + str(result.get("path")) + "\n"
           if result.get("path") else "")
        + "Policy revision: " + str(result.get("revision", "")) + "\n"
        + "Shared registry: " + str(len(roots)) + " configured roots\n"
        + "Only pre-authorized in-scope operations may bypass repeated UNKNOWN confirmation."
    )


__all__ = ["parse_turbo_control", "apply_turbo_control", "turbo_control_reply"]
