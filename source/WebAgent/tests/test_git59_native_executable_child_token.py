"""Git59 direct child OS token attestation tests (synthetic broker receipts)."""
from __future__ import annotations

import base64
import copy
import os
from pathlib import Path

import pytest

from agent_core.turbo_os_broker_client import (
    _git59_native_version_probe, _git59_validate_direct_child,
    TurboOSBrokerUnavailable,
)
from agent_core.requirement_goal_state import (
    _diagnostic_actual, compile_requirement_goal_state,
    evaluate_frozen_goal_contract,
)
from WebAgent.tests.test_git55_requirement_goal_state_contract import (
    REQUEST, _history, SUMMARY, GIT, PYTHON,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows installation paths")
def test_only_literal_absolute_pinned_git_python_versions_select_native_probe():
    project = Path(r"C:\smartagent")
    assert _git59_native_version_probe(
        "& '" + GIT + "' --version", project) == "git"
    assert _git59_native_version_probe(
        "& '" + PYTHON + "' --version", project) == "python"
    for value in (
        "git --version", "python --version",
        "& '" + GIT + "' --version; whoami",
        "& '" + PYTHON + "' -c 'print(42)'",
        "& 'C:\\other\\.venv\\Scripts\\python.exe' --version",
        "Get-Command git, python -ErrorAction SilentlyContinue",
        "$env:PATH", "",
    ):
        assert _git59_native_version_probe(value, project) == ""


def _os_receipt(project: Path, name: str):
    path = project / ".venv" / (
        "git/cmd/git.exe" if name == "git" else "Scripts/python.exe")
    return {
        "native_direct_child_probe_kind": name,
        "native_executable_identity_verified": True,
        "native_direct_child_attestation": {
            "schema": "SMARTAGENT_GIT59_DIRECT_NATIVE_CHILD_V1",
            "status": "OS_TOKEN_VERIFIED",
            "sandbox_backend": "WIN32_APPCONTAINER",
            "sandbox_identity_verified": True,
            "internet_client_token_verified": True,
            "internet_client_capability_sid": "S-1-15-3-1",
            "child_process_policy": "JOB_KILL_ON_CLOSE",
            "os_process_started": True,
            "executable_name": name,
            "executable_path": str(path),
            "pid": 321,
            "exit_code": 0,
            "git60_child_stdout_base64": base64.b64encode(
                ("git version 2.47.0" if name == "git" else "Python 3.13.0"
                ).encode("utf-8")).decode("ascii"),
            "git60_child_stderr_base64": "",
            "git60_child_stdout_bytes": len(
                ("git version 2.47.0" if name == "git"
                 else "Python 3.13.0").encode("utf-8")),
            "git60_child_stderr_bytes": 0,
            "git60_child_capture_complete": True,
        },
    }


@pytest.mark.parametrize("name", ["git", "python"])
def test_native_direct_child_accepts_exact_os_token_receipt(tmp_path, name):
    r = _os_receipt(tmp_path, name)
    observed = _git59_validate_direct_child(r, name, tmp_path)
    assert observed == {
        "verified": True,
        "reason": "OS_DIRECT_EXECUTABLE_TOKEN_VERIFIED",
        "name": name, "exit_code": 0, "pid": 321,
        "stdout": "git version 2.47.0" if name == "git" else "Python 3.13.0",
        "stderr": "", "capture_complete": True,
        "stdout_bytes": 18 if name == "git" else 13, "stderr_bytes": 0,
    }


@pytest.mark.parametrize("tamper", [
    "sid", "backend", "token", "tool", "path", "pid", "job", "capability",
    "request", "executable", "started", "schema",
])
def test_native_direct_child_cannot_be_claimed_from_forged_receipt(tmp_path, tamper):
    r = _os_receipt(tmp_path, "git")
    c = r["native_direct_child_attestation"]
    if tamper == "sid":
        c["sandbox_identity_verified"] = False
    elif tamper == "backend":
        c["sandbox_backend"] = "HOST_CMD"
    elif tamper == "token":
        c["internet_client_token_verified"] = False
    elif tamper == "tool":
        c["executable_name"] = "python"
    elif tamper == "path":
        c["executable_path"] = str(tmp_path / "untrusted.exe")
    elif tamper == "pid":
        c["pid"] = 0
    elif tamper == "job":
        c["child_process_policy"] = "NONE"
    elif tamper == "capability":
        c["internet_client_capability_sid"] = "S-1-15-3-2"
    elif tamper == "request":
        r["native_direct_child_probe_kind"] = "python"
    elif tamper == "executable":
        c["executable_name"] = ""
    elif tamper == "started":
        c["os_process_started"] = False
    elif tamper == "schema":
        c["schema"] = "FAKE"
    with pytest.raises(TurboOSBrokerUnavailable):
        _git59_validate_direct_child(r, "git", tmp_path)


def test_direct_probe_failure_is_visible_never_claimed_success(tmp_path):
    r = {
        "native_direct_child_probe_kind": "python",
        "native_executable_identity_verified": False,
        "native_direct_child_attestation": {
            "status": "UNVERIFIED",
            "reason_code": "GIT59_NATIVE_CHILD_CREATEPROCESS_FAILED",
        },
    }
    result = _git59_validate_direct_child(r, "python", tmp_path)
    assert result["verified"] is False
    assert result["exit_code"] is None
    assert result["reason"] == "GIT59_NATIVE_CHILD_CREATEPROCESS_FAILED"


def test_normal_unprobed_commands_never_claim_executable_identity(tmp_path):
    assert _git59_validate_direct_child({
        "native_executable_identity_verified": False,
        "native_direct_child_attestation": {},
    }, "", tmp_path)["verified"] is False
    with pytest.raises(TurboOSBrokerUnavailable,
                       match="GIT59_UNREQUESTED_CHILD_ATTESTATION"):
        _git59_validate_direct_child({
            "native_executable_identity_verified": True,
            "native_direct_child_attestation": _os_receipt(
                tmp_path, "git")["native_direct_child_attestation"],
        }, "", tmp_path)


def test_diagnostic_reports_native_child_proof_separate_from_parent(tmp_path):
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    for name, index, value in (("git", 4, "git version 2.47.0"),
                               ("python", 5, "Python 3.13.0")):
        sandbox = history[index]["receipt"]["data"]["sandbox_evidence"]
        sandbox.update({
            "native_executable_identity_verified": True,
            "native_executable_probe_name": name,
            "native_executable_probe_exit_code": 0,
            "native_executable_probe_pid": 202 + index,
            "native_executable_direct_launch_count": 1,
            "native_executable_probe_is_second_process": True,
            "native_executable_probe_stdout": value,
            "native_executable_probe_stderr": "",
            "native_executable_probe_capture_complete": True,
            "native_executable_probe_output_source":
                "OS_DIRECT_CHILD_PINNED_FILE_HANDLE",
        })
        history[index]["receipt"]["data"]["stdout"] = value
    report = (SUMMARY
              + " Git 與 Python 分別具有真實 Native AppContainer 子程序身分驗證；"
              + " Git 與 Python --version 均正常，Exit Code 0，stderr 空。")
    state = evaluate_frozen_goal_contract(contract, history, report)
    assert state["verdict"] == "PASS", state
    for ix in (6, 7):
        row = state["state_table"][ix]
        assert row["reason_code"] == "NATIVE_CHILD_EXECUTION_VERIFIED"
        assert row["actual"]["native_executable_identity_verified"] is True


def test_diagnostic_success_is_not_child_identity_without_proof():
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    history[4]["receipt"]["data"]["stdout"] = "git version 2.47.0"
    history[5]["receipt"]["data"]["stdout"] = "Python 3.13.0"
    report = (SUMMARY + " 版本 stdout 有值，但 Git/Python Native Child Token "
              "身分 UNVERIFIED，Exit Code=0，stderr 空。")
    state = evaluate_frozen_goal_contract(contract, history, report)
    assert state["verdict"] == "PASS", state
    assert {row["reason_code"] for row in state["state_table"][6:8]} == {
        "NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT"
    }



@pytest.mark.parametrize("tamper", [
    "stdout_base64", "stderr_base64", "stdout_bytes", "capture_complete",
])
def test_git60_direct_child_capture_must_not_be_forged(tmp_path, tamper):
    receipt = _os_receipt(tmp_path, "git")
    child = receipt["native_direct_child_attestation"]
    if tamper == "stdout_base64":
        child["git60_child_stdout_base64"] = "!!!INVALIDBASE64"
    elif tamper == "stderr_base64":
        child["git60_child_stderr_base64"] = "@@"
    elif tamper == "stdout_bytes":
        child["git60_child_stdout_bytes"] = -1
    elif tamper == "capture_complete":
        child["git60_child_capture_complete"] = "yes"
    with pytest.raises(TurboOSBrokerUnavailable,
                       match="GIT60_NATIVE_DIRECT_CHILD_CAPTURE_INVALID"):
        _git59_validate_direct_child(receipt, "git", tmp_path)


def test_git60_parent_only_version_output_cannot_prove_native_child():
    contract = compile_requirement_goal_state(REQUEST)
    history = _history()
    history[4]["receipt"]["data"]["stdout"] = "git version 2.47.0"
    history[5]["receipt"]["data"]["stdout"] = "Python 3.13.0"
    summary = SUMMARY + " Native direct child capture UNVERIFIED"
    report = evaluate_frozen_goal_contract(contract, history, summary)
    assert report["verdict"] == "PASS"
    assert report["state_table"][6]["reason_code"] == \
        "NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT"
    assert report["state_table"][7]["reason_code"] == \
        "NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT"
