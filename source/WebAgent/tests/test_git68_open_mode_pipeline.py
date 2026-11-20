"""Git68 single-mode pipeline: ACL OFF / Turbo OFF / Auto Approve ON.

This is isolated integration/UT, not an actual Telegram API/WebCopilot browser
delivery or user's Windows machine execution. Runtime ToolResult production is
injected; all Runtime-owned evidence persistence, Blackboard and Finish V2 are
executed by production functions. Nothing writes outside pytest tmp_path.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core.auto_approve_mode import auto_approve_snapshot, evaluate_auto_approve
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.runtime_evidence_store import (
    read_runtime_action_result, read_runtime_evidence, RuntimeEvidenceError,
)
from agent_core.tool_result_contract import decode_result, encode_result
from agent_core.tri_one_task_result import (
    make_task_result_from_webagent_loop, validate_task_result,
)
from WebAgent.pokeclaw_executor_loop import run_pokeclaw_executor
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
from WebAgent.tests.test_minimal_core_v10 import tool_json

MODE = ("OFF", "OFF", "ON")
SOURCE_PATH = r"E:\AIspace\case_a\data.csv"
RAW_STDOUT = (
    "case_a\\data.csv\t已確認\n"
    "第二行：原始資料 影像品質 RAW / AE\n"
    "Windows path: " + SOURCE_PATH + "\n"
)
REPLY = "已由 Runtime 確認命令退出碼為 0；原始輸出留存於 Runtime Evidence。"


def _arm_open_mode(monkeypatch, workspace):
    """Use a real writable temp workspace and explicit mocked OS ACL state."""
    policy = {
        "revision": 68, "turbo_requested": False,
        "auto_approve_requested": True, "denied_write_roots": [],
    }
    access = {
        "policy": policy, "writable_workspaces": (str(workspace),),
        "read_only_roots": (), "denied_write_roots": (),
    }
    monkeypatch.setattr("agent_core.auto_approve_mode.access_snapshot", lambda: access)
    monkeypatch.setattr("agent_core.auto_approve_mode.acl_mode", lambda: "off")
    return auto_approve_snapshot(workspace)


def test_open_mode_effective_and_secondary_approval_but_not_security_bypass(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = _arm_open_mode(monkeypatch, workspace)
    assert (
        state["acl"], state["turbo_requested"], state["turbo_effective"],
        state["auto_approve_effective"],
    ) == MODE[:2] + ("OFF", MODE[2])
    assert state["executor_backend"] == "WINDOWS_EXECUTOR"
    assert state["reason_code"] == "ACL_OFF_WORKSPACE_POLICY_OPT_IN"

    allowed = evaluate_auto_approve(
        workspace, "Write-Output OPEN_MODE_UTF8", working_directory=workspace,
    )
    assert allowed["approved"] is True
    assert allowed["executed"] is False
    assert allowed["executor_backend"] == "WINDOWS_EXECUTOR"
    assert allowed["command_sha256"] == hashlib.sha256(
        b"Write-Output OPEN_MODE_UTF8"
    ).hexdigest()

    # Auto Approve is secondary confirmation, not a blanket safety bypass.
    denied = evaluate_auto_approve(
        workspace, "git clean -fdx", working_directory=workspace,
    )
    assert denied["approved"] is False
    assert "HARD_DENIED" in denied["reason_code"]


@pytest.mark.parametrize("source_interface", ["TELEGRAM", "WEB_COPILOT"])
def test_open_mode_planner_runtime_blackboard_finish_and_taskresult_roundtrip(
    tmp_path, monkeypatch, source_interface,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = _arm_open_mode(monkeypatch, workspace)
    assert state["auto_approve_effective"] == "ON"
    request = (
        "NLIT_CASE_ID=IT10\n"
        "本次是 ACL OFF / Turbo OFF / Auto Approve ON 的資料鏈測試。\n"
        "請檢視 Git 狀態，只讀，原始路徑保持："
        + SOURCE_PATH + "\n"
        "不得刪除或部署檔案。"
    )
    loop, _ = _loop(workspace, monkeypatch, [], request=request)
    loop.intent_digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
    loop.max_turns = 5

    # This mode must not select Native AppContainer; OS execution is injected
    # to test bytes, bookkeeping and finishing, not real-machine permissions.
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.assess",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TURBO_DISABLED"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TURBO_DISABLED"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_execution_policy.turbo_planning_snapshot",
        lambda *a, **k: {
            "turbo_requested": "OFF", "turbo_effective": "OFF",
            "status": "DISABLED", "reason_code": "TURBO_DISABLED",
            "policy_revision": 68,
        },
    )

    executed = []
    raw_result = encode_result(
        "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
        data={
            "process_state": "EXITED", "exit_code": 0,
            "stdout": RAW_STDOUT, "stderr": "", "timed_out": False,
            "verification_status": "PASS",
        },
    )

    def execute(action):
        executed.append(dict(action))
        return raw_result

    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        assert request in prompt  # end-to-end original request, no lost slash
        assert SOURCE_PATH in prompt
        assert "Auto Approve ON" in prompt
        assert "TURBO_PLANNER_CONTEXT" not in prompt
        if len(loop.prompts) == 1:
            return tool_json(
                "run_command", command="git status --short",
                operation="GIT_INSPECT",
            )
        recent = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1]
            .split("\n", 1)[0]
        )
        entry = recent[-1]
        preview = json.loads(entry["toolResult"]["data"])
        assert preview["result"]["stdout"] == RAW_STDOUT
        assert preview["result"]["exit_code"] == 0
        assert entry["toolResult"]["isSuccess"] is True
        assert entry["evidence_ref"].startswith("MIN-EV-")
        return json.dumps({
            "type": "finish", "contract_version": 2,
            "summary": REPLY,
            "goal_checks": [{
                "criterion": "原始 Runtime 命令完成",
                "evidence_ref": entry["evidence_ref"],
                "field": "data.exit_code",
                "operator": "eq", "expected": 0,
            }],
        }, ensure_ascii=False)

    loop.planner = planner
    answer = run_pokeclaw_executor(loop, request)
    assert len(executed) == 1
    assert executed[0]["tool"] == "run_command"
    assert executed[0]["command"] == "git status --short"
    assert len(loop.prompts) == 2
    assert loop.terminal_outcome == "SUCCESS", answer
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert REPLY in answer

    board = read_executor_run(
        workspace, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch,
    )
    assert board["task_id"] == loop.task_id
    assert board["request_id"] == loop.run_id
    assert board["goal_driver_state"] == "DISABLED"
    assert board["prerequisite_state"] == "DISABLED"
    assert board["parent_dispatch_state"] == "NOT_DISPATCHED"
    assert board["parent_consumption_state"] == "NOT_APPLICABLE_EXECUTOR_ONLY"
    results = [x for x in board["events"] if x["kind"] == "TOOL_RESULT"]
    assert len(results) == 1
    ev = results[0]
    ref = ev["runtimeEvidence"]["evidence_ref"]
    assert board["terminal"]["tool_evidence_refs"] == [ref]
    assert ev["runtimeEvidence"]["status"] == "SUCCEEDED"
    assert ev["runtimeEvidence"]["executed"] is True
    assert json.loads(ev["toolResult"]["data"])["result"]["stdout"] == RAW_STDOUT

    action_id = executed[0]["action_id"]
    original = read_runtime_action_result(
        workspace, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch, action_id=action_id, evidence_ref=ref,
    )
    assert original == raw_result
    receipt = decode_result(original)
    assert receipt["data"]["stdout"] == RAW_STDOUT
    source_stream = read_runtime_evidence(
        workspace, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch, evidence_ref=ref, section="stdout",
    )
    assert source_stream["content"] == RAW_STDOUT
    assert source_stream["complete"] is True
    assert source_stream["source_sha256"] == hashlib.sha256(
        RAW_STDOUT.encode("utf-8")
    ).hexdigest()

    # The terminal TaskResult is a new read-only projection, not evidence.
    result = make_task_result_from_webagent_loop(
        loop, answer, source_interface=source_interface,
    )
    validate_task_result(result)
    assert result["source_interface"] == source_interface
    assert result["request_id"] == loop.run_id
    assert result["task_id"] == loop.task_id
    assert result["task_epoch"] == loop.task_epoch
    assert result["intent_digest"] == loop.intent_digest
    assert result["answer_text"] == answer
    assert result["task_state"] == "COMPLETED"
    assert result["outcome"] == "SUCCESS"
    assert result["evidence_refs"] == [ref]
    assert result["operational_action_count"] == 1
    assert result["completion_kind"] == "EVIDENCE_BACKED"
    assert result["terminal_acceptance"]["receipt_id"] == (
        loop.terminal_acceptance["receipt_id"]
    )
    assert result["delivery"]["status"] == "PENDING"

    with pytest.raises(RuntimeEvidenceError):
        read_runtime_evidence(
            workspace, task_id=loop.task_id,
            request_id="RR-FOREIGN", task_epoch=loop.task_epoch,
            evidence_ref=ref, section="stdout",
        )


def test_open_mode_auto_approve_receipt_reaches_tool_confirmation_boundary(
    tmp_path, monkeypatch,
):
    """Production approval decision; fake external binary, never executed."""
    from agent_core.tools import _execution_approval_manifest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = _arm_open_mode(monkeypatch, workspace)
    assert state["auto_approve_effective"] == "ON"
    command = "Write-Output OPEN_MODE_SECONDARY_APPROVAL"
    context = SimpleNamespace(
        workspace_root=workspace,
        task_id="TASK-GIT68-AUTO-ON",
        request_id="RR-GIT68-AUTO-ON",
    )

    # Only simulate a tool that requires an approval manifest; production
    # evaluate_auto_approve() and its actual workspace/safety checks still run.
    monkeypatch.setattr(
        "agent_core.tools._native_run_command_admitted",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "agent_core.tools.inspect_command",
        lambda *a, **k: SimpleNamespace(
            allowed=False, approval_eligible=True,
        ),
    )
    monkeypatch.setattr(
        "agent_core.tools.build_command_approval_manifest",
        lambda *a, **k: {
            "schema": "COMMAND_ALLOW_ONCE_V1",
            "target": str(workspace / "synthetic_tool.exe"),
            "manifest_digest": "1" * 64,
        },
    )
    receipt = _execution_approval_manifest(
        {"command": command}, None, context, workspace,
    )
    assert receipt is not None
    assert receipt["_auto_approval"]["decision"] == "AUTO_APPROVED"
    assert receipt["_auto_approval"]["executed"] is False
    assert receipt["_auto_approval"]["policy_revision"] == 68
    assert receipt["_auto_approval"]["executor_backend"] == "WINDOWS_EXECUTOR"
    assert receipt["_auto_approval"]["authorization_scope"] == str(workspace)
    assert receipt["_auto_approval"]["command_sha256"] == hashlib.sha256(
        command.encode("utf-8")
    ).hexdigest()


def _arm_two_writable_roots(
    monkeypatch, first, second, *, read_only=(), denied=(), acl="off",
    enabled=True,
):
    """Both directories are created by the pytest temp fixture."""
    access = {
        "policy": {
            "revision": 69, "turbo_requested": False,
            "auto_approve_requested": enabled,
            "denied_write_roots": [str(path) for path in denied],
        },
        "writable_workspaces": (str(first), str(second)),
        "read_only_roots": tuple(str(path) for path in read_only),
    }
    monkeypatch.setattr(
        "agent_core.auto_approve_mode.access_snapshot", lambda: access,
    )
    monkeypatch.setattr(
        "agent_core.auto_approve_mode.acl_mode", lambda: acl,
    )
    return access


def test_open_mode_cross_writable_roots_auto_approve_exact_build_bat(
    tmp_path, monkeypatch,
):
    """Regression: E-workspace Planner -> different authorized C-workspace BAT."""
    root_a = tmp_path / "E-AIspace"
    root_b = tmp_path / "C-remoteagent"
    project = root_b / "HDRViewer" / "sources" / "Androidviewer"
    root_a.mkdir()
    project.mkdir(parents=True)
    script = project / "buildingAPK.bat"
    script.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    _arm_two_writable_roots(monkeypatch, root_a, root_b)
    command = 'cmd.exe /d /c "' + str(script) + '"'

    from agent_core.command_risk import classify_command
    assert classify_command({
        "tool": "run_command", "command": command, "operation": "BUILD",
    }) == "UNKNOWN"  # Human consent would be required without Auto Approve.
    decision = evaluate_auto_approve(
        root_a, command, working_directory=project,
    )
    assert decision["acl"] == "OFF"
    assert decision["turbo_effective"] == "OFF"
    assert decision["auto_approve_requested"] == "ON"
    assert decision["auto_approve_effective"] == "ON"
    assert decision["approved"] is True
    assert decision["reason_code"] == "AUTO_APPROVE_SECONDARY_CONFIRMATION"
    assert decision["authorization_scope"] == str(root_a)
    assert decision["command_sha256"] == hashlib.sha256(
        command.encode("utf-8")
    ).hexdigest()
    assert decision["executed"] is False
    # Authorization is symmetric, does not require either root to contain
    # the other or the task's active root to match the command's root.
    assert auto_approve_snapshot(project, working_directory=root_a)[
        "auto_approve_effective"
    ] == "ON"


@pytest.mark.parametrize("restricted", [
    "unregistered", "nested_read_only", "nested_denied",
    "nonexistent_cwd", "readonly_same_depth", "workspace_unregistered",
])
def test_open_mode_cross_root_restricted_or_absent_endpoint_denied(
    tmp_path, monkeypatch, restricted,
):
    root_a = tmp_path / "workspace_A"
    root_b = tmp_path / "workspace_B"
    project = root_b / "HDRViewer"
    root_a.mkdir()
    project.mkdir(parents=True)
    excluded = tmp_path / "not_authorized"
    excluded.mkdir()
    readonly = ()
    denied = ()
    active = root_a
    cwd = project
    if restricted == "unregistered":
        cwd = excluded
    elif restricted == "nested_read_only":
        readonly = (project,)
    elif restricted == "nested_denied":
        denied = (project,)
    elif restricted == "nonexistent_cwd":
        cwd = project / "does_not_exist"
    elif restricted == "readonly_same_depth":
        readonly = (root_b,)
    elif restricted == "workspace_unregistered":
        active = excluded

    _arm_two_writable_roots(
        monkeypatch, root_a, root_b, read_only=readonly, denied=denied,
    )
    decision = evaluate_auto_approve(
        active, "Write-Output any", working_directory=cwd,
    )
    assert decision["approved"] is False
    assert decision["auto_approve_effective"] == "OFF"
    assert decision["reason_code"] == "WORKSPACE_NOT_AUTHORIZED_WRITABLE"


def test_cross_writable_roots_keep_all_command_hard_denies(
    tmp_path, monkeypatch,
):
    first, second = tmp_path / "writable_A", tmp_path / "writable_B"
    first.mkdir()
    second.mkdir()
    _arm_two_writable_roots(monkeypatch, first, second)
    assert evaluate_auto_approve(
        first, "Write-Output healthy", working_directory=second,
    )["approved"] is True
    for dangerous in ("git clean -fdx", "icacls C:\\ /grant Everyone:F"):
        decision = evaluate_auto_approve(
            first, dangerous, working_directory=second,
        )
        assert decision["approved"] is False
        assert decision["reason_code"].startswith(
            "COMMAND_SECURITY_HARD_DENIED:"
        )


def test_cross_root_not_active_when_auto_requested_off_or_acl_on(
    tmp_path, monkeypatch,
):
    first, second = tmp_path / "writable_A", tmp_path / "writable_B"
    first.mkdir()
    second.mkdir()
    for acl, enabled in [("off", False), ("on", True)]:
        _arm_two_writable_roots(
            monkeypatch, first, second, acl=acl, enabled=enabled,
        )
        receipt = evaluate_auto_approve(
            first, "Write-Output x", working_directory=second,
        )
        assert receipt["approved"] is False
        assert receipt["auto_approve_effective"] == "OFF"


def test_cross_writable_roots_unknown_build_never_requests_human_apr(
    tmp_path, monkeypatch,
):
    """Executor decision and Blackboard, without running an actual BAT."""
    first, second = tmp_path / "workspace_A", tmp_path / "workspace_B"
    first.mkdir()
    project = second / "HDRViewer"
    project.mkdir(parents=True)
    script = project / "buildingAPK.bat"
    script.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    _arm_two_writable_roots(monkeypatch, first, second)
    # In CI, tmp_path contains a literal pytest segment: the existing
    # BUILD/TEST classifier matches that word even inside an absolute BAT
    # path. Use a CWD-relative BAT invocation here to isolate the Executor
    # Auto Approve boundary. The separate test above covers the exact
    # absolute buildingAPK.bat command.
    command = 'cmd.exe /d /c "buildingAPK.bat"'
    request = "使用兩個已授權 Writable Workspace 編譯 APK 並驗證退出碼"
    loop, _ = _loop(first, monkeypatch, [], request=request)
    loop.max_turns = 5
    loop.tools._security_approval_notifier = lambda *a: pytest.fail(
        "Human APR notifier must not be called when Auto Approve is effective"
    )

    def prohibited_human_approval(*args, **kwargs):
        pytest.fail("UNKNOWN approval requested despite two writable roots")

    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval",
        prohibited_human_approval,
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.assess",
        lambda *a, **kw: {"decision": "NORMAL", "reason_code": "TURBO_DISABLED"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **kw: {"decision": "NORMAL", "reason_code": "TURBO_DISABLED"},
    )
    executed = []

    def runtime_execute(action):
        executed.append(dict(action))
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
            data={
                "exit_code": 0, "stdout": "BUILD SUCCESSFUL",
                "stderr": "", "process_state": "EXITED", "timed_out": False,
                "verification_status": "PASS",
            },
        )

    loop._execute_action = runtime_execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json(
                "run_command", command=command, operation="BUILD",
                working_directory=str(project),
                success_criteria="Build command exits with code zero",
                verify=[{
                    "action": "file_exists",
                    "path": str(project / "app-debug.apk"), "expect": True,
                }],
            )
        preview = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1]
            .split("\n", 1)[0]
        )
        assert "evidence_ref" in preview[-1], f"action_not_dispatched: {preview[-1]}"
        evidence = preview[-1]["evidence_ref"]
        return json.dumps({
            "type": "finish", "contract_version": 2,
            "summary": "BUILD SUCCESSFUL",
            "goal_checks": [{
                "criterion": "Build completed", "evidence_ref": evidence,
                "field": "data.exit_code", "operator": "eq", "expected": 0,
            }],
        })

    loop.planner = planner
    answer = run_pokeclaw_executor(loop, request)
    assert len(executed) == 1, answer
    assert executed[0]["command"] == command
    assert executed[0]["working_directory"] == str(project)
    board = read_executor_run(
        first, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    evaluations = [
        event for event in board["events"]
        if event.get("kind") == "AUTO_APPROVAL_EVALUATED"
    ]
    assert len(evaluations) == 1, board["events"]
    assert evaluations[0]["auto_approval"]["approved"] is True
    assert evaluations[0]["auto_approval"]["auto_approve_effective"] == "ON"
    assert any(
        event.get("kind") == "AUTO_APPROVAL_DECIDED"
        and event.get("state") == "APPROVED"
        for event in board["events"]
    )
    assert not any(
        event.get("kind", "").startswith("HUMAN_APPROVAL")
        for event in board["events"]
    )
    assert loop.terminal_outcome == "SUCCESS", answer
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
