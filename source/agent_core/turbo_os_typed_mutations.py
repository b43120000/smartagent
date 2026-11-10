"""Git50: typed file/directory writes executed only in AppContainer.

This module does not grant OS permissions. It consumes the already installed
protected native OS broker, retains SecurityContext authorization, and
requires exact postconditions. No host-side filesystem mutation is allowed.
Recursive deletes, chunked writes and other tools stay fail-closed.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .path_security import PathSecurityError, is_reparse_path
from .turbo_broker_scope import TurboBrokerScopeError, _checked_path
from .turbo_os_broker_client import (
    TurboOSBrokerUnavailable, _load_activation, execute_turbo_command,
)
from .tool_result_contract import encode_result


def _p64(data: str) -> str:
    return base64.b64encode(data.encode("utf-8")).decode("ascii")


def _checked_target(context, raw_path) -> tuple[Path, Path]:
    allowed = context.require_write(raw_path, require_absolute=True,
                                    forbid_glob=True, allow_root=False)
    workspace = context.workspace_root
    _, state, _ = _load_activation(workspace)
    root = Path(state["turbo_root"]).absolute()
    target = _checked_path(allowed, root)
    if target == root or is_reparse_path(target):
        raise TurboBrokerScopeError("TURBO_TYPED_ROOT_OR_REPARSE_TARGET_FORBIDDEN")
    if not target.parent.is_dir():
        raise TurboBrokerScopeError("TURBO_TYPED_PARENT_MISSING")
    return target, root


def create_directory_in_broker(path, *, security_context, action_id="") -> str:
    """Create exactly one new directory under the already-enrolled Turbo root."""
    tool = "create_directory"
    try:
        target, root = _checked_target(security_context, path)
        if target.exists() or target.is_symlink():
            return encode_result(
                "REJECTED", "DIRECTORY_ALREADY_EXISTS_REOBSERVE", tool=tool,
                executed=False, requested_path=path,
                data={"directory_created": False, "terminal_eligible": False},
            )
        literal = str(target).replace("'", "''")
        script = (
            "if ([IO.Directory]::Exists('" + literal + "') -or " +
            "[IO.File]::Exists('" + literal + "')) { exit 73 }; " +
            "[void][IO.Directory]::CreateDirectory('" + literal + "');"
        )
        result = execute_turbo_command(
            script, cwd=root, timeout=30, max_stream_bytes=65536,
            task_id=security_context.task_id, action_id=action_id,
        )
        proof = result["sandbox_evidence"]
        if result["exit_code"] != 0 or not target.is_dir() or is_reparse_path(target):
            return encode_result(
                "UNKNOWN", "TURBO_DIRECTORY_OS_POSTCONDITION_UNVERIFIED",
                tool=tool, executed=result["process_started"],
                requested_path=path, resolved_path=str(target),
                data={"sandbox_evidence": proof,
                      "possible_partial_execution": True,
                      "terminal_eligible": False},
            )
        return encode_result(
            "SUCCEEDED", "DIRECTORY_CREATED", tool=tool, executed=True,
            requested_path=path, resolved_path=str(target),
            data={
                "directory_created": True, "observed_exists": True,
                "observed_is_directory": True,
                "observation_status": "CONFIRMED",
                "no_existing_files_overwritten": True,
                "no_parent_directories_created": True,
                "sandbox_evidence": proof,
                "goal_completion_verified": False,
                "terminal_eligible": False,
            },
        )
    except (PathSecurityError, TurboBrokerScopeError, TurboOSBrokerUnavailable,
            OSError, ValueError, TypeError) as exc:
        # A native broker timeout or lost receipt might have executed: never
        # claim certain "not executed" for that class of failure.
        uncertain = isinstance(exc, TurboOSBrokerUnavailable) and any(
            word in str(exc) for word in
            ("TIMEOUT", "INVALID_NATIVE_RECEIPT", "NATIVE_DATA_INVALID")
        )
        return encode_result(
            "UNKNOWN" if uncertain else "REJECTED",
            "TURBO_TYPED_BROKER_OUTCOME_UNCERTAIN" if uncertain
                else "TURBO_TYPED_CREATE_REJECTED",
            tool=tool, executed=False, requested_path=path,
            data={"error_class": type(exc).__name__,
                  "possible_partial_execution": uncertain,
                  "terminal_eligible": False},
            detail=str(exc)[:250],
        )


def write_file_in_broker(path, content, *, security_context, action_id="") -> str:
    """Bounded UTF-8 write with independent readback, no host controller write."""
    tool = "write_file"
    if not isinstance(content, str) or len(content) > 4096:
        return encode_result(
            "REJECTED", "WRITE_CONTENT_LIMIT", tool=tool,
            executed=False, requested_path=path,
            data={"terminal_eligible": False},
        )
    try:
        target, root = _checked_target(security_context, path)
        if target.exists() and not target.is_file():
            raise TurboBrokerScopeError("TURBO_TYPED_TARGET_NOT_FILE")
        payload = content.encode("utf-8")
        literal = str(target).replace("'", "''")
        value = _p64(content)
        script = (
            "$v=[Text.Encoding]::UTF8.GetString(" +
            "[Convert]::FromBase64String('" + value + "')); " +
            "$e=New-Object Text.UTF8Encoding($false); " +
            "[IO.File]::WriteAllText('" + literal + "',$v,$e);"
        )
        if len(script) > 8192:
            return encode_result(
                "REJECTED", "WRITE_CONTENT_BROKER_COMMAND_LIMIT",
                tool=tool, executed=False, requested_path=path,
                data={"terminal_eligible": False},
            )
        result = execute_turbo_command(
            script, cwd=root, timeout=30, max_stream_bytes=65536,
            task_id=security_context.task_id, action_id=action_id,
        )
        proof = result["sandbox_evidence"]
        try:
            readback = target.read_bytes()
            matched = readback == payload
        except OSError:
            readback = b""
            matched = False
        if result["exit_code"] != 0 or not matched:
            return encode_result(
                "PARTIAL", "TURBO_WRITE_READBACK_MISMATCH",
                tool=tool, executed=result["process_started"],
                requested_path=path, resolved_path=str(target),
                data={"sandbox_evidence": proof,
                      "expected_bytes": len(payload),
                      "readback_bytes": len(readback),
                      "possible_partial_write": True,
                      "verification_status": "FAIL",
                      "terminal_eligible": False},
            )
        return encode_result(
            "SUCCEEDED", "FILE_WRITTEN_VERIFIED", tool=tool, executed=True,
            requested_path=path, resolved_path=str(target),
            data={
                "file_saved": True, "readback_checked": True,
                "bytes_written": len(payload),
                "readback_bytes": len(readback),
                "expected_bytes": len(payload),
                "sha256": hashlib.sha256(readback).hexdigest(),
                "verification_status": "PASS",
                "possible_partial_write": False,
                "terminal_eligible": True,
                "sandbox_evidence": proof,
            },
        )
    except (PathSecurityError, TurboBrokerScopeError, TurboOSBrokerUnavailable,
            OSError, ValueError, TypeError) as exc:
        uncertain = isinstance(exc, TurboOSBrokerUnavailable) and any(
            word in str(exc) for word in
            ("TIMEOUT", "INVALID_NATIVE_RECEIPT", "NATIVE_DATA_INVALID")
        )
        return encode_result(
            "UNKNOWN" if uncertain else "REJECTED",
            "TURBO_TYPED_BROKER_OUTCOME_UNCERTAIN" if uncertain
                else "TURBO_TYPED_WRITE_REJECTED",
            tool=tool, executed=False, requested_path=path,
            data={"error_class": type(exc).__name__,
                  "possible_partial_write": uncertain,
                  "terminal_eligible": False},
            detail=str(exc)[:250],
        )


__all__ = ["create_directory_in_broker", "write_file_in_broker"]
