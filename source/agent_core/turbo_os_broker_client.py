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


# Classify *known* native broker stderr without exposing a full PowerShell
# exception, command text, paths or potentially sensitive process output.
_NATIVE_BROKER_REASON = re.compile(r"\bGIT50_[A-Z0-9_]{3,120}\b")
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

    # No implicit "first of many" root authority: a native activation binds
    # exactly one workspace and one project root.
    access = access_snapshot()
    policy = access["policy"]
    roots = tuple(access.get("turbo_roots") or ())
    if len(roots) != 1:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_AMBIGUOUS_ROOT_SET")
    root = Path(state["turbo_root"]).absolute()
    if (root != Path(roots[0]).absolute() or not root.is_dir() or
            is_reparse_path(root)):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ROOT_NOT_VERIFIED")

    journal_path = _activation_path()
    # A protected old-to-new switch remains non-authoritative until its
    # final durable commit. No Planner / Runtime action may enter the new
    # Broker while the switch journal is present, including after a crash.
    if journal_path.with_name("turbo_existing_root_switch.json").exists():
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
    project = _install_root()
    workspace_path = Path(workspace).absolute()
    if (
        activation.get("schema") != ACTIVATION_SCHEMA
        or activation.get("status") != "ACTIVE_PROVISIONED"
        or type(activation.get("policy_revision")) is not int
        or activation["policy_revision"] != int(state["policy_revision"])
        or str(project).casefold() != str(activation.get("install_root", "")).casefold()
        or str(workspace_path).casefold() != str(activation.get("workspace_root", "")).casefold()
        or str(root).casefold() != str(activation.get("turbo_root", "")).casefold()
        or not str(activation.get("package_sid", "")).startswith("S-1-15-2-")
    ):
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ACTIVATION_STALE")

    code_dir = journal_path.parent / "turbo_broker_code"
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
    if type(policy.get("revision")) is not int or policy["revision"] != activation["policy_revision"]:
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
    access = access_snapshot()
    roots = tuple(access.get("turbo_roots") or ())
    if len(roots) != 1:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_ROOT_SET_NOT_UNIQUE")
    root = Path(roots[0]).absolute()
    candidates = [
        Path(candidate).absolute()
        for candidate in access["writable_workspaces"]
        if is_within(root, Path(candidate).absolute(), allow_root=False)
    ]
    if len(candidates) != 1:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_WORKSPACE_AMBIGUOUS")
    workspace = candidates[0]
    requested_cwd = Path(cwd).absolute() if cwd else workspace
    if requested_cwd == workspace:
        actual_cwd = root
    elif is_within(requested_cwd, root):
        actual_cwd = requested_cwd
    else:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_CWD_OUTSIDE_ROOT")
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
) -> dict[str, Any]:
    """Run a PowerShell command only through the OS-attested native broker.

    Failure never falls back to an ordinary subprocess or restricted
    executor. Broker stdout is treated as untrusted *data*, not authority.
    """
    if not isinstance(command, str) or not command.strip() or len(command) > 8192:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_COMMAND_INVALID")
    if type(timeout) is not int or timeout < 1 or timeout > 300:
        raise TurboOSBrokerUnavailable("TURBO_OS_BROKER_TIMEOUT_INVALID")
    limit = min(max(int(max_stream_bytes), 1), 2 * 1024 * 1024)
    workspace, actual_cwd = _resolve_workspace_and_cwd(cwd)
    activation, state, broker = _load_activation(workspace)
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
        "-ExpectedRevision", str(state["policy_revision"]),
        "-TaskId", request_task,
        "-ActionId", request_action,
        "-CommandBase64", base64.b64encode(command.encode("utf-8")).decode("ascii"),
        "-TimeoutSeconds", str(timeout),
        "-MaxOutputBytes", str(limit),
    ]
    try:
        completed = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout + 75,
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
    # Process exit 1 is distinct from not starting, DNS/TLS failures, and
    # from native command-policy denials. Keep native output and typed hint.
    stderr_text = stderr.decode("utf-8", errors="replace")
    stdout_text = stdout.decode("utf-8", errors="replace")
    error_text = (stderr_text + "\n" + stdout_text)[:32768].casefold()
    if "commandnotfoundexception" in error_text or "is not recognized" in error_text:
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
        error_class = "NATIVE_EXIT_NONZERO" if exit_code else "NONE"
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
        "timed_out": False,
        "process_started": True,
        "security_rejected": False,
        "sandbox_evidence": evidence,
    }


__all__ = [
    "TurboOSBrokerUnavailable", "active_broker_available",
    "execute_turbo_command",
]
