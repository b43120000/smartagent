"""Opt-in Turbo confirmation policy: narrower than execution authorization.

Only an *explicit* verified writable workspace descendant may get an automatic
second-gate approval. Ordinary Scope / ACL / Write Ceiling / command security
must have admitted the action before this evaluator is consulted.

Unknown arbitrary shell, interpreters, destructive Git, and any compound syntax
remain human-gated. Absence of a literal 'delete' word is NOT a safety proof.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Mapping

from .workspace_access import access_snapshot, is_valid_turbo_root

_SAFE_GIT = re.compile(
    r"(?i)^git(?:\.exe)?\s+(?:"
    r"(?:-C\s+('[^'\r\n]+'|\"[^\"\r\n]+\")\s+)?"
    r"(?:status|diff|log|show|rev-parse|ls-files|ls-tree|cat-file)"
    r")\b(?:\s+[^;&|<>$\r\n\`]+)?$"
)
_SAFE_PS = re.compile(
    r"(?i)^(?:Get-Location|Get-Date|Get-Command\s+(?:git|python|cmake|gcc))$"
    r"|^(?:Get-ChildItem|Get-Item|Get-Content|Test-Path|Get-FileHash)"
    r"\s+-LiteralPath\s+('[^'\r\n]+'|\"[^\"\r\n]+\")"
    r"(?:\s+-Algorithm\s+SHA256)?$",
    re.IGNORECASE,
)
_DISALLOWED = re.compile(
    r"(?i)\b(?:remove-item|rm|del|erase|rmdir|rd|clear-content|"
    r"set-content|out-file|move-item|rename-item|stop-process|"
    r"git\s+(?:clean|reset|push|rebase)|invoke-expression|iex|"
    r"start-process|powershell|pwsh|cmd|python|py|node|bash|sh|wsl)\b"
)
_PATH_TOKEN = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s'\";|&<>]+")


def evaluate_turbo_confirmation(loop, action: Mapping) -> dict:
    """Return a non-authorizing record, never change an Action or permission."""
    result = {"schema": "SMARTAGENT_TURBO_DECISION_V1",
              "auto_confirm": False, "reason_code": "TURBO_NOT_APPLICABLE",
              "policy_revision": 0}
    if action.get("tool") != "run_command":
        return result
    try:
        settings = access_snapshot()
        policy = settings["policy"]
        result["policy_revision"] = int(policy.get("revision", 0) or 0)
        workspace = Path(loop.workspace).expanduser().resolve()
        cwd = Path(action.get("working_directory") or workspace).expanduser().resolve()
        roots = [Path(root).expanduser().resolve()
                 for root in settings.get("turbo_roots") or ()]
        accepted_root = None
        for root in roots:
            if (is_valid_turbo_root(
                    root, workspace, settings["writable_workspaces"],
                    settings["read_only_roots"],
                    policy.get("denied_write_roots") or ())
                    and (cwd == root or root in cwd.parents)):
                accepted_root = root
                break
        if accepted_root is None:
            result["reason_code"] = "TURBO_OUTSIDE_ACTIVE_ROOT"
            return result
        command = str(action.get("command") or "").strip()
        if (not command or len(command) > 4096
                or any(c in command for c in ";\r\n|&<>$(){}@" + chr(96))
                or _DISALLOWED.search(command)):
            result["reason_code"] = "TURBO_UNPROVEN_COMMAND"
            return result
        # Literal paths appearing in a free-form command must stay inside
        # the same configured Turbo root. Reject unparseable PS expressions.
        if not (_SAFE_GIT.fullmatch(command) or _SAFE_PS.fullmatch(command)):
            result["reason_code"] = "TURBO_UNSUPPORTED_COMMAND_GRAMMAR"
            return result
        from .path_security import reject_reparse_components, is_within
        # Reject provider paths, unresolved relative paths and reparse targets
        # for any PowerShell operation with an explicit file argument.
        if command.lower().startswith(("get-childitem", "get-item", "get-content",
                                       "test-path", "get-filehash")):
            from shlex import split
            try:
                tokens = split(command, posix=False)
            except ValueError:
                result["reason_code"] = "TURBO_PATH_TOKEN_INVALID"
                return result
            if "-literalpath" not in [p.casefold() for p in tokens]:
                result["reason_code"] = "TURBO_PATH_NOT_LITERAL"
                return result
            index = [p.casefold() for p in tokens].index("-literalpath")
            raw = tokens[index + 1].strip("'\\\"")
            candidate = Path(raw).expanduser().absolute()
            if (not candidate.is_absolute() or
                    not is_within(candidate, accepted_root)):
                result["reason_code"] = "TURBO_COMMAND_PATH_OUT_OF_SCOPE"
                return result
            reject_reparse_components(candidate, accepted_root)
        for raw in _PATH_TOKEN.findall(command):
            candidate = Path(raw).expanduser().absolute()
            if not is_within(candidate, accepted_root):
                result["reason_code"] = "TURBO_COMMAND_PATH_OUT_OF_SCOPE"
                return result
            reject_reparse_components(candidate, accepted_root)
        result["auto_confirm"] = True
        result["reason_code"] = "TURBO_KNOWN_NONDESTRUCTIVE_SINGLE_COMMAND"
        result["turbo_root"] = str(accepted_root)
        return result
    except (ValueError, OSError, RuntimeError):
        result["reason_code"] = "TURBO_POLICY_UNAVAILABLE"
        return result


__all__ = ["evaluate_turbo_confirmation"]
