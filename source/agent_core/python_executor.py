#!/usr/bin/env python3
"""Controlled Python script execution primitive for future SmartAgent skills."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bounded_process import run_bounded_process
from .security_context import SecurityContext


_ALLOWED_INTERPRETERS = {"python", "py"}


@dataclass(frozen=True)
class PythonExecutionRequest:
    script: str | Path
    args: tuple[str, ...] = ()
    timeout: int = 30
    capture_root: str | Path | None = None
    interpreter: str = "python"


class PythonExecutor:
    """Execute an existing authorized .py file without shell or inline code."""

    def execute(
        self,
        request: PythonExecutionRequest,
        *,
        security_context: SecurityContext,
        telemetry: dict | None = None,
    ) -> dict:
        if not isinstance(request, PythonExecutionRequest):
            raise TypeError("request must be PythonExecutionRequest")
        interpreter = str(request.interpreter or "").strip().lower()
        if interpreter not in _ALLOWED_INTERPRETERS:
            raise ValueError("python_interpreter_not_allowed")
        if isinstance(request.args, (str, bytes)):
            raise TypeError("args must be a sequence of strings")
        args = tuple(request.args)
        if not all(isinstance(value, str) for value in args):
            raise TypeError("args must contain only strings")
        timeout = int(request.timeout)
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        script = security_context.require_read(request.script, forbid_glob=True)
        if script.suffix.casefold() != ".py":
            raise ValueError("python_script_extension_required")
        if not script.is_file():
            raise FileNotFoundError(str(script))

        command = [interpreter, "-B", str(script), *args]
        return run_bounded_process(
            command,
            cwd=security_context.workspace_root,
            timeout=timeout,
            shell=False,
            capture_root=request.capture_root,
            telemetry=telemetry,
        )


__all__ = ["PythonExecutionRequest", "PythonExecutor"]
