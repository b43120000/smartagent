"""Git50 Windows OS AppContainer one-shot broker client.

This module NEVER executes the user script itself. It validates an
administrator-provisioned, revision-bound activation and launches only the
protected broker copy under localdata/secure/windows_security. The protected
PowerShell broker creates a native AppContainer process and verifies the
Windows token, a Job Object and a kernel-denied outside read *per action*.

There is no normal-host subprocess fallback if any attestation fails.
"""
from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .path_security import is_reparse_path, is_within
from .workspace_access import access_snapshot, workspace_access_policy_path
from .turbo_sandbox import resolve_turbo_state

RECEIPT_SCHEMA = "SMARTAGENT_TURBO_OS_EXECUTION_RECEIPT_V1"
ACTIVATION_SCHEMA = "SMARTAGENT_TURBO_OS_ACTIVATION_V1"
_ACTION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


class TurboOSBrokerUnavailable(RuntimeError):
    pass


_GIT59_EXACT_VERSION = re.compile(r"\A&\s+'([^'\r\n]+)'\s+--version\s*\Z", re.I)


def _git59_native_version_probe(command: str, project: Path) -> str:
    """Opt in ONLY to exact quoted, pinned, read-only --version commands.

    The protected broker checks the command again independently.
    """
    matched = _GIT59_EXACT_VERSION.fullmatch(str(command or "").strip())
    if not matched:
        return ""
    candidate = ntpath.normcase(ntpath.normpath(matched.group(1)))
    paths = {
        "git": ntpath.normcase(ntpath.normpath(str(
            project / ".venv" / "git" / "cmd" / "git.exe"))),
        "python": ntpath.normcase(ntpath.normpath(str(
            project / ".venv" / "Scripts" / "python.exe"))),
    }
    for name, pinned in paths.items():
        if candidate == pinned:
            return name
    return ""


def _git59_validate_direct_child(receipt: dict, requested: str, project: Path) -> dict:
    """Validate the protected broker's kernel-derived child-token receipt."""
    child = receipt.get("native_direct_child_attestation")
    if not requested:
        if receipt.get("native_executable_identity_verified") is True or child:
            raise TurboOSBrokerUnavailable("GIT59_UNREQUESTED_CHILD_ATTESTATION")
        return {"verified": False, "reason": "POWERSHELL_PARENT_ONLY_ATTESTED",
                "name": "", "exit_code": None, "pid": None,
                "stdout": "", "stderr": "", "capture_complete": False,
                "stdout_bytes": 0, "stderr_bytes": 0}
    if (receipt.get("native_direct_child_probe_kind") != requested
            or not isinstance(child, dict)
            or receipt.get("native_executable_identity_verified") is not
                (child.get("status") == "OS_TOKEN_VERIFIED")):
        raise TurboOSBrokerUnavailable("GIT59_CHILD_ATTESTATION_SCHEMA_INVALID")
    if child.get("status") != "OS_TOKEN_VERIFIED":
        if child.get("reason_code") not in {
                "GIT59_NATIVE_CHILD_CREATEPROCESS_FAILED",
                "GIT59_NATIVE_CHILD_EXECUTION_TIMEOUT",
                "GIT59_NATIVE_CHILD_TOKEN_UNVERIFIED",
                "GIT59_NATIVE_CHILD_EXIT_UNAVAILABLE"}:
            raise TurboOSBrokerUnavailable("GIT59_CHILD_ATTESTATION_REASON_INVALID")
        return {"verified": False, "reason": child["reason_code"],
                "name": requested, "exit_code": None, "pid": None,
                "stdout": "", "stderr": "", "capture_complete": False,
                "stdout_bytes": 0, "stderr_bytes": 0}
    executable = project / ".venv" / (
        "git/cmd/git.exe" if requested == "git" else "Scripts/python.exe")
    if not (
        child.get("schema") == "SMARTAGENT_GIT59_DIRECT_NATIVE_CHILD_V1"
        and child.get("sandbox_backend") == "WIN32_APPCONTAINER"
        and child.get("sandbox_identity_verified") is True
        and child.get("internet_client_token_verified") is True
        and child.get("internet_client_capability_sid") == "S-1-15-3-1"
        and child.get("child_process_policy") == "JOB_KILL_ON_CLOSE"
        and child.get("os_process_started") is True
        and child.get("executable_name") == requested
        and ntpath.normcase(str(child.get("executable_path") or ""))
            == ntpath.normcase(str(executable))
        and type(child.get("pid")) is int and child["pid"] > 0
        and type(child.get("exit_code")) is int and child["exit_code"] >= 0
    ):
        raise TurboOSBrokerUnavailable("GIT59_CHILD_OS_TOKEN_PROOF_INVALID")
    # Git60 native stdout/stderr was written by the actual OS-attested
    # executable through explicitly allowlisted, scoped standard handles.
    # Capture text remains untrusted data, not the authority for the SID.
    try:
        stdout = base64.b64decode(child["git60_child_stdout_base64"], validate=True)
        stderr = base64.b64decode(child["git60_child_stderr_base64"], validate=True)
        out_len = child["git60_child_stdout_bytes"]
        err_len = child["git60_child_stderr_bytes"]
        complete = child["git60_child_capture_complete"]
        if (
            type(out_len) is not int or type(err_len) is not int
            or type(complete) is not bool or out_len < len(stdout)
            or err_len < len(stderr)
            or len(stdout) > 32768 or len(stderr) > 32768
            or (complete is not (out_len == len(stdout) and err_len == len(stderr)))
        ):
            raise ValueError("capture_length_mismatch")
    except (TypeError, KeyError, ValueError) as exc:
        raise TurboOSBrokerUnavailable(
            "GIT60_NATIVE_DIRECT_CHILD_CAPTURE_INVALID") from exc
    return {"verified": True, "reason": "OS_DIRECT_EXECUTABLE_TOKEN_VERIFIED",
            "name": requested, "exit_code": child["exit_code"],
            "pid": child["pid"], "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "capture_complete": complete, "stdout_bytes": out_len,
            "stderr_bytes": err_len}



# Classify *known* native broker stderr without exposing a full PowerShell
# exception, command text, paths or potentially sensitive process output.
# Report only bounded native error codes, not raw stderr or paths.
_NATIVE_BROKER_REASON = re.compile(
    r"\b(?:GIT50|GIT59|GIT62|GIT63|TURBO_D3)_[A-Z0-9_]{3,120}\b"
)
_NATIVE_OS_OPERATIONS = (
    ("CreateProcessW(AppContainer)", "WIN32_CREATEPROCESS_APP_CONTAINER"),
    ("InitializeProcThreadAttributeList", "WIN32_ATTRIBUTE_LIST"),
    ("UpdateProcThreadAttribute", "WIN32_SECURITY_CAPABILITIES_ATTRIBUTE"),
    ("AssignProcessToJobObject", "WIN32_ASSIGN_JOB"),
    ("CreateJobObjectW", "WIN32_CREATE_JOB"),
    ("SetInformationJobObject", "WIN32_SET_JOB_LIMIT"),
    ("OpenProcessToken", "WIN32_OPEN_PROCESS_TOKEN"),
    ("GetTokenInformation", "WIN32_VERIFY_CHILD_TOKEN"),
    ("ResumeThread", "WIN32_RESUME_THREAD"),
    ("WaitForSingleObject", "WIN32_WAIT_FOR_CHILD"),
    ("GetExitCodeProcess", "WIN32_GET_EXIT_CODE"),
    ("appcontainer_sid_derivation_failed", "WIN32_DERIVE_APP_CONTAINER_SID"),
    ("child_token_is_not_appcontainer", "WIN32_CHILD_NOT_APP_CONTAINER"),
    ("appcontainer_child_sid_mismatch", "WIN32_CHILD_SID_MISMATCH"),
    ("appcontainer_execution_timeout", "NATIVE_CHILD_EXECUTION_TIMEOUT"),
)


def _native_broker_rejection_reason(stderr: str) -> str:
    """Return bounded non-secret classification, never raw broker stderr.

    An error code never asserts that any AppContainer child started, passed
    OS attestation or wrote to an authorized filesystem path.
    """
    base = "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED"
    if not isinstance(stderr, str):
        return base
    output = stderr[:16384]
    marker = _NATIVE_BROKER_REASON.search(output)
    if marker:
        return base + ":" + marker.group(0)
    for phrase, category in _NATIVE_OS_OPERATIONS:
        if phrase.casefold() in output.casefold():
            return base + ":" + category
    for phrase, category in (
        ("PSSecurityException", "POWERSHELL_SCRIPT_POLICY_BLOCKED"),
        ("running scripts is disabled on this system", "POWERSHELL_SCRIPT_POLICY_BLOCKED"),
        ("因為這個系統上已停用指令碼執行", "POWERSHELL_SCRIPT_POLICY_BLOCKED"),
        ("ParserError", "POWERSHELL_PARSER_ERROR"),
        ("ParameterBindingException", "POWERSHELL_PARAMETER_BINDING"),
        ("NamedParameterNotFound", "POWERSHELL_PARAMETER_BINDING"),
        ("UnauthorizedAccessException", "POWERSHELL_ACCESS_DENIED"),
        ("Access is denied", "POWERSHELL_ACCESS_DENIED"),
        ("拒絕存取", "POWERSHELL_ACCESS_DENIED"),
        ("Add-Type", "POWERSHELL_NATIVE_TYPE_COMPILATION"),
    ):
        if phrase.casefold() in output.casefold():
            return base + ":" + category
    return base


def _broker_process_env() -> dict[str, str]:
    """Remove inherited module search contamination only for Broker child.

    Starting Windows PowerShell without PSModulePath lets it reconstruct its
    native module search paths. No persistent system/user policy is changed.
    The native AppContainer child is separately given a restricted environment.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.casefold() == "psmodulepath":
            del env[key]
    return env


def _install_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _activation_path() -> Path:
    return workspace_access_policy_path().with_name("turbo_os_activation.json")


def _load_activation(workspace: str | Path) -> tuple[dict, dict, Path]:
    if os.name != "nt":
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_WINDOWS_REQUIRED")
    state = resolve_turbo_state(workspace)
    if (state.get("acl_mode") == "on" and
            state.get("turbo_requested") == "ON" and
            state.get("scope_verdict") != "TURBO_SCOPE_ALLOWED"):
        # Typed mutations may carry a task-scoped (child) workspace.
        # Bind the OS activation only to the unique protected policy parent.
        from .workspace_access import resolve_turbo_authorization_workspace
        parent = resolve_turbo_authorization_workspace(workspace)
        if parent is not None:
            workspace = parent
            state = resolve_turbo_state(workspace)
    if state.get("sandbox_status") == "FAILED":
        raise TurboOSBrokerUnavailable(str(state.get("reason_code")))
    if (state.get("acl_mode") != "on" or
            state.get("turbo_requested") != "ON" or
            state.get("scope_verdict") != "TURBO_SCOPE_ALLOWED"):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_SCOPE_NOT_REQUESTED")

    # Git62: route by actual task directory, not 'first of many'.
    from .turbo_root_registry import (
        journal_path as root_journal_path, owning_workspace,
        TurboRootRoutingError,
    )
    access = access_snapshot()
    policy = access["policy"]
    roots = tuple(access.get("turbo_roots") or ())
    root = Path(state["turbo_root"]).absolute()
    if not any(root == Path(r).absolute() for r in roots) or (
            not root.is_dir() or is_reparse_path(root)):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ROOT_NOT_VERIFIED")
    try:
        workspace = owning_workspace(root, policy)
    except TurboRootRoutingError as exc:
        raise TurboOSBrokerUnavailable(str(exc)) from exc
    secure = _activation_path().parent
    journal_path = (
        root_journal_path(secure, root) if len(roots) > 1
        else (_activation_path() if _activation_path().is_file()
              else root_journal_path(secure, root))
    )
    # A protected old-to-new switch remains non-authoritative until its
    # final durable commit. No Planner / Runtime action may enter the new
    # Broker while the switch journal is present, including after a crash.
    if secure.joinpath("turbo_existing_root_switch.json").exists():
        raise TurboOSBrokerUnavailable(
            "TURBO_OS_BROKER_SWITCH_TRANSACTION_PENDING")
    try:
        if is_reparse_path(journal_path):
            raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_JOURNAL_REPARSE")
        activation = json.loads(journal_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ACTIVATION_UNAVAILABLE") from exc
    if not isinstance(activation, dict):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ACTIVATION_INVALID")
    # A user-mode Broker must not read the administrator-only SDDL import
    # journal. Trust only the OS-protected public activation commit marker.
    if (activation.get("existing_root_import") is True and
            activation.get("existing_import_committed") is not True):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_IMPORT_NOT_COMMITTED")
    from .workspace_access import turbo_root_epoch
    try:
        expected_epoch = turbo_root_epoch(policy, root)
    except (ValueError, RuntimeError, TypeError) as exc:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ROOT_EPOCH_INVALID") from exc
    project = _install_root()
    workspace_path = Path(workspace).absolute()
    if (
        activation.get("schema") != ACTIVATION_SCHEMA
        or activation.get("status") != "ACTIVE_PROVISIONED"
        or type(activation.get("policy_revision")) is not int
        or activation["policy_revision"] != expected_epoch
        or str(project).casefold() != str(activation.get("install_root", "")).casefold()
        or str(workspace_path).casefold() != str(activation.get("workspace_root", "")).casefold()
        or str(root).casefold() != str(activation.get("turbo_root", "")).casefold()
        or not str(activation.get("package_sid", "")).startswith("S-1-15-2-")
    ):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ACTIVATION_STALE")

    code_dir = secure / "turbo_broker_code"
    broker = code_dir / "turbo_os_execute.ps1"
    if (is_reparse_path(code_dir) or is_reparse_path(broker) or
            not broker.is_file() or
            str(broker.absolute()).casefold() !=
            str(activation.get("broker_entry", "")).casefold()):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_PROTECTED_ENTRY_MISSING")
    digest = str(activation.get("broker_source_sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_SOURCE_DIGEST_MISSING")
    try:
        current = hashlib.sha256(broker.read_bytes()).hexdigest()
    except OSError as exc:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_SOURCE_UNREADABLE") from exc
    if current != digest:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_SOURCE_CHANGED")
    if type(policy.get("revision")) is not int or policy["revision"] != int(state["policy_revision"]):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_POLICY_REVISION_STALE")
    return activation, state, broker


def active_broker_available(workspace: str | Path | None) -> bool:
    """Advisory availability, NOT authorization to execute.

    Every native action still re-attests Windows ACL, policy and token.
    """
    if workspace is None:
        return False
    try:
        _load_activation(workspace)
        return True
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        # Missing security_profile.json, an inaccessible policy, or an
        # incomplete installation must be ordinary NOT_READY rather than
        # an exception escaping through central admission. Never fall back
        # to executing with the host identity.
        return False


def _resolve_workspace_and_cwd(cwd: str | Path | None) -> tuple[Path, Path]:
    from .turbo_root_registry import (
        resolve_root, owning_workspace, TurboRootRoutingError,
    )
    access = access_snapshot()
    policy = access["policy"]
    roots = tuple(access.get("turbo_roots") or ())
    if not roots:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_NO_ROOTS")
    if cwd is None and len(roots) != 1:
        raise TurboOSBrokerUnavailable("TURBO_MULTI_ROOT_CWD_REQUIRED")
    requested_cwd = Path(cwd).expanduser().absolute() if cwd else Path(roots[0]).absolute()
    try:
        root = resolve_root(requested_cwd, policy)
        workspace = owning_workspace(root, policy)
    except TurboRootRoutingError as exc:
        # Backward compatibility: a single configured root can still use
        # the parent Workspace as its CWD, but this is NEVER used with 2+.
        if (len(roots) == 1
                and requested_cwd in Path(roots[0]).absolute().parents):
            root = Path(roots[0]).absolute()
            workspace = owning_workspace(root, policy)
            if requested_cwd != workspace:
                raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_CWD_OUTSIDE_ROOT") from exc
        else:
            raise TurboOSBrokerUnavailable(str(exc)) from exc
    actual_cwd = root if requested_cwd == workspace else requested_cwd
    if not actual_cwd.is_dir() or is_reparse_path(actual_cwd):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_CWD_UNAVAILABLE")
    return workspace, actual_cwd


def _id(candidate: str | None, prefix: str) -> str:
    value = str(candidate or "").strip()
    if value:
        if not _ACTION_ID.fullmatch(value):
            raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ACTION_ID_INVALID")
        return value
    return prefix + uuid.uuid4().hex.upper()


def execute_turbo_command(
    command: str,
    *, cwd: str | Path | None = None,
    timeout: int = 30,
    max_stream_bytes: int = 1048576,
    task_id: str | None = None,
    action_id: str | None = None,
    wait_policy: str = "BOUNDED",
) -> dict[str, Any]:
    """Run a PowerShell command only through the OS-attested native broker.

    Failure never falls back to an ordinary subprocess or restricted
    executor. Broker stdout is treated as untrusted *data*, not authority.
    """
    if not isinstance(command, str) or not command.strip() or len(command) > 8192:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_COMMAND_INVALID")
    if wait_policy not in {"BOUNDED", "UNTIL_EXIT"}:
        raise TurboOSBrokerUnavailable("RUN_COMMAND_WAIT_POLICY_INVALID")
    until_exit = wait_policy == "UNTIL_EXIT"
    if type(timeout) is not int or timeout < 1 or (not until_exit and timeout > 300):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_TIMEOUT_INVALID")
    broker_timeout = min(timeout, 300)
    limit = min(max(int(max_stream_bytes), 1), 2 * 1024 * 1024)
    workspace, actual_cwd = _resolve_workspace_and_cwd(cwd)
    activation, state, broker = _load_activation(actual_cwd)
    request_task = _id(task_id, "TASK-")
    request_action = _id(action_id, "ACT-")

    # The protected broker entry has already passed policy/revision,
    # non-reparse-path and SHA256 verification in _load_activation().
    # Windows defaults to Restricted when every execution-policy scope is
    # Undefined; allow *this process* to load that verified local broker.
    # RemoteSigned, not Bypass, preserves machine/group policy precedence.
    powershell = Path(os.environ.get("SystemRoot") or r"C:\Windows") / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    args = [
        str(powershell), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "RemoteSigned", "-File",
        str(broker),
        "-ProjectRoot", str(_install_root()),
        "-Workspace", str(workspace),
        "-WorkingDirectory", str(actual_cwd),
        "-SelectedTurboRoot", str(activation["turbo_root"]),
        "-ExpectedRevision", str(state["policy_revision"]),
        "-TaskId", request_task,
        "-ActionId", request_action,
        "-CommandBase64", base64.b64encode(command.encode("utf-8")).decode("ascii"),
        "-TimeoutSeconds", str(broker_timeout),
        "-MaxOutputBytes", str(limit),
    ]
    if until_exit:
        args.append("-WaitUntilExit")
    native_version_probe = _git59_native_version_probe(command, _install_root())
    if native_version_probe:
        args.extend(["-NativeVersionProbe", native_version_probe])
    try:
        completed = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=None if until_exit else timeout + 75,
            check=False, text=True, encoding="utf-8", errors="replace",
            shell=False, env=_broker_process_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TurboOSBrokerUnavailable(
            "TURBO_OS_BROKER_TIMEOUT_EXECUTION_UNCERTAIN"
        ) from exc
    except OSError as exc:
        raise TurboOSBrokerUnavailable(
            "TURBO_OS_BROKER_HOST_LAUNCH_UNAVAILABLE"
        ) from exc
    if completed.returncode != 0:
        raise TurboOSBrokerUnavailable(
            _native_broker_rejection_reason(completed.stderr)
        )
    try:
        receipt = json.loads(completed.stdout.strip().lstrip("\ufeff"))
    except (ValueError, TypeError) as exc:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_INVALID_NATIVE_RECEIPT") from exc
    if not isinstance(receipt, dict) or not (
        receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("task_id") == request_task
        and receipt.get("action_id") == request_action
        and receipt.get("policy_revision") == state["policy_revision"]
        and receipt.get("acl_mode") == "on"
        and receipt.get("sandbox_backend") == "WIN32_APPCONTAINER"
        and receipt.get("sandbox_identity_verified") is True
        and receipt.get("kernel_outside_read_denied") is True
        and receipt.get("kernel_outside_write_denied") is True
        and receipt.get("internet_client_token_verified") is True
        and receipt.get("internet_client_capability_sid") == "S-1-15-3-1"
        and receipt.get("child_process_policy") == "JOB_KILL_ON_CLOSE"
        and receipt.get("process_started") is True
        and receipt.get("executed") is True
        and receipt.get("output_is_authoritative") is False
        and receipt.get("status") == "EXECUTED_IN_OS_APPCONTAINER"
        and (not until_exit or receipt.get("wait_policy") == "UNTIL_EXIT")
        and str(receipt.get("turbo_root", "")).casefold() ==
            str(activation["turbo_root"]).casefold()
    ):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ATTESTATION_INVALID")
    try:
        stdout = base64.b64decode(receipt["stdout_base64"], validate=True)
        stderr = base64.b64decode(receipt["stderr_base64"], validate=True)
        exit_code = int(receipt["command_exit_code"])
    except (TypeError, ValueError, KeyError) as exc:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_NATIVE_DATA_INVALID") from exc
    if len(stdout) > limit or len(stderr) > limit:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_STREAM_CAP_VIOLATED")
    # Treat capture lengths as evidence-quality metadata, never infer a
    # successful Git/Python launch from the PowerShell parent exit code.
    out_size = receipt.get("stdout_bytes")
    err_size = receipt.get("stderr_bytes")
    out_cut = receipt.get("stdout_truncated")
    err_cut = receipt.get("stderr_truncated")
    if (type(out_size) is not int or type(err_size) is not int
            or out_size < len(stdout) or err_size < len(stderr)
            or type(out_cut) is not bool or type(err_cut) is not bool
            or out_cut != (out_size > len(stdout))
            or err_cut != (err_size > len(stderr))):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_CAPTURE_METADATA_INVALID")
    tools = receipt.get("native_trusted_tools")
    if (
        receipt.get("native_toolchain_status") not in {"PINNED", "NOT_ENROLLED"}
        or type(receipt.get("native_trusted_tool_count")) is not int
        or receipt["native_trusted_tool_count"] not in (0, 1, 2)
        or not isinstance(tools, list)
        or len(tools) != receipt["native_trusted_tool_count"]
        or len(set(tools)) != len(tools)
        or any(name not in ("git", "python") for name in tools)
    ):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_TOOLCHAIN_RECEIPT_INVALID")
    direct_child = _git59_validate_direct_child(
        receipt, native_version_probe, _install_root(),
    )

    # P2: Validate historical RUNNING samples from the protected native
    # Win32 WaitForSingleObject loop, never from a user-supplied stdout line.
    process_pid = receipt.get("native_process_pid")
    native_samples = receipt.get("native_running_sample_count")
    native_last = receipt.get("native_last_running_evidence")
    if (type(process_pid) is not int or process_pid <= 0
            or type(native_samples) is not int
            or not 0 <= native_samples <= (2147483647 if until_exit else 150)
            or not isinstance(native_last, dict)
            or bool(native_last) != (native_samples > 0)):
        raise TurboOSBrokerUnavailable("GIT63_NATIVE_RUNNING_EVIDENCE_INVALID")
    if native_samples:
        now = __import__("time").time()
        observed = native_last.get("observed_at")
        if (native_last.get("schema") != "SMARTAGENT_PROCESS_LIVENESS_V1"
                or native_last.get("execution_id") != request_action
                or native_last.get("process_state") != "RUNNING"
                or native_last.get("liveness_verified") is not True
                or native_last.get("process_identity_verified") is not True
                or native_last.get("evidence_source") !=
                   "NATIVE_WIN32_WAIT_FOR_SINGLE_OBJECT"
                or native_last.get("child_process_policy") != "JOB_KILL_ON_CLOSE"
                or native_last.get("pid") != process_pid
                or native_last.get("running_sample_count") != native_samples
                or type(observed) not in (int, float)
                or not 0 <= now - float(observed)
                or (not until_exit and now - float(observed) > timeout + 75)
                or native_last.get("terminal_eligible") is not False):
            raise TurboOSBrokerUnavailable("GIT63_NATIVE_RUNNING_EVIDENCE_INVALID")

    # P0: diagnostic stage is not a kernel security attestation.
    native_stage = receipt.get("native_failure_stage")
    native_substage = receipt.get("native_failure_substage")
    native_exception_type = receipt.get("native_exception_type")
    native_exception_hresult = receipt.get("native_exception_hresult")
    native_stage_source = receipt.get("native_failure_stage_source")
    native_command_trace = receipt.get("native_command_trace")
    known_steps = {
        "CWD_GET_LOCATION_INITIAL", "CWD_SET_LOCATION",
        "CWD_PSDRIVE_FALLBACK", "CWD_GET_LOCATION_VERIFY", "CWD_GET_NATIVE_CURRENT_DIRECTORY",
        "CWD_COMPARE", "ENV_APPLY", "COMMAND_DISPATCH",
        "NATIVE_CWD_CHECK", "PSDRIVE_CHECK", "PROVIDER_PATH_CHECK",
    }
    if (not isinstance(native_command_trace, list)
            or len(native_command_trace) > 32
            or any(not isinstance(t, str) or len(t) > 80
                   or not re.fullmatch(r"[A-Z_]+\.(START|PASS|SKIP|FINISH|FAIL)", t)
                   or t.split(".", 1)[0] not in known_steps
                   for t in native_command_trace)):
        raise TurboOSBrokerUnavailable("GIT63_NATIVE_COMMAND_TRACE_INVALID")
    # Sandbox-only diagnostics. These values are bounded and are never
    # accepted as OS kernel authorization, root-scoping or success evidence.
    native_cwd_probes = receipt.get("native_cwd_probes")
    allowed_probe_statuses = {
        "native_cwd_status": {"NOT_OBSERVED", "MATCH", "MISMATCH", "ERROR"},
        "psdrive_status": {
            "NOT_OBSERVED", "FILESYSTEM", "OTHER_PROVIDER", "NOT_FOUND", "ERROR"},
        "provider_path_status": {"NOT_OBSERVED", "MATCH", "MISMATCH", "ERROR"},
    }
    if (not isinstance(native_cwd_probes, dict)
            or set(native_cwd_probes) != {
                "native_cwd_status", "native_cwd_hresult",
                "psdrive_status", "psdrive_hresult",
                "provider_path_status", "provider_path_hresult"}):
        raise TurboOSBrokerUnavailable("GIT63_NATIVE_CWD_PROBES_INVALID")
    for name, allowed in allowed_probe_statuses.items():
        status = native_cwd_probes[name]
        hresult = native_cwd_probes[name.replace("_status", "_hresult")]
        if (not isinstance(status, str) or status not in allowed
                or not isinstance(hresult, str)
                or not re.fullmatch(r"(?:[0-9A-F]{8})?", hresult)
                or (status == "NOT_OBSERVED") != (hresult == "")
                or (status in {"MATCH", "MISMATCH", "FILESYSTEM",
                               "OTHER_PROVIDER"} and hresult != "00000000")):
            raise TurboOSBrokerUnavailable("GIT63_NATIVE_CWD_PROBES_INVALID")

    if (not isinstance(native_stage, str)
            or native_stage not in {
                "", "CWD_SETUP", "ENV_SETUP", "COMMAND_EXECUTE", "OUTPUT_CAPTURE"}
            or not isinstance(native_substage, str)
            or native_substage not in (known_steps | {"", "OUTPUT_CAPTURE_WRITE", "WRAPPER_INIT"})
            or not isinstance(native_exception_type, str)
            or not re.fullmatch(r"[A-Za-z0-9.]{0,100}", native_exception_type)
            or not isinstance(native_exception_hresult, str)
            or not re.fullmatch(r"[0-9A-F]{0,8}", native_exception_hresult)
            or native_stage_source not in {
                "NONE", "WRAPPER_RESERVED_EXIT_CODE",
                "SANDBOX_WRAPPER_DIAGNOSTIC"}
            or (native_stage_source == "NONE" and
                (native_stage != "" or native_substage != ""
                 or native_exception_type != "" or native_exception_hresult != ""))
            or (native_stage_source == "WRAPPER_RESERVED_EXIT_CODE" and
                (native_stage != "OUTPUT_CAPTURE"
                 or native_substage != "OUTPUT_CAPTURE_WRITE"
                 or native_exception_type != "" or native_exception_hresult != ""
                 or exit_code != 95))
            or (native_stage_source == "SANDBOX_WRAPPER_DIAGNOSTIC" and
                (native_stage not in {"CWD_SETUP", "ENV_SETUP", "COMMAND_EXECUTE"}
                 or not native_substage or not native_exception_type
                 or len(native_exception_hresult) != 8 or exit_code == 0))
            or (exit_code == 0 and native_stage != "")):
        raise TurboOSBrokerUnavailable("GIT63_NATIVE_FAILURE_STAGE_INVALID")

    # Process exit 1 is distinct from not starting, DNS/TLS failures, and
    # from native command-policy denials. Keep native output and typed hint.
    stderr_text = stderr.decode("utf-8", errors="replace")
    stdout_text = stdout.decode("utf-8", errors="replace")
    error_text = (stderr_text + "\n" + stdout_text)[:32768].casefold()
    if exit_code == 0:
        error_class = "NONE"
    elif "git52_native_executable_not_visible" in error_text:
        error_class = "NATIVE_PINNED_EXECUTABLE_NOT_VISIBLE"
    elif "commandnotfoundexception" in error_text or "is not recognized" in error_text:
        error_class = "NATIVE_EXECUTABLE_NOT_FOUND"
    elif any(word in error_text for word in ("access is denied", "unauthorizedaccessexception", "permissionerror")):
        error_class = "NATIVE_ACCESS_DENIED"
    elif "could not resolve host" in error_text or "nameresolutionfailure" in error_text:
        error_class = "NATIVE_DNS_FAILURE"
    elif "ssl" in error_text or "certificate verify" in error_text or "tls" in error_text:
        error_class = "NATIVE_TLS_FAILURE"
    elif "connect" in error_text or "network" in error_text:
        error_class = "NATIVE_NETWORK_FAILURE"
    else:
        error_class = "NATIVE_EXIT_NONZERO"
    evidence = {
        "schema": "SMARTAGENT_TURBO_OS_ACTION_ATTESTATION_V1",
        "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_read_denied": True,
        "kernel_outside_write_denied": True,
        "internet_client_token_verified": True,
        "internet_client_capability_sid": "S-1-15-3-1",
        "native_toolchain_status": receipt["native_toolchain_status"],
        "native_trusted_tools": tools,
        "native_error_class": error_class,
        "native_failure_stage": native_stage,
        "native_failure_substage": native_substage,
        "native_exception_type": native_exception_type,
        "native_exception_hresult": native_exception_hresult,
        "native_failure_stage_source": native_stage_source,
        "native_command_trace": list(native_command_trace),
        "native_cwd_probes": dict(native_cwd_probes),
        "native_process_pid": process_pid,
        "native_running_sample_count": native_samples,
        "stdout_capture_state": ("TRUNCATED" if out_cut else
                                 "COMPLETE_NONEMPTY" if out_size else "COMPLETE_EMPTY"),
        "stderr_capture_state": ("TRUNCATED" if err_cut else
                                 "COMPLETE_NONEMPTY" if err_size else "COMPLETE_EMPTY"),
        "stream_capture_complete": not (out_cut or err_cut),
        "native_executable_identity_verified": direct_child["verified"],
        "native_executable_identity_reason": direct_child["reason"],
        "native_executable_probe_name": direct_child["name"],
        "native_executable_probe_exit_code": direct_child["exit_code"],
        "native_executable_probe_pid": direct_child["pid"],
        "native_executable_probe_stdout": direct_child["stdout"],
        "native_executable_probe_stderr": direct_child["stderr"],
        "native_executable_probe_capture_complete": direct_child["capture_complete"],
        "native_executable_probe_stdout_bytes": direct_child["stdout_bytes"],
        "native_executable_probe_stderr_bytes": direct_child["stderr_bytes"],
        "native_executable_probe_output_source": (
            "OS_DIRECT_CHILD_PINNED_FILE_HANDLE" if native_version_probe
            else "NONE"),
        "native_executable_direct_launch_count": (
            1 if native_version_probe else 0),
        "native_executable_probe_is_second_process": bool(native_version_probe),
        "child_process_policy": "JOB_KILL_ON_CLOSE",
        "policy_revision": state["policy_revision"],
        "task_id": request_task,
        "action_id": request_action,
        "turbo_root": str(activation["turbo_root"]),
        "os_execution_result": "OS_EXECUTION_SUCCEEDED" if exit_code == 0
                               else "OS_EXECUTION_NONZERO",
        "executed": True,
        "turbo_effective_for_action": "ON",
        "global_production_full_access_attested": False,
    }
    return {
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_bytes": int(receipt["stdout_bytes"]),
        "stderr_bytes": int(receipt["stderr_bytes"]),
        "stdout_truncated": bool(receipt["stdout_truncated"]),
        "stderr_truncated": bool(receipt["stderr_truncated"]),
        "stdout_ref": "", "stderr_ref": "",
        "output_complete": not (out_cut or err_cut),
        "timed_out": False,
        "wait_policy": wait_policy,
        "long_wait_decision": "AUTO_CONTINUE" if until_exit else "",
        "long_wait_state": "COMPLETED" if until_exit else "",
        "long_wait_os_samples": native_samples if until_exit else 0,
        "process_started": True,
        "process_state": "EXITED",
        "execution_id": request_action,
        "pid": process_pid,
        "last_running_evidence": native_last if native_samples else None,
        # The protected Native Broker has already waited for its
        # AppContainer child and attested the final OS exit code.
        # This is an EXIT receipt, never a fabricated live RUNNING sample.
        "liveness_evidence": {
            "schema": "SMARTAGENT_PROCESS_LIVENESS_V1",
            "execution_id": request_action,
            "process_state": "EXITED",
            "liveness_verified": False,
            "process_identity_verified": True,
            "pid": process_pid,
            "evidence_source": "NATIVE_APPCONTAINER_ATTESTED_PROCESS_EXIT",
            "observed_at": __import__("time").time(),
            "exit_code": exit_code,
            "terminal_eligible": False,
        },
        "security_rejected": False,
        "sandbox_evidence": evidence,
    }


__all__ = [
    "TurboOSBrokerUnavailable", "active_broker_available",
    "execute_turbo_command",
]
