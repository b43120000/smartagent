"""Git59: exact authenticated Telegram one-shot native executable diagnostic.

Only an already-authorized Telegram private chat can request this control.
The Receiver expands it into a normal, source-grounded read-only diagnostic
requirement. The Agent and frozen Goal compiler never see a command to
reconfigure ACL, Turbo or Broker; every run_command uses standard admission.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import os

COMMAND = "/git59_native_exec_live"
MARKER = "git59_native_exec_live"


def build_requirement(project_root: str | Path) -> str:
    project = Path(project_root).resolve()
    if os.name != "nt" or not project.is_absolute():
        raise ValueError("GIT59_WINDOWS_INSTALL_ROOT_REQUIRED")
    git = project / ".venv" / "git" / "cmd" / "git.exe"
    python = project / ".venv" / "Scripts" / "python.exe"
    for executable in (git, python):
        if "'" in str(executable) or "\n" in str(executable) or "\r" in str(executable):
            raise ValueError("GIT59_EXECUTABLE_PATH_UNSAFE")
    return (
        "請使用 ACL ON、Turbo ON 的正常 SmartAgent Runtime，透過 "
        "WIN32_APPCONTAINER Broker，執行 Git／Python Executable Resolution "
        "與 Native Child OS Token 身分診斷。這是唯讀測試；不可修改既有檔案、"
        "ACL、PATH、Security Profile、Turbo 設定，也不得使用 Telegram Host CMD fallback。"
        "所有工具呼叫均須走正常 Runtime run_command，operation=INSPECT，"
        "且六項必須各為獨立 ToolCall，不要合併、不要重跑；"
        "不論命令成功或失敗，都回報實際 Evidence。"
        "\n\n請依序執行這六條 PowerShell 命令，每條只執行一次："
        "\n1. $env:PATH"
        "\n2. Test-Path -LiteralPath '" + str(git) + "'"
        "\n3. Test-Path -LiteralPath '" + str(python) + "'"
        "\n4. Get-Command git, python -ErrorAction SilentlyContinue"
        "\n5. & '" + str(git) + "' --version"
        "\n6. & '" + str(python) + "' --version"
        "\n\n最後彙整 PATH、Test-Path、Get-Command、Git、Python、Exit Code、"
        "stdout、stderr、Native Error Class、Runtime Action Evidence、"
        "Sandbox AppContainer OS Attestation 與每筆證據引用。"
        "對第 5、6 項另外列出 native_executable_identity_verified、"
        "native_executable_identity_reason、native_executable_probe_exit_code、"
        "native_executable_probe_pid 及是否為第二次直接啟動驗證。"
        "無法證實的項目一律明確標示 UNVERIFIED，不得以 PowerShell 父程序 Token "
        "或版本輸出推定 Git／Python 直接子程序已獨立通過驗證。"
        "這是診斷工作，不要求執行檔都必須成功啟動。"
    )


def prepare_message(message, *, update_id: int, project_root: str | Path):
    if str(message.text or "").strip() != COMMAND:
        return None
    if message.transport != "TELEGRAM" or not str(message.sender_id).isdigit():
        raise ValueError("GIT59_AUTHENTICATED_TELEGRAM_REQUIRED")
    if message.attachments:
        raise ValueError("GIT59_ATTACHMENTS_FORBIDDEN")
    if not isinstance(update_id, int) or update_id < 0:
        raise ValueError("GIT59_UPDATE_ID_INVALID")
    requirement = build_requirement(project_root)
    from agent_core.requirement_goal_state import compile_requirement_goal_state
    contract = compile_requirement_goal_state(requirement)
    if contract.get("status") != "COMPILED" or contract.get(
            "goal_kind") != "DIAGNOSTIC_EXECUTABLE_RESOLUTION":
        raise ValueError("GIT59_DIAGNOSTIC_GOAL_UNCOMPILED")
    marker = {
        "schema": "SMARTAGENT_GIT59_NATIVE_EXEC_LIVE_SELECTOR_V1",
        "source_update_id": update_id,
        "source_message_id": str(message.source_message_id),
        "original_command": COMMAND,
        "request_sha256": hashlib.sha256(requirement.encode("utf-8")).hexdigest(),
        "action_authorization_granted": False,
    }
    return replace(
        message, text=requirement,
        metadata={**dict(message.metadata or {}), MARKER: marker},
    )
