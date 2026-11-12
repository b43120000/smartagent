"""Git52: ensure collector exports exact native precheck causes.

Windows uses a disposable checkout-root fixture; no SmartAgent OS broker or
privileged local user policy is exercised by this test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_TASK = "TASK-GIT52COLLECTOR"
_REQUEST = "RR-TELEGRAM-GIT52COLLECTOR"
_ACTION = "MIN-GIT52COLLECTOR"
_DENIAL = "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"


def test_collector_has_native_diagnostic_and_causal_coverage_contract():
    script = (_REPO / "collect.bat").read_text(encoding="utf-8")
    for required in (
        "executor_native_confirmation_blocks.json",
        "TURBO_NATIVE_CONFIRMATION_BLOCKED",
        "TURBO_NATIVE_COMMAND_SECURITY_CLASSIFIER_MISSING",
        "TURBO_NATIVE_BLOCK_DECISION_LINK_MISSING",
        "command_security_code",
        "command_security_detail_sha256",
    ):
        assert required in script
    assert "raw_command_included=$false" in script


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize("has_classifier,expected_coverage", [
    (True, "FULL_AVAILABLE_LOG_SCAN"),
    (False, "PARTIAL"),
])
def test_real_collector_exports_native_block_and_flags_missing_code(
    tmp_path, has_classifier, expected_coverage,
):
    shutil.copy2(_REPO / "collect.bat", tmp_path / "collect.bat")
    skeleton = tmp_path / "source" / "WebAgent"
    skeleton.mkdir(parents=True)
    (skeleton / "protocol_loop.py").write_text("# disposable fixture\n")
    board_dir = tmp_path / "localdata" / "runtime" / "executor_run_blackboard"
    board_dir.mkdir(parents=True)
    code = "diskpart_forbidden" if has_classifier else ""
    reason = _DENIAL
    common = {
        "round": 7, "action_id": _ACTION,
        "toolName": "run_command", "risk_class": "FORBIDDEN",
        "reason_code": reason, "command_sha256": "a" * 64,
        "arguments_sha256": "b" * 64,
        "command_security_code": code,
        "command_security_approval_eligible": False,
        "command_security_detail_sha256": "c" * 64,
        "broker_decision": "BROKER_REQUIRED",
        "executed": False, "authorization_granted": False,
    }
    board = {
        "schema": "SMARTAGENT_EXECUTOR_RUN_BLACKBOARD_V1",
        "task_id": _TASK, "request_id": _REQUEST,
        "task_epoch": "a" * 32,
        "events": [
            {"kind": "TURBO_DECISION", "decision": "NATIVE_PRECHECK_BLOCKED",
             "auto_confirm": False, "admission_status": "ADMITTED",
             "policy_revision": 52, **common},
            {"kind": "TURBO_NATIVE_CONFIRMATION_BLOCKED", **common},
        ],
        "terminal": {"kind": "BLOCKED", "admission_rejection_count": 0},
    }
    (board_dir / f"{_TASK}.json").write_text(
        json.dumps(board), encoding="utf-8",
    )
    cmd = ["cmd.exe", "/c", str(tmp_path / "collect.bat"), _TASK, _REQUEST]
    finished = subprocess.run(cmd, cwd=str(tmp_path),
                              capture_output=True, text=True, timeout=90)
    assert finished.returncode == 0, (finished.stdout, finished.stderr)
    zips = list((tmp_path / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zipfile_in:
        block = json.loads(zipfile_in.read(
            "executor_native_confirmation_blocks.json"
        ).decode("utf-8-sig"))
        summary = json.loads(zipfile_in.read(
            "executor_admission_summary.json"
        ).decode("utf-8-sig"))
        coverage = json.loads(zipfile_in.read(
            "rca_causal_coverage.json"
        ).decode("utf-8-sig"))
    assert len(block) == 1
    assert block[0]["action_id"] == _ACTION
    assert block[0]["command_security_code"] == code or (
        not has_classifier and block[0]["command_security_code"] is None
    )
    assert block[0]["broker_decision"] == "BROKER_REQUIRED"
    assert block[0]["raw_command_included"] is False
    assert block[0]["executed"] is False
    assert summary["turbo_native_confirmation_block_count"] == 1
    assert coverage["status"] == expected_coverage
    if not has_classifier:
        assert "TURBO_NATIVE_COMMAND_SECURITY_CLASSIFIER_MISSING" in coverage["issues"]
