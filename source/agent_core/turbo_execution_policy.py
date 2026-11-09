"""Opt-in Turbo: evidence-producing confirmation policy, not filesystem authority.

Second-gate auto confirmation is permitted only after canonical Action Admission
and only for proven non-destructive single commands. Nothing here grants ACL,
network, write ceiling, an outside executable, or permission to overwrite/delete.

Git cloning to a NEW target, Git archive to a NEW tar, and tar extraction to an
EMPTY target are the supported generic, constrained deployment building blocks.
Arbitrary shell/scripts, checkout of an existing tree, pipelines, and unproven
mutations continue to require normal gates. No Case D names or hashes embedded.
"""
from __future__ import annotations

import re
import shlex
import tarfile
from pathlib import Path
from collections.abc import Mapping

from .workspace_access import access_snapshot, is_valid_turbo_root
from .path_security import is_within, reject_reparse_components
from .command_security import inspect_command

_SAFE_GIT = re.compile(
    r"(?i)^git(?:\.exe)?\s+(?:"
    r"(?:-C\s+('[^'\r\n]+'|\"[^\"\r\n]+\")\s+)?"
    r"(?:status|diff|log|show|rev-parse|ls-files|ls-tree|cat-file)"
    r")\b(?:\s+[^;&|<>$\r\n" + chr(96) + r"]+)?$"
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
_SHELL_META = ";\r\n|&<>$(){}@" + chr(96)
_SHA = re.compile(r"(?i)[0-9a-f]{40}")
_REPO_URL = re.compile(r"(?i)https://[a-z0-9.-]+/[a-z0-9._/-]+(?:\.git)?")
_BAD_ARCHIVE_PARTS = {".", ".."}


def _args(command: str) -> list[str]:
    # PowerShell is our actual execution shell. Accept no escaping,
    # substitutions, redirections, pipelines or embedded newlines.
    tokens = shlex.split(command, posix=False)
    return [token[1:-1] if len(token) >= 2
            and token[0] == token[-1] and token[0] in ("'", '"')
            else token for token in tokens]


def _path(raw: str, cwd: Path, root: Path) -> Path | None:
    try:
        value = str(raw)
        if (not value or any(c in value for c in "*?[]\r\n")
                or ".." in Path(value).parts):
            return None
        source = Path(value).expanduser()
        target = (source if source.is_absolute() else cwd / source).absolute()
        if not is_within(target, root):
            return None
        reject_reparse_components(target, root)
        resolved = target.resolve(strict=False)
        return resolved if is_within(resolved, root) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _safe_archive_members(archive: Path) -> bool:
    try:
        if not archive.is_file() or archive.stat().st_size > 256 * 1024 * 1024:
            return False
        count = 0
        total = 0
        paths = set()
        with tarfile.open(archive, mode="r:*") as source:
            for entry in source:
                count += 1
                if count > 10000 or not (entry.isfile() or entry.isdir()):
                    return False
                name = entry.name.replace("\\", "/")
                if (name.startswith(("/", "\\")) or ":" in name
                        or "\x00" in name or not name.strip()
                        or any(p == ".." for p in name.split("/"))):
                    return False
                parts = [p for p in name.split("/") if p and p != "."]
                if not parts or any(part.rstrip(". ").upper() in {
                    "CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                    *("LPT" + str(i) for i in range(1, 10)),
                } for part in parts):
                    return False
                normalized = "/".join(parts).casefold()
                if normalized in paths:
                    return False
                paths.add(normalized)
                total += max(0, entry.size)
                if total > 512 * 1024 * 1024:
                    return False
        return count > 0
    except (OSError, tarfile.TarError, ValueError):
        return False


def _new_clone(command: list[str], cwd: Path, root: Path,
               original_request: str) -> tuple[bool, str]:
    args = command[2:]
    flags = {"--no-checkout", "--filter=blob:none", "--depth=1", "--no-tags"}
    if args and args[0] in flags:
        while args and args[0] in flags:
            args = args[1:]
    if len(args) != 2 or not _REPO_URL.fullmatch(args[0]):
        return False, "TURBO_GIT_CLONE_GRAMMAR_UNPROVEN"
    url, raw_destination = args
    if url not in original_request:
        return False, "TURBO_REMOTE_NOT_BOUND_TO_REQUEST"
    target = _path(raw_destination, cwd, root)
    if target is None:
        return False, "TURBO_TARGET_OUT_OF_SCOPE"
    if target.exists():
        return False, "TURBO_CLONE_TARGET_ALREADY_EXISTS"
    if not target.parent.is_dir():
        return False, "TURBO_CLONE_PARENT_MISSING"
    return True, "TURBO_NEW_GIT_CLONE"


def _archive_git(command: list[str], cwd: Path, root: Path,
                 original_request: str) -> tuple[bool, str]:
    # git -C <repo> archive --format=tar --output=<new.tar> SHA [subdir]
    if len(command) not in (7, 8) or command[1] != "-C" or command[3] != "archive":
        return False, "TURBO_ARCHIVE_GRAMMAR_UNPROVEN"
    repo = _path(command[2], cwd, root)
    if repo is None or not repo.is_dir() or not (repo / ".git").exists():
        return False, "TURBO_ARCHIVE_SOURCE_UNVERIFIED"
    if command[4] != "--format=tar" or not command[5].startswith("--output="):
        return False, "TURBO_ARCHIVE_OPTIONS_UNPROVEN"
    if not _SHA.fullmatch(command[6]) or command[6] not in original_request:
        return False, "TURBO_ARCHIVE_COMMIT_NOT_REQUEST_BOUND"
    if len(command) == 8 and (
        command[7].startswith(("/", "-", "\\")) or ".." in command[7].replace("\\", "/").split("/")):
        return False, "TURBO_ARCHIVE_SUBPATH_UNSAFE"
    output = _path(command[5].split("=", 1)[1], cwd, root)
    if output is None or output.suffix.lower() != ".tar":
        return False, "TURBO_ARCHIVE_OUTPUT_OUT_OF_SCOPE"
    if output.exists() or not output.parent.is_dir():
        return False, "TURBO_ARCHIVE_MAY_OVERWRITE_OR_PARENT_MISSING"
    return True, "TURBO_GIT_ARCHIVE_TO_NEW_FILE"


def _tar_operation(args: list[str], cwd: Path, root: Path) -> tuple[bool, str]:
    if len(args) == 3 and args[1] == "-tf":
        archive = _path(args[2], cwd, root)
        return (True, "TURBO_TAR_LIST_READ") if (
            archive is not None and archive.is_file()
        ) else (False, "TURBO_ARCHIVE_NOT_AVAILABLE")
    if len(args) != 5 or args[1] != "-xf" or args[3] != "-C":
        return False, "TURBO_TAR_UNSUPPORTED_GRAMMAR"
    archive = _path(args[2], cwd, root)
    destination = _path(args[4], cwd, root)
    if archive is None or destination is None or not archive.is_file():
        return False, "TURBO_TAR_INPUT_OR_DESTINATION_OUT_OF_SCOPE"
    if (not destination.is_dir() or
            any(destination.iterdir()) or destination == archive.parent):
        return False, "TURBO_TAR_DESTINATION_NOT_EMPTY_OR_MISSING"
    if not _safe_archive_members(archive):
        return False, "TURBO_TAR_ARCHIVE_UNSAFE_OR_UNVERIFIED"
    return True, "TURBO_TAR_EXTRACT_EMPTY_DESTINATION"


def evaluate_turbo_confirmation(loop, action: Mapping) -> dict:
    """Non-authorizing audit decision, called ONLY after regular admission."""
    result = {
        "schema": "SMARTAGENT_TURBO_DECISION_V2",
        "auto_confirm": False, "reason_code": "TURBO_NOT_APPLICABLE",
        "policy_revision": 0, "turbo_root": "",
        "workspace_root": str(getattr(loop, "workspace", "") or ""),
        "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
        "authorization_granted": False,
    }
    if action.get("tool") != "run_command":
        return result
    try:
        settings = access_snapshot()
        policy = settings["policy"]
        result["policy_revision"] = int(policy.get("revision", 0) or 0)
        configured = settings.get("turbo_roots") or ()
        if not configured:
            result["reason_code"] = "TURBO_DISABLED"
            return result
        workspace = Path(loop.workspace).expanduser().resolve()
        cwd = Path(action.get("working_directory") or workspace).expanduser().resolve()
        roots = [Path(value).expanduser().absolute() for value in configured]
        valid_roots = []
        for root in roots:
            if is_valid_turbo_root(root, workspace,
                                   settings["writable_workspaces"],
                                   settings["read_only_roots"],
                                   policy.get("denied_write_roots") or ()):
                valid_roots.append(root.resolve())
        if not valid_roots:
            result["reason_code"] = "TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE"
            return result
        # Fail closed for outside working directories, except source invocation
        # from the authorized workspace ancestor (clone target must be scoped).
        if not is_within(cwd, workspace):
            result["reason_code"] = "TURBO_WORKING_DIRECTORY_OUTSIDE_WORKSPACE"
            return result
        command = str(action.get("command") or "").strip()
        if (not command or len(command) > 4096
                or any(c in command for c in _SHELL_META)
                or _DISALLOWED.search(command)):
            result["reason_code"] = "TURBO_UNPROVEN_COMMAND"
            return result
        if not inspect_command(command, workspace=cwd).allowed:
            result["reason_code"] = "TURBO_COMMAND_SECURITY_NOT_ADMITTED"
            return result
        try:
            args = _args(command)
        except ValueError:
            result["reason_code"] = "TURBO_COMMAND_TOKENIZATION_FAILED"
            return result
        if not args:
            return result
        reason = "TURBO_UNSUPPORTED_COMMAND_GRAMMAR"
        for root in valid_roots:
            result["turbo_root"] = str(root)
            in_root = is_within(cwd, root)
            ok = False
            if re.fullmatch(r"(?i)git(?:\.exe)?", args[0]):
                if len(args) >= 3 and args[1].lower() == "clone":
                    ok, reason = _new_clone(args, cwd, root,
                                            str(getattr(loop, "task_request", "") or ""))
                elif len(args) >= 4 and args[1] == "-C" and args[3] == "archive":
                    ok, reason = _archive_git(args, cwd, root,
                                              str(getattr(loop, "task_request", "") or ""))
                elif _SAFE_GIT.fullmatch(command) and in_root:
                    git_repo = _path(args[2], cwd, root) if args[1:2] == ["-C"] else cwd
                    ok = git_repo is not None and is_within(git_repo, root)
                    reason = ("TURBO_KNOWN_NONDESTRUCTIVE_SINGLE_COMMAND" if ok
                              else "TURBO_GIT_REPO_OUT_OF_SCOPE")
            elif re.fullmatch(r"(?i)tar(?:\.exe)?", args[0]):
                ok, reason = _tar_operation(args, cwd, root)
            elif _SAFE_PS.fullmatch(command) and in_root:
                if args[0].casefold() in {
                    "get-childitem", "get-item", "get-content",
                    "test-path", "get-filehash",
                }:
                    lower = [part.casefold() for part in args]
                    if "-literalpath" in lower:
                        ix = lower.index("-literalpath")
                        item = _path(args[ix + 1], cwd, root) if ix + 1 < len(args) else None
                        ok = item is not None
                    reason = "TURBO_SCOPED_LITERAL_READ" if ok else "TURBO_READ_PATH_UNSAFE"
                else:
                    ok = True
                    reason = "TURBO_KNOWN_NONDESTRUCTIVE_SINGLE_COMMAND"
            elif args[0].casefold() in {"certutil", "certutil.exe"} and len(args) == 4:
                if args[1].lower() == "-hashfile" and args[3].upper() == "SHA256":
                    ok = _path(args[2], cwd, root) is not None
                    reason = "TURBO_SCOPED_FILE_HASH_READ" if ok else "TURBO_READ_PATH_UNSAFE"
            elif args[0].casefold() in {"git", "git.exe"} and len(args) in (4, 5):
                pass
            # An unrelated absolute literal path in the command is disallowed,
            # even if the command category above matched.
            if ok:
                for raw in _PATH_TOKEN.findall(command):
                    if _path(raw, cwd, root) is None:
                        ok = False
                        reason = "TURBO_COMMAND_PATH_OUT_OF_SCOPE"
                        break
            if ok:
                result["auto_confirm"] = True
                result["reason_code"] = reason
                return result
        result["reason_code"] = reason
        return result
    except (OSError, ValueError, RuntimeError, tarfile.TarError):
        result["reason_code"] = "TURBO_POLICY_UNAVAILABLE"
        return result


__all__ = ["evaluate_turbo_confirmation"]
