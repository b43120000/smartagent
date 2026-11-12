"""Turbo secondary confirmation policy, never filesystem/OS authority.

Git52: an enrolled and verified Native AppContainer Broker (ACL ON, Turbo ON)
allows full-access command grammar *within its OS-confined Turbo Root* without
an extra UNKNOWN-command human prompt. All hard command-security, canonical
Action Admission and native per-action OS attestation still apply. This does
not grant ACL, network, outside-root writes, or a host-executor fallback.

Legacy software confirmation recognizers remain disabled unless an
independently verified native path is selected. No task-specific hardcoding.
"""
from __future__ import annotations

import re
import shlex
import tarfile
from pathlib import Path
from collections.abc import Mapping

from .workspace_access import access_snapshot, is_valid_turbo_root
from .path_security import is_within, reject_reparse_components
from .command_security import inspect_command, inspect_native_command

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
_PATH_TOKEN = re.compile(r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\)[^\s'\";|&<>]+")
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
        regular_files = set()
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
                if not parts:
                    return False
                reserved = {"CON", "PRN", "AUX", "NUL"} | {
                    prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
                }
                for part in parts:
                    if (len(part) > 255 or part != part.rstrip(" .")
                            or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part)
                            or part.split(".", 1)[0].upper() in reserved):
                        return False
                normalized = "/".join(parts).casefold()
                if normalized in paths:
                    return False
                for i in range(1, len(parts)):
                    if "/".join(parts[:i]).casefold() in regular_files:
                        return False
                if entry.isfile() and any(
                    known.startswith(normalized + "/") for known in paths
                ):
                    return False
                paths.add(normalized)
                if entry.isfile():
                    regular_files.add(normalized)
                total += max(0, entry.size)
                if total > 512 * 1024 * 1024:
                    return False
        return count > 0
    except (OSError, tarfile.TarError, ValueError):
        return False


def _safe_git_read(tokens: list[str], cwd: Path, root: Path) -> bool:
    """Constrained read signatures, not a verb-prefix allowlist.

    E.g. git show/diff --output may write files and --ext-diff may execute
    external programs. Never accept arbitrary flags for a known read verb.
    """
    argv = tokens[1:]
    if len(argv) >= 2 and argv[0] == "-C":
        repo = _path(argv[1], cwd, root)
        argv = argv[2:]
    else:
        repo = cwd
    if repo is None or not is_within(repo, root) or not argv:
        return False
    verb, args = argv[0].casefold(), argv[1:]
    case = {
        "status": [[], ["--short"], ["--porcelain"], ["--porcelain=v1"]],
        "rev-parse": [["HEAD"], ["--is-inside-work-tree"], ["--show-toplevel"]],
        "branch": [["--show-current"]],
        "log": [["-1"], ["-1", "--oneline"]],
        "diff": [["--stat"], ["--name-only"]],
        "ls-files": [[], ["--stage"], ["--cached"], ["--others", "--exclude-standard"]],
        "ls-tree": [["HEAD"], ["-r", "HEAD"], ["--name-only", "HEAD"]],
        "show": [["--stat", "HEAD"], ["--stat", "--oneline", "HEAD"]],
        "cat-file": [],
    }
    if verb not in case:
        return False
    if args in case[verb]:
        return True
    if verb in {"cat-file", "ls-tree"} and len(args) == 2:
        return (
            (verb == "cat-file" and args[0] in {"-p", "-t", "-s"} and
             bool(_SHA.fullmatch(args[1])))
            or (verb == "ls-tree" and args[0] == "-r" and
                bool(_SHA.fullmatch(args[1])))
        )
    if verb == "show" and len(args) == 2:
        return args[0] == "--stat" and bool(_SHA.fullmatch(args[1]))
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
    if repo is None or not repo.is_dir() or not (repo / ".git").is_dir():
        return False, "TURBO_ARCHIVE_SOURCE_UNVERIFIED"
    if _path(str(repo / ".git"), cwd, root) is None:
        return False, "TURBO_ARCHIVE_GIT_METADATA_OUT_OF_SCOPE"
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



def evaluate_native_broker_confirmation(
    loop, action: Mapping, broker_gate: Mapping,
) -> dict:
    """Skip *secondary UNKNOWN-command consent* only for scoped Native Broker.

    This is NOT execution authorization: canonical Action Admission already
    ran; protected activation was verified by the Runtime-owned broker gate;
    and the real Broker must independently re-attest Windows AppContainer,
    policy revision, identity, job and kernel denial for EVERY action.
    """
    result = {
        "schema": "SMARTAGENT_TURBO_DECISION_V2",
        "auto_confirm": False,
        "reason_code": "TURBO_NATIVE_BROKER_NOT_REQUIRED",
        "policy_revision": 0,
        "turbo_root": "",
        "workspace_root": str(getattr(loop, "workspace", "") or ""),
        "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
        "authorization_granted": False,
    }
    if action.get("tool") != "run_command":
        return result
    if broker_gate.get("decision") != "BROKER_REQUIRED":
        return result
    state = broker_gate.get("state")
    if not isinstance(state, Mapping):
        result["reason_code"] = "TURBO_NATIVE_BROKER_STATE_MISSING"
        return result
    if (state.get("acl_mode") != "on"
            or state.get("turbo_requested") != "ON"
            or state.get("scope_verdict") != "TURBO_SCOPE_ALLOWED"
            or state.get("sandbox_status") == "FAILED"):
        result["reason_code"] = "TURBO_NATIVE_BROKER_POLICY_NOT_ADMITTED"
        return result

    try:
        root = Path(str(state["turbo_root"])).expanduser().absolute()
        authorization = Path(
            str(broker_gate["authorization_workspace"])
        ).expanduser().absolute()
        requested = Path(
            str(action.get("working_directory") or loop.workspace)
        ).expanduser().absolute()
        # Match the Native Broker's canonical cwd mapping: an omitted cwd at
        # the trusted parent is dispatched in the Turbo Root, not the parent.
        if requested == authorization:
            cwd = root
        elif is_within(requested, root):
            cwd = requested
        else:
            result["reason_code"] = "TURBO_NATIVE_BROKER_CWD_OUTSIDE_ROOT"
            return result
        if (not root.is_dir() or not cwd.is_dir()
                or not is_within(cwd, root)):
            result["reason_code"] = "TURBO_NATIVE_BROKER_CWD_UNAVAILABLE"
            return result
        reject_reparse_components(cwd, root)
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        result["reason_code"] = "TURBO_NATIVE_BROKER_SCOPE_UNVERIFIED"
        return result

    command = str(action.get("command") or "")
    # Hard command-security bans and explicit outside-workspace executable
    # approvals are never bypassed by Full-Access UX. Unknown grammar is
    # *not* reclassified as safe/read-only.
    command_admission = inspect_native_command(command, workspace=cwd)
    if not command_admission.allowed:
        # Preserve the specific, fixed-rule classifier code for Planner RCA.
        # CommandAdmission.detail can contain absolute executable paths and
        # other user input: never write it verbatim into a persisted event.
        import hashlib
        result["reason_code"] = "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"
        result["command_security_code"] = str(command_admission.code or "UNSPECIFIED")
        result["command_security_approval_eligible"] = bool(command_admission.approval_eligible)
        result["command_security_target_kind"] = str(command_admission.target_kind or "")
        result["command_security_detail_sha256"] = (
            hashlib.sha256(command_admission.detail.encode("utf-8")).hexdigest()
            if command_admission.detail else ""
        )
        return result
    if len(command) > 8192:
        result["reason_code"] = "TURBO_NATIVE_BROKER_COMMAND_TOO_LARGE"
        return result
    revision = state.get("policy_revision")
    if type(revision) is not int or revision < 1:
        result["reason_code"] = "TURBO_NATIVE_BROKER_POLICY_REVISION_UNVERIFIED"
        return result
    result.update({
        "auto_confirm": True,
        "reason_code": "TURBO_NATIVE_BROKER_SCOPED_FULL_ACCESS",
        "turbo_root": str(root),
        "workspace_root": str(authorization),
        "policy_revision": revision,
        "broker_admission": "BROKER_REQUIRED",
    })
    return result

def native_precheck_replan_eligible(decision: Mapping) -> bool:
    """Only an untrusted external target may be replanned, never executed.

    Explicit hard bans, uncertain policy, scope failures, and missing Broker
    are terminal. The new proposal still must pass independent admission.
    """
    return (
        decision.get("reason_code") == "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"
        and decision.get("command_security_code") == "execution_target_outside_workspace"
        and decision.get("command_security_approval_eligible") is True
        and decision.get("auto_confirm") is False
    )


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
    # Git46: a nominated Turbo root is never evidence of an OS sandbox.
    # Git45's secondary confirmation must not bypass a missing kernel boundary.
    from .turbo_sandbox import resolve_turbo_state
    sandbox = resolve_turbo_state(getattr(loop, "workspace", None))
    result["policy_revision"] = sandbox["policy_revision"]
    result["turbo_root"] = sandbox["turbo_root"]
    result["reason_code"] = (
        sandbox["reason_code"] if sandbox["turbo_requested"] == "ON"
        else "TURBO_DISABLED"
    )
    result["sandbox_status"] = sandbox["sandbox_status"]
    result["turbo_effective"] = sandbox["turbo_effective"]
    return result
    # Legacy confirmation recognizers are intentionally unreachable until
    # replacement by a verified OS-broker execution route.
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
                elif _SAFE_GIT.fullmatch(command):
                    ok = _safe_git_read(args, cwd, root)
                    reason = ("TURBO_KNOWN_NONDESTRUCTIVE_SINGLE_COMMAND" if ok
                              else "TURBO_GIT_READ_OPTIONS_UNPROVEN")
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




def native_trusted_tool_snapshot() -> dict:
    """Read-only, non-authorizing per-tool hints from operator-pinned SHA256.

    "Present", "trusted" and "executed inside AppContainer" are distinct.
    No candidate executable path or hashed secret is revealed to the Planner.
    """
    names = {"git": {"git.exe"}, "python": {"python.exe", "python3.exe"},
             "pwsh": {"pwsh.exe"}, "tar": {"tar.exe"}}
    status = {
        name: {"operator_sha256_pin_present": False,
               "trusted_executable_verified": False,
               "native_execution_verified": False}
        for name in names
    }
    try:
        from .windows_security import (
            load_security_profile, trusted_executable_records, is_trusted_executable,
        )
        profile = load_security_profile()
        rows = trusted_executable_records(profile)
        for record in rows[:128]:
            path = Path(str(record["path"]))
            for name, aliases in names.items():
                if path.name.casefold() not in aliases:
                    continue
                status[name]["operator_sha256_pin_present"] = True
                try:
                    if is_trusted_executable(path, profile):
                        status[name]["trusted_executable_verified"] = True
                except (OSError, RuntimeError, ValueError):
                    pass
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        pass
    return status


def turbo_planning_snapshot(workspace: str | Path) -> dict:
    """Advisory only: show Planner the active, exact Turbo scope.

    This snapshot can go stale during a task; the Executor still revalidates
    the *current* policy and scope on EVERY action. It grants no permissions.
    """
    try:
        from .turbo_sandbox import resolve_turbo_state
        gate = resolve_turbo_state(workspace)
        access = access_snapshot()
        from .workspace_access import resolve_turbo_authorization_workspace
        authorization_workspace = workspace
        if (gate["acl_mode"] == "on" and gate["turbo_requested"] == "ON"
                and gate["scope_verdict"] != "TURBO_SCOPE_ALLOWED"):
            parent = resolve_turbo_authorization_workspace(
                workspace, snapshot=access,
            )
            if parent is not None:
                authorization_workspace = parent
                gate = resolve_turbo_state(parent)
        from .turbo_broker_admission import assess
        broker_admission = assess(workspace, tool="run_command")
        broker_enrolled = (
            broker_admission["decision"] == "BROKER_REQUIRED"
            and gate["scope_verdict"] == "TURBO_SCOPE_ALLOWED"
        )
        policy = access["policy"]
        roots = [
            str(Path(raw).expanduser().absolute())
            for raw in access.get("turbo_roots") or ()
            if is_valid_turbo_root(
                raw, authorization_workspace, access["writable_workspaces"],
                access["read_only_roots"],
                policy.get("denied_write_roots") or (),
            )
        ]
        return {
            "schema": "SMARTAGENT_TURBO_PLANNER_HINT_V1",
            "status": (
                "BLOCKED" if gate["sandbox_status"] == "FAILED"
                else "BROKER_ENROLLED" if broker_enrolled
                else "ON" if gate["turbo_effective"] == "ON"
                else "OFF" if gate["acl_mode"] == "off" or gate["turbo_requested"] == "OFF"
                else "BLOCKED"
            ),
            "turbo_requested": gate["turbo_requested"],
            "turbo_effective": gate["turbo_effective"],
            "sandbox_status": gate["sandbox_status"],
            "reason_code": (
                "TURBO_OS_BROKER_PER_ACTION_ATTESTATION_REQUIRED"
                if broker_enrolled else gate["reason_code"]
            ),
            "native_broker_tools": (
                ["run_command", "create_directory", "write_file", "delete_path"]
                if broker_enrolled else []
            ),
            "trusted_native_tool_hints": (
                native_trusted_tool_snapshot() if broker_enrolled else {}
            ),
            "tool_hints_are_execution_authority": False,
            "per_action_os_attestation_required": broker_enrolled,
            "configured_roots": roots[:8],
            "authorization_workspace": str(authorization_workspace or ""),
            "task_working_directory": str(workspace or ""),
            "policy_revision": int(policy.get("revision", 0) or 0),
            "safe_action_examples": [
                "git clone <user-requested-https-url> <new-destination-under-turbo-root>",
                "git -C <scoped-repo> archive --format=tar --output=<new-scoped-tar> <requested-commit> [subdir]",
                "tar -tf <scoped-tar>",
                "tar -xf <verified-safe-scoped-tar> -C <existing-empty-turbo-directory>",
            ] if gate["turbo_effective"] == "ON" else [
                "Run approved PowerShell commands from the enrolled Turbo Root (OS broker reattests per action)",
                "create_directory: one new child directory of the Turbo Root",
                "write_file: bounded UTF-8 content under the Turbo Root",
                "delete_path: one regular file or empty directory only; recursive remains blocked",
            ] if broker_enrolled else [],
            "avoid": (
                "recursive typed delete, chunked writes, and host process fallback"
                if broker_enrolled else
                "compound pipelines, destructive Git, unknown scripts, overwrite"
            ),
            "runtime_admission_still_required": True,
            "authorization_granted": False,
        }
    except (OSError, ValueError, RuntimeError):
        return {
            "schema": "SMARTAGENT_TURBO_PLANNER_HINT_V1",
            "status": "UNKNOWN_FAIL_CLOSED",
            "configured_roots": [],
            "authorization_workspace": "",
            "task_working_directory": str(workspace or ""),
            "runtime_admission_still_required": True,
            "authorization_granted": False,
        }


__all__ = ["evaluate_turbo_confirmation", "evaluate_native_broker_confirmation", "turbo_planning_snapshot"]
