#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from .payload_budget import RESULT_ATTACHMENT_MAX_BYTES


def _read_capture(path: Path, limit: int) -> tuple[str, int, bool, str]:
    size = path.stat().st_size if path.exists() else 0
    if size <= limit:
        raw = path.read_bytes() if path.exists() else b""
        try:
            path.unlink()
        except OSError:
            pass
        return raw.decode("utf-8", errors="replace").strip(), size, False, ""

    preview_bytes = 16 * 1024
    with path.open("rb") as handle:
        head = handle.read(preview_bytes // 2)
        handle.seek(max(0, size - preview_bytes // 2))
        tail = handle.read(preview_bytes // 2)
    preview = (
        head.decode("utf-8", errors="replace")
        + "\n...[command output retained locally; middle omitted]...\n"
        + tail.decode("utf-8", errors="replace")
    ).strip()
    return preview, size, True, str(path.resolve())


def run_bounded_process(
    command: str | list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 30,
    shell: bool = False,
    capture_root: str | Path | None = None,
    max_stream_bytes: int = RESULT_ATTACHMENT_MAX_BYTES,
    telemetry: dict | None = None,
    approval_manifest: dict | None = None,
    security_command: str | None = None,
    noninteractive: bool = False,
) -> dict:
    """Capture process streams to disk so large output cannot exhaust agent memory."""
    from .command_security import CommandSecurityError, require_command_allowed
    command_text = (
        command if isinstance(command, str)
        else subprocess.list2cmdline([str(value) for value in command])
    )
    security_text = str(security_command if security_command is not None else command_text)
    # Git49 OS Broker admission. This runs before selecting any executor,
    # so neither direct host subprocess nor a stale service can be a fallback.
    from .turbo_broker_admission import assess
    # cwd is an execution location, not a Workspace authorization identity.
    # Obtain the only eligible parent from protected policy, never command text.
    from .workspace_access import resolve_turbo_authorization_workspace
    authorization_workspace = resolve_turbo_authorization_workspace(cwd)
    broker = assess(
        authorization_workspace if authorization_workspace is not None else cwd,
        tool="run_command",
    )
    def reject_broker(reason: str) -> dict:
        return {
            "exit_code": None, "stdout": "", "stderr": "",
            "stdout_bytes": 0, "stderr_bytes": 0,
            "stdout_truncated": False, "stderr_truncated": False,
            "stdout_ref": "", "stderr_ref": "", "timed_out": False,
            "error": "TURBO_OS_BROKER_BLOCKED:" + reason,
            "security_rejected": True, "process_started": False,
            "sandbox_evidence": broker["evidence"],
        }

    if broker["decision"] == "BLOCKED":
        return reject_broker(str(broker["reason_code"]))
    if broker["decision"] != "BROKER_REQUIRED":
        # Preserve the original command blacklist, one-shot authorization,
        # and executable checks in ACL OFF / Turbo OFF modes.
        try:
            if security_text != command_text:
                require_command_allowed(command_text)
            require_command_allowed(
                security_text, workspace=cwd,
                approval_manifest=approval_manifest,
            )
        except CommandSecurityError as exc:
            return {
                "exit_code": None, "stdout": "", "stderr": "",
                "stdout_bytes": 0, "stderr_bytes": 0,
                "stdout_truncated": False, "stderr_truncated": False,
                "stdout_ref": "", "stderr_ref": "", "timed_out": False,
                "error": f"SECURITY_COMMAND_REJECTED:{exc}",
                "security_rejected": True, "process_started": False,
            }
    if broker["decision"] == "BROKER_REQUIRED":
        # Even an enrolled Broker must not execute from an unrelated sibling.
        # The registered parent CWD remains supported and is redirected into
        # Turbo Root by the native client; all other CWDs must be inside it.
        from .path_security import is_within
        state = broker.get("state") or {}
        if state.get("turbo_root") and authorization_workspace is not None:
            try:
                actual_cwd = Path(cwd).expanduser().resolve(strict=True)
                allowed_parent = Path(authorization_workspace).resolve(strict=True)
                actual_root = Path(state["turbo_root"]).resolve(strict=True)
                if (actual_cwd != allowed_parent and
                        not is_within(actual_cwd, actual_root)):
                    return reject_broker("TURBO_OS_BROKER_CWD_OUTSIDE_ROOT")
            except (OSError, RuntimeError, ValueError, TypeError):
                return reject_broker("TURBO_OS_BROKER_CWD_UNVERIFIED")
        # ONLY the already-admitted single PowerShell command adapter can
        # cross the boundary. Reject all other host process launch forms:
        # arbitrary shell=True, direct Python/Git binaries, elevated scripts,
        # and wrapper-free subprocess calls. Within the AppContainer,
        # PowerShell itself can launch tools that the OS token permits.
        is_powershell_adapter = (
            not shell and isinstance(command, (tuple, list))
            and len(command) == 4
            and str(command[0]).casefold() in {"powershell", "powershell.exe"}
            and str(command[1]).casefold() == "-noprofile"
            and str(command[2]).casefold() == "-command"
            and security_command is not None
        )
        if not is_powershell_adapter:
            return reject_broker("TURBO_BROKER_UNSUPPORTED_LAUNCH_FORM")
        # Authorization/approval has already been checked above, but no
        # privileged host process receives the user's script. This helper
        # launches only an admin-protected native broker and independently
        # validates a Windows AppContainer process-token receipt per action.
        from .turbo_os_broker_client import (
            TurboOSBrokerUnavailable, execute_turbo_command,
        )
        metadata = dict(telemetry or {})
        try:
            return execute_turbo_command(
                security_text, cwd=cwd, timeout=int(timeout),
                max_stream_bytes=max_stream_bytes,
                task_id=str(metadata.get("task_id") or ""),
                action_id=str(metadata.get("action_id") or ""),
            )
        except (TurboOSBrokerUnavailable, OSError, ValueError) as exc:
            # No host/restricted-executor fallback on invalid or timed-out
            # broker output. On an uncertain timeout, never claim process
            # execution or a successful result.
            return reject_broker(str(exc))
    from .windows_security import restricted_executor_required
    restricted_required = restricted_executor_required()
    if restricted_required and os.environ.get("SMARTAGENT_RESTRICTED_EXECUTOR_SERVICE") != "1":
        try:
            from .restricted_executor_client import execute_restricted
            telemetry = dict(telemetry or {})
            telemetry_root = telemetry.get("root"); telemetry_task = telemetry.get("task_id")
            if telemetry_root and telemetry_task:
                from .execution_telemetry import write_snapshot
                write_snapshot(telemetry_root, telemetry_task, request_id=telemetry.get("request_id", ""), action_id=telemetry.get("action_id", ""), state="RUNNING", command=command_text[:500], started_at=__import__("time").time(), worker_pid=os.getpid())
            result = execute_restricted(
                command, cwd=cwd, timeout=timeout, shell=shell,
                max_stream_bytes=max_stream_bytes,
                approval_manifest=approval_manifest, security_command=security_text,
                noninteractive=noninteractive,
            )
            result.setdefault('process_started', True if result.get('exit_code') is not None else None)
            if telemetry_root and telemetry_task:
                write_snapshot(telemetry_root, telemetry_task, state="TIMED_OUT" if result.get("timed_out") else "EXITED", ended_at=__import__("time").time(), exit_code=result.get("exit_code"), stdout_tail=str(result.get("stdout", ""))[-8192:], stderr_tail=str(result.get("stderr", ""))[-8192:])
            return result
        except Exception as exc:
            return {
                "exit_code": None, "stdout": "", "stderr": "",
                "stdout_bytes": 0, "stderr_bytes": 0,
                "stdout_truncated": False, "stderr_truncated": False,
                "stdout_ref": "", "stderr_ref": "", "timed_out": False,
                "error": f"RESTRICTED_EXECUTOR_REQUIRED:{type(exc).__name__}: {exc}",
                "security_rejected": True,
                "process_started": None,
            }
    capture_dir = (
        Path(capture_root).expanduser().resolve()
        if capture_root
        else Path(tempfile.gettempdir()) / "smartagent_command_capture"
    )
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture_id = uuid.uuid4().hex
    stdout_path = capture_dir / f"{capture_id}.stdout.txt"
    stderr_path = capture_dir / f"{capture_id}.stderr.txt"
    telemetry = dict(telemetry or {})
    telemetry_root = telemetry.get("root")
    telemetry_task = telemetry.get("task_id")
    if telemetry_root and telemetry_task:
        from .execution_telemetry import write_snapshot
        write_snapshot(telemetry_root, telemetry_task, request_id=telemetry.get("request_id", ""), action_id=telemetry.get("action_id", ""), state="STARTING", command=command_text[:500], started_at=__import__("time").time(), worker_pid=os.getpid(), stdout_path=str(stdout_path), stderr_path=str(stderr_path))
    process_started = False
    try:
        from .runtime_environment import build_runtime_environment
        child_environment = build_runtime_environment()
        if noninteractive:
            # No synthetic confirmation keystrokes. Git must fail explicitly
            # if credentials require interactive input; cooperative BAT scripts
            # can skip cosmetic PAUSE via SMARTAGENT_NONINTERACTIVE.
            child_environment.update({
                "SMARTAGENT_NONINTERACTIVE": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
            })
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
                shell=shell,
                stdout=stdout_handle,
                stderr=stderr_handle,
                stdin=subprocess.DEVNULL if noninteractive else None,
                env=child_environment,
            )
            process_started = True
            if telemetry_root and telemetry_task:
                write_snapshot(telemetry_root, telemetry_task, state="RUNNING", pid=process.pid)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # On Windows shell=True owns cmd.exe, not just the actual child.
                # Kill only this still-live Popen tree; otherwise inherited capture
                # handles (and the command itself) can survive the timeout.
                if os.name == 'nt' and process.poll() is None:
                    try:
                        subprocess.run(
                            [str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'),
                             '/PID', str(process.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

        stdout, stdout_bytes, stdout_truncated, stdout_ref = _read_capture(stdout_path, max_stream_bytes)
        stderr, stderr_bytes, stderr_truncated, stderr_ref = _read_capture(stderr_path, max_stream_bytes)
        result = {
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_ref": stdout_ref,
            "stderr_ref": stderr_ref,
            "timed_out": timed_out,
            "process_started": process_started,
        }
        if timed_out:
            result["error"] = f"指令超過 {timeout} 秒"
        if telemetry_root and telemetry_task:
            write_snapshot(telemetry_root, telemetry_task, state="TIMED_OUT" if timed_out else "EXITED", ended_at=__import__("time").time(), exit_code=process.returncode, stdout_tail=stdout[-8192:], stderr_tail=stderr[-8192:])
        return result
    except Exception as exc:
        for path in (stdout_path, stderr_path):
            try:
                path.unlink()
            except OSError:
                pass
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_ref": "",
            "stderr_ref": "",
            "timed_out": False,
            "error": str(exc),
            "process_started": process_started,
        }
