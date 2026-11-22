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


def assess_native_child_live(task, loop, *, source_transport: str) -> dict:
    """Separate successful diagnostic from positive native executable proof.

    Reads only Runtime-owned terminal Goal State evidence, never model prose.
    An accurate UNVERIFIED report can pass its diagnostic Goal; that is NOT
    native executable execution success.
    """
    metadata = dict((getattr(task, "metadata", None) or {}).get(
        "ingress_metadata") or {})
    marker = metadata.get(MARKER)
    if marker is None:
        raise ValueError("GIT59_DIAGNOSTIC_MARKER_MISSING")
    request = str(getattr(task, "request", "") or "")
    if (
        source_transport != "TELEGRAM"
        or not isinstance(marker, dict)
        or marker.get("schema") != "SMARTAGENT_GIT59_NATIVE_EXEC_LIVE_SELECTOR_V1"
        or marker.get("original_command") != COMMAND
        or marker.get("action_authorization_granted") is not False
        or marker.get("request_sha256") != hashlib.sha256(
            request.encode("utf-8")).hexdigest()
        or str(marker.get("source_message_id") or "") != str(
            (task.metadata or {}).get("source_message_id") or "")
    ):
        raise ValueError("GIT59_DIAGNOSTIC_TASK_BOUNDARY_INVALID")
    from agent_core.requirement_goal_state import compile_requirement_goal_state
    contract = compile_requirement_goal_state(request)
    terminal = dict(getattr(loop, "terminal_acceptance", {}) or {})
    rows = {
        str(row.get("condition_id") or ""): row
        for row in terminal.get("goal_state_table", [])
        if isinstance(row, dict)
    }
    kinds = {"D07": "git", "D08": "python"}
    positive = {}
    for condition_id, name in kinds.items():
        row = rows.get(condition_id, {})
        actual = row.get("actual") or {}
        evidence_refs = row.get("evidence_refs") or []
        positive[name] = bool(
            row.get("verdict") == "PASS"
            and row.get("reason_code") == "NATIVE_CHILD_EXECUTION_VERIFIED"
            and isinstance(actual, dict)
            and actual.get("native_executable_identity_verified") is True
            and actual.get("native_executable_probe_name") == name
            and type(actual.get("native_executable_probe_pid")) is int
            and actual["native_executable_probe_pid"] > 0
            and actual.get("native_executable_probe_exit_code") == 0
            and actual.get("native_executable_direct_launch_count") == 1
            and actual.get("native_executable_probe_is_second_process") is True
            and actual.get("exit_code") == 0
            and actual.get("native_executable_probe_capture_complete") is True
            and actual.get("native_executable_probe_output_source")
                == "OS_DIRECT_CHILD_PINNED_FILE_HANDLE"
            and isinstance(actual.get("native_executable_probe_stdout"), str)
            and isinstance(actual.get("native_executable_probe_stderr"), str)
            and bool(
                actual["native_executable_probe_stdout"]
                or actual["native_executable_probe_stderr"])
            and len(evidence_refs) == 1
            and str(evidence_refs[0]).startswith("MIN-EV-")
        )
    required = ("D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09")
    complete = bool(
        contract.get("status") == "COMPILED"
        and contract.get("goal_kind") == "DIAGNOSTIC_EXECUTABLE_RESOLUTION"
        and terminal.get("final_verdict") == "PASS"
        and terminal.get("goal_contract_sha256") ==
            contract.get("contract_sha256")
        and terminal.get("comparison", {}).get("reason_code") ==
            "FROZEN_GOAL_STATE_SATISFIED"
        and len(rows) == len(required)
        and all(rows.get(step, {}).get("verdict") == "PASS" for step in required)
    )
    verdict = (
        "GIT59_NATIVE_CHILD_EXECUTION_PASS" if (
            complete and all(positive.values()))
        else "GIT59_NATIVE_CHILD_EXECUTION_UNVERIFIED"
    )
    return {
        "schema": "SMARTAGENT_GIT59_NATIVE_CHILD_LIVE_REPORT_V1",
        "task_id": str(getattr(task, "task_id", "")),
        "request_id": str(getattr(task, "request_id", "")),
        "diagnostic_goal_verdict": terminal.get("final_verdict", "NOT_ADMITTED"),
        "goal_coverage_complete": complete,
        "sandbox_probe": rows.get("D01", {}).get("reason_code", "MISSING"),
        "tool_coverage": rows.get("D02", {}).get("reason_code", "MISSING"),
        "git_direct_child_verified": positive["git"],
        "python_direct_child_verified": positive["python"],
        "git_reason": rows.get("D07", {}).get("reason_code", "MISSING"),
        "python_reason": rows.get("D08", {}).get("reason_code", "MISSING"),
        "git_evidence_refs": rows.get("D07", {}).get("evidence_refs", []),
        "python_evidence_refs": rows.get("D08", {}).get("evidence_refs", []),
        "verdict": verdict,
    }
