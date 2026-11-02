"""Request-scoped *ceiling* for explicitly restricted PR-B/C/D project writes.

Only a trusted original request can activate this restrictive policy. It does
not grant any permission: canonical workspace ACL/authorization, PR-C ledger
and command approval remain independent gates. Unsupported write shapes are
rejected until a verified PR-C/D adapter is introduced.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Mapping

from .action_dependencies import command_is_proven_observation

_WRITE_RESTRICTION = "禁止其他專案路徑寫入"
_TARGET = re.compile(
    r"(?im)^\s*允許套件安裝目標\s*[：:]\s*([^\r\n]+)"
)
_METADATA = re.compile(
    r"(?im)^\s*另允許必要的\s*Project Environment metadata\s*寫入\s*[：:]\s*([^\r\n]+)"
)
_READ_TOOLS = frozenset({
    "list_directory", "inspect_directory", "find_file",
    "read_file", "observe_fact", "inspect_project_scope",
    "inspect_project_working_set", "extract_project_dependencies",
    "compare_project_snapshot",
})


def parse_bounded_project_writes(
    request: str, *, explicit_paths: list[str], project_root: str | Path,
) -> dict | None:
    """Read a narrow, deterministic restriction; never infer a new grant."""
    text = str(request or "")
    if _WRITE_RESTRICTION not in text:
        return None
    bound = {
        "schema": "SMARTAGENT_PR_BCD_REQUEST_WRITE_CEILING_V1",
        "active": True, "valid": False, "reason": "DECLARED_WRITE_TARGETS_UNRESOLVED",
        "project_root": "", "deps": "", "metadata": "",
        "grants_authorization": False,
    }
    target = _TARGET.search(text)
    metadata = _METADATA.search(text)
    if not target or not metadata or not explicit_paths:
        return bound
    try:
        root = Path(project_root).expanduser().resolve()
        deps = Path(target.group(1).strip().strip('"')).expanduser().resolve()
        meta = Path(metadata.group(1).strip().strip('"')).expanduser().resolve()
        declared = {Path(p).expanduser().resolve() for p in explicit_paths}
        if not root.is_dir() or root not in declared or deps not in declared or meta not in declared:
            return bound
        if deps == root or root not in deps.parents:
            return bound
        if meta == root or root not in meta.parents or deps in meta.parents:
            return bound
        if meta.name.lower() != "environment.json":
            return bound
        if not deps.is_dir():
            return bound
    except (OSError, ValueError, RuntimeError):
        return bound
    bound.update(valid=True, reason="REQUEST_WRITE_CEILING_READY",
                 project_root=str(root), deps=str(deps), metadata=str(meta))
    return bound


def check_action_against_write_ceiling(
    action: Mapping[str, object], ceiling: Mapping[str, object] | None,
) -> tuple[bool, str]:
    """Fail closed without executing anything; this never approves a write."""
    if ceiling is None:
        return True, "NO_REQUEST_SPECIFIC_CEILING"
    tool = str(action.get("tool", ""))
    if not ceiling.get("valid"):
        if tool in _READ_TOOLS:
            return True, "READ_ONLY_ACTION_INVALID_WRITE_CEILING"
        return False, "REQUEST_WRITE_CEILING_UNRESOLVED"
    if tool in _READ_TOOLS:
        return True, "READ_ONLY_ACTION"
    if tool == "run_command":
        if command_is_proven_observation(action):
            return True, "PROVEN_READ_ONLY_COMMAND"
        # PR-C/D needs a separate, effect-bounded install/verify adapter.
        # A Planner label such as INSPECT or MUTATE is not sufficient proof.
        return False, "REQUEST_WRITE_CEILING_COMMAND_UNVERIFIED"
    return False, "REQUEST_WRITE_CEILING_TOOL_NOT_AUTHORIZED"
