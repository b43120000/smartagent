"""Generic command capability classification; never grant permissions.

The positive read grammar remains authoritative. Unknown commands require
human approval *in addition to* normal schema, scope and command security.
"""
from __future__ import annotations

import re
from typing import Mapping

from .action_dependencies import command_is_proven_observation
from .command_security import inspect_command

# These signatures intentionally cover common plainly side-effecting commands;
# unknown syntaxes are not declared safe or granted read-only status.
_MUTATION = re.compile(
    r"""(?ix)^\s*(?:
       git(?:\.exe)?(?:\s+-C\s+(?:'[^']+'|"[^"]+"|\S+))?\s+
          (?:clone|fetch|pull|push|checkout|sparse-checkout|switch|reset|stash|add|commit|init)\b
       |(?:Copy-Item|Move-Item|New-Item|Set-Content|Add-Content|Out-File|
          Remove-Item|Rename-Item|Start-Process|Stop-Process)\b
       |(?:cmake\s+--build|msbuild|ninja|dotnet\s+build|cargo\s+build|
          python\s+-m\s+pip\s+install)\b
    )""",
)


def classify_command(action: Mapping) -> str:
    """One of PROVEN_READ, MUTATING, UNKNOWN, FORBIDDEN.

    No model operation label, user consent, or executable name can turn an
    unknown command into PROVEN_READ.
    """
    if str(action.get("tool") or "") != "run_command":
        return "FORBIDDEN"
    command = str(action.get("command") or "")
    hard = inspect_command(command)
    if not hard.allowed and not hard.approval_eligible:
        return "FORBIDDEN"
    if command_is_proven_observation(action):
        return "PROVEN_READ"
    if hard.approval_eligible:
        return "UNKNOWN"  # Separate executable approval remains mandatory.
    # A compound command is not fully classified by its first verb.
    if any(token in command for token in (";", "&&", "||", "|", "\n", "\r", "`", "$(")):
        return "UNKNOWN"
    if _MUTATION.match(command):
        return "MUTATING"
    return "UNKNOWN"


__all__ = ["classify_command"]
