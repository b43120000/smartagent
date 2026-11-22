"""Runtime-authored field constraints shared by Planner catalog and Admission.

No task routing, operation inference, or permission grants live here.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


def enum_constraints(tool: str) -> dict[str, list[str]]:
    """Return currently registered field enums from their canonical authority."""
    if tool == "run_command":
        from .command_operation import COMMAND_OPERATIONS
        return {"operation": sorted(COMMAND_OPERATIONS)}
    return {}


def enum_issue(tool: str, reason: str, arguments: Mapping,
               *, detail: str = "") -> dict | None:
    """Project an invalid enum into bounded safe facts; never echo commands."""
    for field, values in enum_constraints(tool).items():
        if reason not in {f"{tool}_{field}_invalid",
                          f"{tool}_{field}_mismatch"}:
            continue
        proposed = arguments.get(field)
        actual = str(proposed).strip().upper() if isinstance(proposed, str) else ""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", actual):
            actual = "[REDACTED_ENUM]"
        issue = {
            "field": field,
            "reason": "invalid_enum" if reason.endswith("_invalid") else "operation_mismatch",
            "expected_type": "enum",
            "actual_type": type(proposed).__name__ if proposed is not None else "MISSING",
            "actual_value": actual,
            "allowed_values": values[:32],
        }
        # Operation mismatch details can contain the entire raw command.
        # Extract only the registered reason token, never the shell command.
        if reason.endswith("_mismatch"):
            match = re.search(r"(?:^|;)reason=([A-Za-z0-9_]{1,80})(?:;|$)",
                              str(detail))
            if match:
                issue["constraint_code"] = match.group(1)
        return issue
    return None


__all__ = ["enum_constraints", "enum_issue"]
