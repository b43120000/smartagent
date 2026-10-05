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
) -> dict:
    """Capture process streams to disk so large output cannot exhaust agent memory."""
    from .command_security import CommandSecurityError, require_command_allowed
    command_text = (
        command if isinstance(command, str)
        else subprocess.list2cmdline([str(value) for value in command])
    )
    security_text = str(security_command if security_command is not None else command_text)
    try:
        if security_text != command_text:
            require_command_allowed(command_text)
        require_command_allowed(security_text, workspace=cwd, approval_manifest=approval_manifest)
    except CommandSecurityError as exc:
        return {
            "exit_code": None, "stdout": "", "stderr": "",
            "stdout_bytes": 0, "stderr_bytes": 0,
            "stdout_truncated": False, "stderr_truncated": False,
            "stdout_ref": "", "stderr_ref": "", "timed_out": False,
            "error": f"SECURITY_COMMAND_REJECTED:{exc}",
            "security_rejected": True,
        }
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
            result = execute_restricted(command, cwd=cwd, timeout=timeout, shell=shell, max_stream_bytes=max_stream_bytes, approval_manifest=approval_manifest, security_command=security_text)
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
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
                shell=shell,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            if telemetry_root and telemetry_task:
                write_snapshot(telemetry_root, telemetry_task, state="RUNNING", pid=process.pid)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
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
        }
