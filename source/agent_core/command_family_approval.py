"""Bounded, task-scoped consent for matching command structure and workspace paths.

Never bypasses Tool Admission, command security, Workspace scope or verification.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
import shlex
import time
from pathlib import Path, PureWindowsPath
from typing import Mapping

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock
from .security_approval import SecurityApprovalLedger

SCHEMA = "SMARTAGENT_TASK_COMMAND_FAMILY_GRANTS_V1"
TTL_SECONDS = 3600
_EXCLUDED = {
    "rm", "ri", "rd", "rmdir", "del", "erase", "remove-item",
    "robocopy", "xcopy", "takeown", "icacls", "reg", "reg.exe",
    "sc", "sc.exe", "schtasks", "shutdown", "start-process",
    "invoke-expression", "iex", "cmd", "cmd.exe", "powershell",
    "powershell.exe", "pwsh", "pwsh.exe", "bash", "sh", "wsl",
    "curl", "curl.exe", "wget",
}
_SCRIPT_SUFFIX = (".exe", ".com", ".bat", ".cmd", ".ps1", ".py",
                  ".pyw", ".js", ".mjs", ".vbs", ".wsf", ".hta", ".jar")
_PATH_FLAGS = {"-path", "-literalpath", "-destination", "-source", "-outfile",
               "-filepath", "--output", "--output-dir", "--directory", "-c", "--cwd"}
_GIT_COMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files",
                 "fetch", "clone"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unquote(text: str) -> str:
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"') else text


def _inside(path: str, workspace: str) -> bool:
    try:
        root_win = PureWindowsPath(workspace)
        if root_win.is_absolute():
            target_win = PureWindowsPath(path)
            if not target_win.is_absolute():
                target_win = root_win / target_win
            root = ntpath.normcase(ntpath.normpath(str(root_win)))
            target = ntpath.normcase(ntpath.normpath(str(target_win)))
            return ntpath.commonpath([root, target]) == root
        if re.match(r"(?i)^[a-z]:[\\/]|^\\\\", path):
            return False  # Do not treat Windows absolute paths as POSIX relative paths.
        root = Path(workspace).resolve()
        target = Path(path)
        return (target if target.is_absolute() else root / target).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _path_token(token: str, preceding: str, workspace: str) -> bool:
    if not token or "://" in token or any(ch in token for ch in "*?{}$%") or token.lower().endswith(_SCRIPT_SUFFIX):
        return False
    explicit = bool(re.match(r"(?i)^[a-z]:[\\/]|^\\\\|^/|^\.[\\/]|^\.\.[\\/]", token))
    relative = ("\\" in token or "/" in token) and not token.startswith("-")
    return (explicit or relative or preceding.lower() in _PATH_FLAGS) and _inside(token, workspace)


def command_family_signature(action: Mapping, workspace: str) -> str | None:
    if str(action.get("tool") or "") != "run_command":
        return None
    command = str(action.get("command") or "").strip()
    operation = str(action.get("operation") or "").strip()
    cwd = str(action.get("working_directory") or workspace)
    if not command or not operation or len(command) > 8192 or not _inside(cwd, workspace):
        return None
    # A family is never inferred for compound shells, substitutions or redirects.
    if any(ch in command for ch in ("\r", "\n", ";", "|", "&", ">", "<", chr(96), "$", "%")):
        return None
    try:
        tokens = [_unquote(t) for t in shlex.split(command, posix=False)]
    except ValueError:
        return None
    if not tokens or not tokens[0] or any(not t for t in tokens):
        return None
    executable = ntpath.basename(tokens[0].replace("/", "\\")).casefold()
    if executable in _EXCLUDED:
        return None
    if executable in {"python", "python.exe", "py", "py.exe", "node", "node.exe"}:
        if any(t.casefold() in {"-c", "-e", "--eval", "--require", "-m"} for t in tokens[1:]):
            return None
    if executable in {"git", "git.exe"}:
        index = 1
        while index < len(tokens) and tokens[index] in {"-C", "-c"}:
            index += 2
        if index >= len(tokens) or tokens[index].casefold() not in _GIT_COMMANDS:
            return None
    normalized = [
        "<WORKSPACE_PATH>" if i and _path_token(t, tokens[i - 1], workspace) else t
        for i, t in enumerate(tokens)
    ]
    # A relative script/program resolves differently in a different cwd. Never
    # generalize that situation, even when the text of the script is identical.
    relative_program = (tokens[0].lower().endswith(_SCRIPT_SUFFIX)
                        and not PureWindowsPath(tokens[0]).is_absolute()
                        and not Path(tokens[0]).is_absolute())
    relative_script = executable in {"python", "python.exe", "py", "py.exe", "node", "node.exe"}
    # An interpreter may locate relative modules/scripts through its cwd,
    # even if flags precede the script path. Keep cwd fixed for all of them.
    payload = {"schema": SCHEMA, "operation": operation, "argv_template": normalized}
    if relative_program or relative_script:
        payload["bound_cwd"] = str(Path(cwd).resolve())
    return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")))


class TaskCommandFamilyGrants:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = SecurityApprovalLedger(self.root).path.with_name("command_family_grants.json")
        self.lock = self.path.with_suffix(".lock")

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "grants": {}}
        state = read_json_retry(self.path)
        if not isinstance(state, dict) or state.get("schema") != SCHEMA or not isinstance(state.get("grants"), dict):
            raise ValueError("command_family_grant_store_invalid")
        return state

    @staticmethod
    def _scope(loop):
        scope = {
            "task_id": str(getattr(loop, "task_id", "") or ""),
            "request_id": str(getattr(loop, "run_id", "") or ""),
            "epoch": str(getattr(loop, "task_epoch", "") or ""),
            "owner": str(getattr(loop, "continuation_owner", "") or ""),
            "workspace": str(Path(loop.workspace).resolve()),
        }
        return scope if all(scope.values()) else None

    def _key(self, scope: dict, signature: str) -> str:
        return _sha(json.dumps([scope, signature], sort_keys=True))

    def remember(self, *, loop, action: Mapping, approval_id: str, evidence_ref: str) -> bool:
        scope = self._scope(loop)
        if not scope or not approval_id or not evidence_ref:
            return False
        signature = command_family_signature(action, scope["workspace"])
        if not signature:
            return False
        now = time.time()
        with exclusive_process_lock(self.lock, label="command-family"):
            state = self._load()
            state["grants"][self._key(scope, signature)] = {
                "scope": scope, "signature": signature, "approval_id": str(approval_id),
                "evidence_ref": str(evidence_ref), "granted_at": now,
                "expires_at": now + TTL_SECONDS,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, state)
        return True

    def match(self, *, loop, action: Mapping) -> dict | None:
        scope = self._scope(loop)
        if not scope:
            return None
        signature = command_family_signature(action, scope["workspace"])
        if not signature:
            return None
        with exclusive_process_lock(self.lock, label="command-family"):
            grant = self._load()["grants"].get(self._key(scope, signature))
            if (not grant or grant.get("scope") != scope
                    or grant.get("signature") != signature
                    or float(grant.get("expires_at", 0)) <= time.time()):
                return None
            return {
                "state": "APPROVED", "reason": "same_task_command_family_reused",
                "approval_id": str(grant["approval_id"]),
                "command_sha256": _sha(str(action.get("command") or "")),
            }


__all__ = ["TaskCommandFamilyGrants", "command_family_signature"]
