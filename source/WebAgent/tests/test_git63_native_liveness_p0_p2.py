"""Git63 Native AppContainer P0/P1/P2 contract and OS-evidence tests.

Synthetic receipts verify fail-closed interpretation only; Windows sandbox
execution, ACL enforcement and the PowerShell 5.1 parser require live tests.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import turbo_os_broker_client as client

ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "install_smart_agent" / "turbo_appcontainer_probe.cs"
BROKER_PS1 = ROOT / "install_smart_agent" / "turbo_os_execute.ps1"


def _mock_receipt(tmp_path, code=0, stderr="", samples=4):
    action_id = "ACT-GIT63LIVENESS"
    observed_at = time.time() - 1.5
    proof = {
        "schema": "SMARTAGENT_PROCESS_LIVENESS_V1",
        "execution_id": action_id,
        "process_state": "RUNNING",
        "liveness_verified": True,
        "process_identity_verified": True,
        "evidence_source": "NATIVE_WIN32_WAIT_FOR_SINGLE_OBJECT",
        "pid": 4567,
        "observed_at": observed_at,
        "running_sample_count": samples,
        "child_process_policy": "JOB_KILL_ON_CLOSE",
        "terminal_eligible": False,
    } if samples else {}
    return {
        "schema": client.RECEIPT_SCHEMA,
        "task_id": "TASK-GIT63LIVENESS",
        "action_id": action_id,
        "policy_revision": 63,
        "acl_mode": "on",
        "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_read_denied": True,
        "kernel_outside_write_denied": True,
        "child_process_policy": "JOB_KILL_ON_CLOSE",
        "internet_client_token_verified": True,
        "internet_client_capability_sid": "S-1-15-3-1",
        "native_toolchain_status": "NOT_ENROLLED",
        "native_trusted_tool_count": 0,
        "native_trusted_tools": [],
        "process_started": True,
        "executed": True,
        "output_is_authoritative": False,
        "status": "EXECUTED_IN_OS_APPCONTAINER",
        "turbo_root": str(tmp_path),
        "command_exit_code": code,
        "stdout_base64": base64.b64encode(b"").decode(),
        "stderr_base64": base64.b64encode(stderr.encode()).decode(),
        "stdout_bytes": 0,
        "stderr_bytes": len(stderr.encode()),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "native_process_pid": 4567,
        "native_running_sample_count": samples,
        "native_last_running_evidence": proof,
        "native_failure_stage": "",
        "native_failure_substage": "",
        "native_exception_type": "",
        "native_exception_hresult": "",
        "native_failure_stage_source": "NONE",
        "native_command_trace": ["CWD_GET_LOCATION_INITIAL.START", "CWD_GET_LOCATION_INITIAL.PASS"],
    }


def _execute(monkeypatch, tmp_path, receipt):
    monkeypatch.setattr(client, "_resolve_workspace_and_cwd",
                        lambda cwd: (tmp_path, tmp_path))
    monkeypatch.setattr(client, "_load_activation", lambda workspace: (
        {"turbo_root": str(tmp_path)},
        {"policy_revision": 63},
        tmp_path / "protected_native_broker.ps1",
    ))
    monkeypatch.setattr(client, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(client.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(
                            returncode=0,
                            stdout=json.dumps(receipt),
                            stderr="",
                        ))
    return client.execute_turbo_command(
        "Start-Sleep -Seconds 12", cwd=tmp_path, timeout=30,
        task_id=receipt["task_id"], action_id=receipt["action_id"],
    )


def test_silent_native_command_returns_genuine_historical_running_receipt(monkeypatch, tmp_path):
    receipt = _mock_receipt(tmp_path, samples=5)
    result = _execute(monkeypatch, tmp_path, receipt)
    assert result["exit_code"] == 0
    assert result["stdout"] == ""
    assert result["process_state"] == "EXITED"
    assert result["liveness_evidence"]["process_state"] == "EXITED"
    assert result["liveness_evidence"]["liveness_verified"] is False
    last = result["last_running_evidence"]
    assert last["pid"] == 4567
    assert last["execution_id"] == receipt["action_id"]
    assert last["liveness_verified"] is True
    assert last["evidence_source"] == "NATIVE_WIN32_WAIT_FOR_SINGLE_OBJECT"
    assert last["running_sample_count"] == 5


@pytest.mark.parametrize("field,value", [
    ("native_process_pid", 0),
    ("native_running_sample_count", -1),
    ("native_running_sample_count", 151),
    ("native_last_running_evidence", {}),
    ("native_last_running_evidence", {"schema": "FAKE"}),
])
def test_malformed_native_running_receipt_rejected(monkeypatch, tmp_path, field, value):
    receipt = _mock_receipt(tmp_path)
    receipt[field] = value
    with pytest.raises(client.TurboOSBrokerUnavailable,
                       match="GIT63_NATIVE_RUNNING_EVIDENCE_INVALID"):
        _execute(monkeypatch, tmp_path, receipt)


def test_native_wrong_execution_identity_rejected(monkeypatch, tmp_path):
    receipt = _mock_receipt(tmp_path)
    receipt["native_last_running_evidence"]["execution_id"] = "ACT-OTHER"
    with pytest.raises(client.TurboOSBrokerUnavailable,
                       match="GIT63_NATIVE_RUNNING_EVIDENCE_INVALID"):
        _execute(monkeypatch, tmp_path, receipt)


def test_stale_running_observation_rejected(monkeypatch, tmp_path):
    receipt = _mock_receipt(tmp_path)
    receipt["native_last_running_evidence"]["observed_at"] = time.time() - 9999
    with pytest.raises(client.TurboOSBrokerUnavailable,
                       match="GIT63_NATIVE_RUNNING_EVIDENCE_INVALID"):
        _execute(monkeypatch, tmp_path, receipt)


def test_native_failed_command_preserves_phase_and_does_not_claim_success(monkeypatch, tmp_path):
    receipt = _mock_receipt(
        tmp_path, code=1, samples=0,
        stderr="SMARTAGENT_NATIVE_FAILURE_STAGE=CWD_SETUP;UnauthorizedAccessException:Access denied",
    )
    receipt.update(
        native_failure_stage="CWD_SETUP",
        native_failure_substage="CWD_GET_LOCATION_INITIAL",
        native_exception_type="UnauthorizedAccessException",
        native_exception_hresult="80070005",
        native_failure_stage_source="SANDBOX_WRAPPER_DIAGNOSTIC",
    )
    result = _execute(monkeypatch, tmp_path, receipt)
    assert result["exit_code"] == 1
    assert result["process_state"] == "EXITED"
    assert result["last_running_evidence"] is None
    evidence = result["sandbox_evidence"]
    assert evidence["native_error_class"] == "NATIVE_ACCESS_DENIED"
    assert evidence["native_failure_stage"] == "CWD_SETUP"
    assert evidence["native_failure_substage"] == "CWD_GET_LOCATION_INITIAL"
    assert evidence["native_exception_hresult"] == "80070005"
    assert evidence["native_exception_type"] == "UnauthorizedAccessException"


def test_native_with_zero_running_samples_is_not_fabricated(monkeypatch, tmp_path):
    receipt = _mock_receipt(tmp_path, samples=0)
    result = _execute(monkeypatch, tmp_path, receipt)
    assert result["exit_code"] == 0
    assert result["last_running_evidence"] is None
    assert result["sandbox_evidence"]["native_running_sample_count"] == 0


def test_native_capture_sid_permissions_are_file_scoped():
    src = NATIVE.read_text(encoding="utf-8")
    assert "AllowAppContainerCapture(stdoutPath, moniker);" in src
    assert "AllowAppContainerCapture(stderrPath, moniker);" in src
    assert "new System.Security.Principal.SecurityIdentifier(" in src
    assert "DeriveSid(moniker)" in src
    assert "System.IO.File.SetAccessControl(path, acl)" in src
    assert "GIT63_NATIVE_CAPTURE_ACL_SETUP_DENIED" in src
    assert "GetAccessControl(cwd)" not in src
    assert "SetAccessControl(cwd)" not in src


def test_native_samples_original_process_handle_not_stdout_or_pid():
    src = NATIVE.read_text(encoding="utf-8")
    assert "VerifyChildToken(pi.hProcess, sidString);" in src
    assert "WaitForSingleObject(pi.hProcess, interval)" in src
    assert "lastRunningObservedAt" in src
    assert "runningSampleCount++" in src
    assert "TerminateJobObject(job, 124)" in src
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in src


def test_broker_marks_phases_and_does_not_skip_cwd_validation():
    src = BROKER_PS1.read_text(encoding="utf-8")
    for phase in ("CWD_SETUP", "ENV_SETUP", "COMMAND_EXECUTE", "OUTPUT_CAPTURE"):
        assert phase in src
    assert "Set-Location -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop" in src
    assert "GIT63_NATIVE_TASK_CWD_MISMATCH" in src
    assert "native_last_running_evidence=$nativeRunningEvidence" in src
    assert "native_running_sample_count=$nativeRunningSamples" in src
    assert "native_failure_stage=$nativeFailureStage" in src
