#!/usr/bin/env python3
"""Regression checks for executable-aware command security classification."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.command_security import inspect_command
from agent_core.smartagent_protocol import validate_tool_envelope


def _run(command: str, *, operation: str = "GIT_MUTATE", verify: list[dict] | None = None) -> tuple[bool, dict | None]:
    call = {
        "tool": "run_command",
        "operation": operation,
        "action_id": "A-COMMAND-SECURITY",
        "command": command,
        "success_criteria": "command exits with zero and its read-only postcondition passes",
        "verify": verify or [{
            "action": "run_command",
            "command": "Write-Output ok",
            "expect_exit_code": 0,
        }],
    }
    return validate_tool_envelope(call)


def validate_format_is_classified_by_executable_position() -> None:
    allowed = (
        "git log -1 --format=%s",
        "git log -1 --pretty=format:%H",
        "Write-Output format",
        "python format_report.py",
        "Get-Date -Format o",
        "Get-ChildItem | Format-Table -AutoSize",
        r".\gradlew.bat clean assembleDebug --no-daemon",
        r"cmake --build C:\SmartAgentWorkspace\build --config Debug",
    )
    for command in allowed:
        result = inspect_command(command)
        assert result.allowed, (command, result)

    forbidden = (
        "format C:",
        "format.com D: /Q",
        "cmd /c format E:",
        r'cmd /c "C:\Windows\System32\format.com H:"',
        'powershell.exe -NoProfile -Command "format I:"',
        "Start-Process format.com -ArgumentList J:",
        'start "" format K:',
        "Write-Output ready; format F:",
        r'"C:\\Windows\\System32\\format.com" G:',
    )
    for command in forbidden:
        result = inspect_command(command)
        assert not result.allowed, command
        assert result.code == "disk_format_forbidden", (command, result)


def validate_git_read_only_verification_is_allowed() -> None:
    valid, diagnostic = _run(
        "git -C 'E:\\card\\release\\SmartAgentv2' commit --allow-empty -m 'checkpoint: before change'",
        verify=[
            {
                "action": "run_command",
                "command": "git -C 'E:\\card\\release\\SmartAgentv2' rev-parse HEAD",
                "expect_exit_code": 0,
                "expect_regex": "(?m)^[0-9a-f]{40}$",
            },
            {
                "action": "run_command",
                "command": "git -C 'E:\\card\\release\\SmartAgentv2' log -1 --format=%s",
                "expect_exit_code": 0,
                "expect_contains": "checkpoint: before change",
            },
        ],
    )
    assert valid, diagnostic


def validate_complex_shell_still_fails_closed() -> None:
    valid, diagnostic = _run(
        "$p='E:\\card\\release\\SmartAgentv2'; git -C $p add -A; "
        "if($LASTEXITCODE -ne 0){exit 1}; "
        "git -C $p commit --allow-empty -m 'checkpoint: before change'; exit $LASTEXITCODE"
    )
    assert not valid
    assert diagnostic is not None
    assert diagnostic["reason"] == "inline_script_too_complex"
    assert "一個 run_command" in diagnostic["suggestion"]
    assert "後續 action" in diagnostic["suggestion"]


def main() -> int:
    validate_format_is_classified_by_executable_position()
    validate_git_read_only_verification_is_allowed()
    validate_complex_shell_still_fails_closed()
    print("COMMAND_SECURITY_CLASSIFICATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
