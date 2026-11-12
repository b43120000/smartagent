"""Git52 command-RCA capture, redaction and Windows collector roundtrip."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from agent_core.executor_command_rca import (
    capture_command_rca, capture_command_result_rca, redact_for_rca,
)
from agent_core.executor_run_blackboard import read_executor_run
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop

_REPO = Path(__file__).resolve().parents[3]


def test_readable_download_commands_mask_secrets_and_preserve_tool_name():
    raw = (
        "Invoke-WebRequest 'https://github.com/b43120000/test.git"
        "?token=DEMO_USER_TOKEN_123456' -OutFile 'fixture.zip'; "
        "python -m urllib.request -Password 'test-secret-value' "
        "-Headers 'Authorization: Bearer TEST_BEARER_SECRET_1234'"
    )
    result = redact_for_rca(raw)
    assert "Invoke-WebRequest" in result["text"]
    assert "python" in result["text"]
    for forbidden in ("DEMO_USER_TOKEN_123456", "test-secret-value",
                      "TEST_BEARER_SECRET_1234"):
        assert forbidden not in result["text"]
    assert result["redaction_count"] >= 2
    assert result["original_sha256"] == hashlib.sha256(raw.encode()).hexdigest()


def test_captured_original_and_dispatched_commands_have_distinct_sha():
    original = {"command": "git ls-remote https://github.com/b43120000/test.git HEAD",
                "operation": "GIT_INSPECT"}
    dispatched = {"command": original["command"] + " "}
    row = capture_command_rca(original, dispatched)
    assert "git ls-remote" in row["original_command_redacted"]
    assert row["dispatch_matches_original"] is False
    assert row["dispatched_command_sha256"] != row["original_command_sha256"]
    assert row["raw_command_included"] is False


def test_captured_transfer_failure_stderr_and_native_receipt():
    raw_result = (
        "[COMMAND_RESULT]\n"
        "operation: TRANSFER\n"
        "command: Invoke-WebRequest https://example.com/download\n"
        "exit_code: 1\n"
        "stdout:\nCould not resolve host: github.com\n"
        "stderr:\nWebException: The remote name could not be resolved\n"
        "VERIFICATION_STATUS: FAIL\n"
        "[OPERATION_RESULT] {}\n"
        '[RUNTIME_TOOL_RESULT] {"status":"FAILED"}'
    )
    receipt = {
        "status": "FAILED", "reason": "COMMAND_EXITED", "executed": True,
        "data": {
            "exit_code": 1, "verification_status": "FAIL",
            "sandbox_evidence": {
                "sandbox_backend": "WIN32_APPCONTAINER",
                "sandbox_identity_verified": True,
                "kernel_outside_read_denied": True,
                "credential": "not-an-evidence-field",
            },
        },
    }
    row = capture_command_result_rca(raw_result, receipt)
    assert row["command_exit_code"] == 1
    assert "WebException" in row["stderr_excerpt_redacted"]
    assert "Could not resolve host" in row["stdout_excerpt_redacted"]
    assert row["network_failure_hint"] == "DNS"
    assert row["sandbox_evidence"]["sandbox_backend"] == "WIN32_APPCONTAINER"
    assert "credential" not in row["sandbox_evidence"]


def test_command_rca_truncation_and_legacy_result_are_explicit():
    row = redact_for_rca("Get-ChildItem " + "x" * 1000, limit=120)
    assert row["truncated"]
    assert len(row["text"]) < 200
    legacy = capture_command_result_rca(
        '[TOOL_RESULT] {"status":"FAILED"}',
        {"status": "FAILED", "data": {"exit_code": 1}},
    )
    assert legacy["result_capture_status"] == "STRUCTURED_RECEIPT_ONLY"
    assert legacy["stderr_excerpt_redacted"] == ""


def test_executor_blackboard_persists_command_without_executing_twice(
    tmp_path, monkeypatch,
):
    from WebAgent.tests.test_git52_native_broker_full_access_approval import (
        _WRITE, _gate,
    )
    turbo = tmp_path / "turbo"
    turbo.mkdir()
    command = _WRITE
    action = json.dumps({
        "type": "tool_call", "name": "run_command",
        "arguments": {"command": command, "operation": "GENERAL",
                      "working_directory": str(turbo),
                      "success_criteria": "Check Runtime command capture",
                      "verify": [{"action": "file_exists",
                                  "path": str(turbo / "turbo_exact_verify.txt"),
                                  "expect": True}]},
    })
    request = "Capture one Native Broker command for diagnostic evidence"
    loop, _ = _loop(
        tmp_path, monkeypatch,
        [action, '{"type":"blocked","reason":"fixture finished"}'],
        request=request,
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda workspace, proposal:
            _gate(tmp_path, turbo) if proposal and
            proposal.get("tool") == "run_command"
            else {"decision": "NOT_APPLICABLE", "state": None, "evidence": {}},
    )
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    admitted = next(x for x in board["events"] if x["kind"] == "ACTION_ADMITTED")
    result = next(x for x in board["events"] if x["kind"] == "TOOL_RESULT")
    assert admitted["command_rca"]["original_command_redacted"] == command
    assert admitted["command_rca"]["dispatch_matches_original"] is True
    assert result["command_execution_rca"]["result_capture_status"] == (
        "STRUCTURED_RECEIPT_ONLY"
    )
    assert len(loop.executed) == 1


@pytest.mark.skipif(os.name != "nt", reason="Real collector requires Windows 5.1")
def test_collector_exports_readable_commands_and_stderr(tmp_path):
    shutil.copy2(_REPO / "collect.bat", tmp_path / "collect.bat")
    skeleton = tmp_path / "source" / "WebAgent"
    skeleton.mkdir(parents=True)
    (skeleton / "protocol_loop.py").write_text("# test fixture\n")
    task, request, action_id = (
        "TASK-GIT52CMDTRACE", "RR-TELEGRAM-GIT52CMDTRACE", "MIN-CMDTRACE",
    )
    cmd = "git ls-remote https://github.com/b43120000/test.git HEAD"
    command_rca = capture_command_rca(
        {"command": cmd, "operation": "GIT_INSPECT"},
        {"command": cmd, "operation": "GIT_INSPECT"},
    )
    receipt = {"status": "FAILED", "reason": "COMMAND_EXITED",
               "executed": True, "data": {
                   "exit_code": 1, "verification_status": "FAIL",
                   "sandbox_evidence": {
                       "sandbox_backend": "WIN32_APPCONTAINER",
                       "sandbox_identity_verified": True,
                       "kernel_outside_read_denied": True,
                   }}}
    output = capture_command_result_rca(
        "[COMMAND_RESULT]\noperation: GIT_INSPECT\n"
        "command: git ls-remote\nexit_code: 1\n"
        "stderr:\nCould not resolve host: github.com\n"
        "VERIFICATION_STATUS: FAIL\n[OPERATION_RESULT] {}",
        receipt,
    )
    board = {
        "schema": "SMARTAGENT_EXECUTOR_RUN_BLACKBOARD_V1",
        "task_id": task, "request_id": request, "task_epoch": "a" * 32,
        "events": [
            {"kind": "TURBO_DECISION", "toolName": "run_command", "round": 1,
             "action_id": action_id, "command_rca": command_rca,
             "risk_class": "UNKNOWN", "decision": "AUTO_CONFIRM",
             "admission_status": "ADMITTED"},
            {"kind": "ACTION_ADMITTED", "toolName": "run_command", "round": 1,
             "action_id": action_id, "command_rca": command_rca,
             "command_risk_class": "UNKNOWN"},
            {"kind": "TOOL_RESULT", "toolName": "run_command", "round": 1,
             "action_id": action_id, "command_execution_rca": output,
             "runtimeEvidence": {
                 "evidence_ref": "MIN-EV-GIT52CMDTRACE",
                 "status": "FAILED", "executed": True}},
        ],
        "terminal": {"kind": "BLOCKED", "admission_rejection_count": 0},
    }
    target = tmp_path / "localdata" / "runtime" / "executor_run_blackboard"
    target.mkdir(parents=True)
    (target / f"{task}.json").write_text(json.dumps(board), encoding="utf-8")
    finished = subprocess.run(
        ["cmd.exe", "/c", str(tmp_path / "collect.bat"), task, request],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=90,
    )
    assert finished.returncode == 0, (finished.stdout, finished.stderr)
    zips = list((tmp_path / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as bundle:
        rows = json.loads(bundle.read("executor_run_commands.json"))
        summary = json.loads(bundle.read("executor_command_capture_summary.json"))
        text = bundle.read("executor_run_commands.txt").decode("utf-8")
    assert len(rows) == 1
    assert rows[0]["original_command_redacted"] == cmd
    assert rows[0]["dispatched_command_redacted"] == cmd
    assert rows[0]["command_exit_code"] == 1
    assert rows[0]["evidence_ref"] == "MIN-EV-GIT52CMDTRACE"
    assert rows[0]["network_failure_hint"] == "DNS"
    assert rows[0]["sandbox_evidence"]["sandbox_backend"] == "WIN32_APPCONTAINER"
    assert "Could not resolve host" in text
    assert summary["capture_status"] == "CAPTURED"
    assert summary["command_result_captured_count"] == 1
