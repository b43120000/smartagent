"""Git55: source-grounded, frozen Goal State Contract acceptance regression.

Synthetic receipts verify the comparator, never actual Win32 AppContainer
security or external Git/Python executable identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core.executor_run_blackboard import begin_executor_run, read_executor_run
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement
from agent_core.requirement_goal_state import (
    compile_requirement_goal_state, evaluate_frozen_goal_contract,
    validate_frozen_goal_contract, _step,
)
from agent_core.runtime_finish_comparator import compare_runtime_finish
from agent_core.tool_result_contract import decode_result, encode_result


GIT = r"C:\smartagent\.venv\git\cmd\git.exe"
PYTHON = r"C:\smartagent\.venv\Scripts\python.exe"
REQUEST = (
    "請使用 ACL ON、Turbo ON 的正常 SmartAgent Runtime，透過 "
    "WIN32_APPCONTAINER Broker，診斷 Git／Python 的 Executable Resolution。\n"
    "不要修改 ACL、PATH 設定、Security Profile 或任何既有檔案。\n"
    "分別執行獨立 run_command：\n"
    "1. 讀取 $env:PATH，確認包含 C:\\smartagent\\.venv\\git\\cmd "
    "和 C:\\smartagent\\.venv\\Scripts。\n"
    "2. 使用 Test-Path 分別確認 C:\\smartagent\\.venv\\git\\cmd\\git.exe "
    "與 C:\\smartagent\\.venv\\Scripts\\python.exe。\n"
    "3. 使用 Get-Command git, python -ErrorAction SilentlyContinue。\n"
    "4. 如果允許使用完整絕對路徑執行 Git 和 Python 的 --version。\n"
    "5. 回報 Exit Code、stdout、stderr、Native Error Class、"
    "Action Evidence、Sandbox OS Attestation。不得 Host CMD fallback。\n"
    "缺乏證據時標示 UNVERIFIED。"
)
COMMANDS = [
    "$env:PATH",
    "Test-Path -LiteralPath '" + GIT + "'",
    "Test-Path -LiteralPath '" + PYTHON + "'",
    "Get-Command git, python -ErrorAction SilentlyContinue",
    "& '" + GIT + "' --version",
    "& '" + PYTHON + "' --version",
]
OUTPUTS = [
    r"C:\Windows\System32;C:\smartagent\.venv\Scripts;C:\smartagent\.venv\git\cmd",
    "True", "True", "", "", "",
]
SUMMARY = (
    "Git / Python Executable Resolution 診斷完成；PATH 包含兩個 .venv 目錄；"
    "Test-Path 均 True；Get-Command、Git --version、Python --version stdout 輸出為空，"
    "PowerShell Exit Code 0，stderr 空，Native Error Class NONE；"
    "Action Evidence 與 Sandbox AppContainer OS Attestation 已回報。"
    "Executable Identity 仍是 UNVERIFIED，不能推定可執行成功。"
)


def _receipt(command: str, output: str, *, sandbox=True, complete=True):
    evidence = {
        "sandbox_backend": ("WIN32_APPCONTAINER" if sandbox else "LEGACY"),
        "sandbox_identity_verified": bool(sandbox),
        "turbo_effective_for_action": "ON" if sandbox else "OFF",
        "native_error_class": "NONE",
        "kernel_outside_read_denied": bool(sandbox),
        "kernel_outside_write_denied": bool(sandbox),
        "native_executable_identity_verified": False,
        "native_executable_identity_reason": "POWERSHELL_PARENT_ONLY_ATTESTED",
    }
    return decode_result(encode_result(
        "SUCCEEDED", "COMMAND_EXITED", executed=True, tool="run_command",
        data={
            "exit_code": 0, "stdout": output, "stderr": "",
            "stdout_bytes": len(output.encode("utf-8")), "stderr_bytes": 0,
            "output_complete": complete, "timed_out": False,
            "process_state": "EXITED", "sandbox_evidence": evidence,
            "verification_status": "UNVERIFIED",
        },
    ))


def _history(*, drop=None, sandbox=True, complete=True, boolean="True"):
    result = []
    for ix, (command, output) in enumerate(zip(COMMANDS, OUTPUTS)):
        if ix == drop:
            continue
        if ix in (1, 2):
            output = boolean
        result.append({
            "action": {"tool": "run_command", "command": command, "operation": "INSPECT"},
            "receipt": _receipt(command, output, sandbox=sandbox, complete=complete),
            "evidence_ref": "MIN-EV-" + format(ix + 1, "024x"),
        })
    return result


def test_diagnostic_compiler_is_source_grounded_immutable_and_not_permission():
    contract = compile_requirement_goal_state(REQUEST)
    assert contract["status"] == "COMPILED"
    assert contract["goal_kind"] == "DIAGNOSTIC_EXECUTABLE_RESOLUTION"
    assert contract["authorizes_operation"] is False
    assert len(contract["conditions"]) == 9
    assert contract == compile_requirement_goal_state(REQUEST)
    assert validate_frozen_goal_contract(contract, REQUEST)
    tampered = copy.deepcopy(contract)
    tampered["conditions"][-1]["expected"] = False
    assert not validate_frozen_goal_contract(tampered, REQUEST)
    assert not validate_frozen_goal_contract(contract, REQUEST + " changed")


def test_unknown_or_more_demanding_goal_cannot_gain_easy_unverified_contract():
    assert compile_requirement_goal_state("請幫我建置一個部署系統")["status"] == "UNCOMPILED"
    assert compile_requirement_goal_state(
        REQUEST.replace("缺乏證據時標示 UNVERIFIED", "Git 與 Python 必須成功執行")
    )["status"] == "UNCOMPILED"


def test_directory_compiler_does_not_ignore_extra_natural_language_clauses():
    mixed = (
        "請確認資料夾存在 E:\\demo，然後傳送電子郵件通知主管"
    )
    contract = compile_requirement_goal_state(mixed)
    assert contract["status"] == "UNCOMPILED"
    assert contract["uncompiled_reason"] == "MULTI_CLAUSE_OR_UNSUPPORTED_DIRECTORY_GOAL"


def test_six_native_steps_are_strict_independent_commands():
    targets = compile_requirement_goal_state(REQUEST)["executable_targets"]
    assert [_step(cmd, targets) for cmd in COMMANDS] == [
        "path", "git_exists", "python_exists",
        "resolve", "git_version", "python_version",
    ]
    for unsafe in (
        "Get-Command git, python -ErrorAction SilentlyContinue; Remove-Item C:\\a",
        "& '" + GIT + "' --version; Set-Content C:\\a x",
        "Test-Path -LiteralPath '" + GIT + "'; echo True",
    ):
        assert _step(unsafe, targets) == ""


def test_empty_version_output_is_unverified_diagnosis_not_failed_task():
    contract = compile_requirement_goal_state(REQUEST)
    state = evaluate_frozen_goal_contract(contract, _history(), SUMMARY)
    assert state["verdict"] == "PASS", state
    assert len(state["state_table"]) == 9
    assert [row["condition_id"] for row in state["state_table"]] == [
        x["condition_id"] for x in contract["conditions"]
    ]
    assert state["state_table"][6]["actual"]["classification"] == "UNVERIFIED"
    assert state["state_table"][7]["actual"]["classification"] == "UNVERIFIED"
    assert state["state_table"][7]["verdict"] == "PASS"
    assert state["state_table"][7]["evidence_refs"]


def test_optional_version_policy_denial_is_truthful_diagnostic_outcome():
    contract = compile_requirement_goal_state(REQUEST)
    history = [row for index, row in enumerate(_history()) if index < 4]
    denied = [{
        "kind": "TURBO_NATIVE_CONFIRMATION_BLOCKED",
        "toolName": "run_command",
        "goal_diagnostic_step": target + "_version",
        "event_id": "ER-EV-" + target,
        "command_sha256": str(index) * 64,
        "executed": False, "authorization_granted": False,
        "reason_code": "TRUSTED_EXECUTABLE_POLICY_DENIED",
    } for index, target in ((4, "git"), (5, "python"))]
    report = SUMMARY + " Git/Python 絕對路徑 --version 已遭 Native 政策拒絕執行。"
    state = evaluate_frozen_goal_contract(
        contract, history, report, admission_denials=denied)
    assert state["verdict"] == "PASS", state
    assert state["state_table"][6]["actual"]["classification"] == "POLICY_REJECTED"
    assert state["state_table"][7]["actual"]["executed"] is False
    assert state["state_table"][6]["evidence_refs"] == ["ER-EV-git"]
    assert state["state_table"][-1]["actual"]["policy_rejection_reported"] is True
    unreported = evaluate_frozen_goal_contract(
        contract, history, SUMMARY, admission_denials=denied)
    assert unreported["verdict"] == "FAIL"
    assert "D09" in unreported["unsatisfied_condition_ids"]
    forged = [dict(denied[0], executed=True), denied[1]]
    rejected = evaluate_frozen_goal_contract(
        contract, history, report, admission_denials=forged)
    assert rejected["verdict"] == "FAIL"


def test_missing_step_wrong_backend_or_incomplete_capture_fails():
    contract = compile_requirement_goal_state(REQUEST)
    cases = [
        (_history(drop=5), "D02"),
        (_history(sandbox=False), "D01"),
        (_history(complete=False), "D03"),
    ]
    for history, missing in cases:
        state = evaluate_frozen_goal_contract(contract, history, SUMMARY)
        assert state["verdict"] == "FAIL", state
        assert missing in state["unsatisfied_condition_ids"]


def test_report_must_disclose_uncertainty_and_can_report_negative_boolean():
    contract = compile_requirement_goal_state(REQUEST)
    assert evaluate_frozen_goal_contract(
        contract, _history(boolean="False"), SUMMARY
    )["verdict"] == "PASS"
    state = evaluate_frozen_goal_contract(
        contract, _history(),
        "Git/Python 版本都已驗證正常執行。",
    )
    assert state["verdict"] == "FAIL"
    assert state["state_table"][-1]["condition_id"] == "D09"


def test_nested_verify_or_mutating_operation_never_passes_read_only_goal():
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    history[0]["action"]["verify"] = [
        {"command": "Remove-Item -Recurse -Force C:\\tmp"}
    ]
    state = evaluate_frozen_goal_contract(contract, history, SUMMARY)
    assert state["verdict"] == "FAIL"
    assert "D02" in state["unsatisfied_condition_ids"]


def test_missing_os_denial_attestation_is_not_a_completed_diagnostic():
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    history[0]["receipt"]["data"]["sandbox_evidence"][
        "kernel_outside_write_denied"] = False
    state = evaluate_frozen_goal_contract(contract, history, SUMMARY)
    assert state["verdict"] == "FAIL"
    assert "D01" in state["unsatisfied_condition_ids"]
    history = _history()
    history[1]["receipt"]["data"]["sandbox_evidence"].pop("native_error_class")
    state = evaluate_frozen_goal_contract(contract, history, SUMMARY)
    assert state["verdict"] == "FAIL"
    assert "D04" in state["unsatisfied_condition_ids"]


def test_extra_command_or_mutation_not_a_read_only_diagnostic():
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    history.append({
        "action": {"tool": "delete_path", "path": r"C:\extra"},
        "receipt": decode_result(encode_result(
            "SUCCEEDED", "DELETED", tool="delete_path", executed=True)),
        "evidence_ref": "MIN-EV-" + "f" * 24,
    })
    state = evaluate_frozen_goal_contract(contract, history, SUMMARY)
    assert state["verdict"] == "FAIL"
    assert "D02" in state["unsatisfied_condition_ids"]


@pytest.mark.parametrize("expected,exists,verdict", [
    ("請確認資料夾不存在 " + r"E:\demo", False, "PASS"),
    ("請確認資料夾不存在 " + r"E:\demo", True, "FAIL"),
    ("請確保資料夾存在 " + r"E:\demo", True, "PASS"),
    ("請確保資料夾存在 " + r"E:\demo", False, "FAIL"),
    ("這個資料夾是否存在？ " + r"E:\demo", False, "PASS"),
    ("這個資料夾是否存在？ " + r"E:\demo", True, "PASS"),
])
def test_directory_goal_exact_state_and_observation_are_distinct(
    expected, exists, verdict,
):
    contract = compile_requirement_goal_state(expected)
    assert contract["status"] == "COMPILED", contract
    receipt = decode_result(encode_result(
        "SUCCEEDED", "DIRECTORY_INSPECTED", executed=True,
        tool="inspect_directory", requested_path=r"E:\demo",
        data={"observed_exists": exists, "observed_is_directory": exists,
              "observation_status": "CONFIRMED"},
    ))
    history = [{
        "action": {"tool": "inspect_directory", "paths": [r"E:\demo"]},
        "receipt": receipt, "evidence_ref": "MIN-EV-" + "a" * 24,
    }]
    result = evaluate_frozen_goal_contract(contract, history, "已確認")
    assert result["verdict"] == verdict, result


def test_blackboard_frozen_before_first_action_and_immutable(tmp_path):
    loop = SimpleNamespace(
        task_id="TASK-GIT55", run_id="RR-GIT55", task_epoch="epoch")
    board = begin_executor_run(tmp_path, loop, REQUEST,
        evidence_requirement=assess_finish_evidence_requirement(REQUEST))
    assert not board["events"]
    assert board["goal_contract_frozen_before_execution"] is True
    assert board["goal_state_contract"]["status"] == "COMPILED"
    reloaded = read_executor_run(tmp_path, task_id="TASK-GIT55")
    assert reloaded["goal_state_contract"] == board["goal_state_contract"]
    assert begin_executor_run(
        tmp_path, loop, REQUEST,
        evidence_requirement=assess_finish_evidence_requirement(REQUEST)
    )["goal_state_contract"] == board["goal_state_contract"]
    with pytest.raises(ValueError, match="goal_contract|identity"):
        begin_executor_run(
            tmp_path, loop, REQUEST + " mutation",
            evidence_requirement=assess_finish_evidence_requirement(REQUEST + " mutation"),
        )


def _compare_with_receipts(monkeypatch, request, history, *, summary,
                           goal_checks=None, mutate_contract=False):
    import agent_core.runtime_finish_comparator as comparator
    from agent_core.requirement_goal_state import compile_requirement_goal_state

    contract = compile_requirement_goal_state(request)
    if mutate_contract:
        contract["conditions"][0]["expected"] = "forged"
    refs = [row["evidence_ref"] for row in history]
    board = {
        "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "evidence_requirement": assess_finish_evidence_requirement(request),
        "goal_state_contract": contract,
    }
    fake = SimpleNamespace()
    monkeypatch.setattr(comparator, "_readback", lambda *a, **kw: (
        history, refs, {"board": board, "results": [{
            "evidence_ref": row["evidence_ref"],
            "execution_status": row["receipt"]["status"],
        } for row in history]}, "",
    ))
    claims = []
    for row in history:
        data = row["receipt"]["data"]
        claims.append({
            "evidence_ref": row["evidence_ref"],
            "fields": {
                "status": row["receipt"]["status"],
                "data.exit_code": data.get("exit_code"),
                "data.sandbox_evidence.native_executable_identity_verified":
                    data.get("sandbox_evidence", {}).get(
                        "native_executable_identity_verified"),
            },
        })
    return compare_runtime_finish(
        fake, ".", request, claims, goal_checks=goal_checks,
        allow_open=True, answer=summary,
    )


def test_finish_comparator_uses_archived_blackboard_evidence(tmp_path):
    from agent_core.executor_run_blackboard import (
        append_executor_event, finish_executor_run,
    )
    request = "這個資料夾是否存在？ " + r"E:\demo"
    ref = "MIN-EV-" + "b" * 24
    action_id = "MIN-GIT55-ARCHIVED"
    action = {
        "action_id": action_id, "tool": "inspect_directory",
        "paths": [r"E:\demo"], "recursive": False,
    }
    raw = encode_result(
        "SUCCEEDED", "DIRECTORY_INSPECTED",
        tool="inspect_directory", executed=True,
        requested_path=r"E:\demo", resolved_path=r"E:\demo",
        data={
            "observed_exists": False, "observed_is_directory": False,
            "observation_status": "CONFIRMED",
        },
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    loop = SimpleNamespace(
        task_id="TASK-GIT55-ARCHIVE", task_epoch="epoch",
        run_id="RR-GIT55-ARCHIVE", turn_id=160,
        action_result_ledger={}, _canonical_action_proposals={action_id: action},
    )
    loop.action_result_ledger[action_id] = {
        "request_id": loop.run_id, "task_id": loop.task_id,
        "task_epoch": loop.task_epoch, "result_id": ref,
        "action_id": action_id, "result": raw,
        "result_sha256": digest, "result_status": "SUCCEEDED",
        "executed": True, "evidence_status": "ACTIVE",
    }
    begin_executor_run(
        tmp_path, loop, request,
        evidence_requirement=assess_finish_evidence_requirement(request),
    )
    append_executor_event(tmp_path, loop, {
        "kind": "TOOL_RESULT", "toolName": "inspect_directory",
        "action_id": action_id,
        "runtimeEvidence": {
            "evidence_ref": ref, "status": "SUCCEEDED",
            "executed": True, "result_sha256": digest,
        },
        "toolResult": {"isSuccess": True, "data": "observed", "error": None},
    })
    for i in range(148):
        append_executor_event(
            tmp_path, loop, {"round": i + 2, "kind": "PLANNER_METADATA_ONLY"}
        )
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(board["archive_chunks"]) > 0
    assert not any(row.get("kind") == "TOOL_RESULT" for row in board["events"])
    terminal = finish_executor_run(
        tmp_path, loop, terminal_kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
        summary="已觀測",
    )
    assert terminal["terminal"]["tool_evidence_refs"] == [ref]
    comparison = compare_runtime_finish(
        loop, tmp_path, request, [{
            "evidence_ref": ref,
            "fields": {
                "status": "SUCCEEDED",
                "data.observed_exists": False,
            },
        }], answer="已觀測",
    )
    assert comparison["verdict"] == "PASS", comparison
    assert comparison["goal_state_table"][0]["actual"]["exists"] is False


def test_planner_wrong_goal_check_is_advisory_not_diagnostic_failure(monkeypatch):
    history = _history()
    checks = [
        {
            "criterion": name + " native child identity must be verified",
            "evidence_ref": history[ix]["evidence_ref"],
            "field": "data.sandbox_evidence.native_executable_identity_verified",
            "operator": "eq", "expected": True,
        } for name, ix in (("git", 4), ("python", 5))
    ]
    outcome = _compare_with_receipts(
        monkeypatch, REQUEST, history, summary=SUMMARY, goal_checks=checks)
    assert outcome["verdict"] == "PASS", outcome
    assert outcome["coverage_basis"] == "RUNTIME_FROZEN_GOAL_CONTRACT"
    assert outcome["model_goal_checks_advisory"]["authoritative"] is False
    assert len(outcome["model_goal_checks_advisory"]["mismatches"]) == 2
    assert outcome["goal_state_table"][7]["actual"]["classification"] == "UNVERIFIED"


def test_frozen_contract_rejects_tampering_or_missing_necessary_action(monkeypatch):
    result = _compare_with_receipts(
        monkeypatch, REQUEST, _history(), summary=SUMMARY,
        mutate_contract=True,
    )
    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "FROZEN_GOAL_CONTRACT_MISSING_OR_TAMPERED"
    missing = _compare_with_receipts(
        monkeypatch, REQUEST, _history(drop=5), summary=SUMMARY,
    )
    assert missing["verdict"] == "FAIL"
    assert missing["reason_code"] == "FROZEN_GOAL_STATE_NOT_SATISFIED"
    assert missing["finish_diagnostic"]["unsatisfied_condition_ids"]
