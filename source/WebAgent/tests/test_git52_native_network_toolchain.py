"""Git52 P0-P2: native network token, pinned tool receipt, fail-closed RCA.

No test grants real network or outside-root write permissions. Actual Windows
AppContainer token/network/filesystem behavior requires user Live validation.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import turbo_os_broker_client as broker
from agent_core.executor_command_rca import capture_command_result_rca

_ROOT = Path(__file__).resolve().parents[3]


def _receipt(root, *, token=True, pins=True, code=0, stderr=""):
    return {
        "schema": broker.RECEIPT_SCHEMA,
        "task_id": "TASK-GIT52NETWORK",
        "action_id": "ACT-GIT52NETWORK",
        "policy_revision": 52,
        "acl_mode": "on",
        "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_read_denied": True,
        "child_process_policy": "JOB_KILL_ON_CLOSE",
        "internet_client_token_verified": token,
        "internet_client_capability_sid": "S-1-15-3-1",
        "native_toolchain_status": "PINNED" if pins else "NOT_ENROLLED",
        "native_trusted_tool_count": 2 if pins else 0,
        "native_trusted_tools": ["git", "python"] if pins else [],
        "process_started": True,
        "executed": True,
        "output_is_authoritative": False,
        "status": "EXECUTED_IN_OS_APPCONTAINER",
        "turbo_root": str(root),
        "command_exit_code": code,
        "stdout_base64": base64.b64encode(b"Git version 2.56.0").decode(),
        "stderr_base64": base64.b64encode(stderr.encode()).decode(),
        "stdout_bytes": 18,
        "stderr_bytes": len(stderr.encode()),
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _set_broker(monkeypatch, tmp_path, receipt):
    monkeypatch.setattr(broker, "_resolve_workspace_and_cwd",
                        lambda cwd: (tmp_path, tmp_path))
    monkeypatch.setattr(broker, "_load_activation", lambda workspace: (
        {"turbo_root": str(tmp_path)},
        {"policy_revision": 52},
        tmp_path / "broker.ps1",
    ))
    monkeypatch.setattr(broker, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(
        broker.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(receipt), stderr=""
        ),
    )


def test_kernel_internet_client_and_verified_toolchain_reach_runtime_rca(
    monkeypatch, tmp_path,
):
    record = _receipt(tmp_path, code=1, stderr="CommandNotFoundException: git")
    _set_broker(monkeypatch, tmp_path, record)
    result = broker.execute_turbo_command(
        "git --version", cwd=tmp_path, task_id=record["task_id"],
        action_id=record["action_id"],
    )
    assert result["exit_code"] == 1
    evidence = result["sandbox_evidence"]
    assert evidence["internet_client_token_verified"] is True
    assert evidence["internet_client_capability_sid"] == "S-1-15-3-1"
    assert evidence["native_trusted_tools"] == ["git", "python"]
    assert evidence["native_error_class"] == "NATIVE_EXECUTABLE_NOT_FOUND"
    rca = capture_command_result_rca(
        "[COMMAND_RESULT]\nstderr:\nCommandNotFoundException: git\n",
        {"status": "FAILED", "executed": True,
         "data": {"exit_code": 1, "sandbox_evidence": evidence}},
    )
    assert rca["sandbox_evidence"]["native_error_class"] == (
        "NATIVE_EXECUTABLE_NOT_FOUND"
    )
    assert rca["sandbox_evidence"]["internet_client_token_verified"] is True


@pytest.mark.parametrize("field,value", [
    ("internet_client_token_verified", False),
    ("internet_client_capability_sid", "S-1-15-3-2"),
    ("native_trusted_tool_count", 3),
    ("native_trusted_tools", ["git", "cmd"]),
    ("native_toolchain_status", "UNVERIFIED"),
    ("kernel_outside_read_denied", False),
    ("sandbox_identity_verified", False),
])
def test_missing_or_forged_native_attestation_is_fatal(
    monkeypatch, tmp_path, field, value,
):
    receipt = _receipt(tmp_path)
    receipt[field] = value
    _set_broker(monkeypatch, tmp_path, receipt)
    with pytest.raises(broker.TurboOSBrokerUnavailable):
        broker.execute_turbo_command(
            "git --version", cwd=tmp_path, task_id=receipt["task_id"],
            action_id=receipt["action_id"],
        )


def test_not_enrolled_is_explicit_without_faking_tool_execution(
    monkeypatch, tmp_path,
):
    receipt = _receipt(tmp_path, pins=False)
    _set_broker(monkeypatch, tmp_path, receipt)
    result = broker.execute_turbo_command(
        "Get-Location", cwd=tmp_path,
        task_id=receipt["task_id"], action_id=receipt["action_id"],
    )
    assert result["sandbox_evidence"]["native_toolchain_status"] == "NOT_ENROLLED"
    assert result["sandbox_evidence"]["native_trusted_tools"] == []


def test_native_source_has_os_capability_proof_and_no_host_path_fallback():
    launcher = (_ROOT / "install_smart_agent/turbo_appcontainer_probe.cs").read_text(
        encoding="utf-8"
    )
    executor = (_ROOT / "install_smart_agent/turbo_os_execute.ps1").read_text(
        encoding="utf-8"
    )
    prepare = (_ROOT / "install_smart_agent/turbo_native_toolchain_prepare.ps1").read_text(
        encoding="utf-8"
    )
    assert "TokenCapabilities = 30" in launcher
    assert 'INTERNET_CLIENT_SID = "S-1-15-3-1"' in launcher
    assert "CapabilityCount = 1" in launcher
    assert "VerifyChildToken(pi.hProcess, sidString);" in launcher
    assert "TokenCapabilities_internetClient_missing" in launcher
    assert "CleanEnvironment(cwd, executable, trustedToolPath)" in launcher
    assert "values[\"PATH\"]" in launcher
    assert "Environment.GetEnvironmentVariable(\"PATH\")" not in launcher
    assert "Get-Pinned-Native-Toolchain" in executor
    assert "GIT52_NATIVE_TOOLCHAIN_SHA256_MISMATCH" in executor
    assert "internet_client_token_verified=$true" in executor
    assert "GIT52_PREPARE_REQUIRES_ELEVATED_ADMINISTRATOR" in prepare
    assert "turbo_broker_code" in prepare
    assert "OPERATOR_PROVISIONED" in prepare
    assert "'*(OI)(CI)F'" not in prepare
