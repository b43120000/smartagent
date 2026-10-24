"""IT4 focused regressions. Run with PYTHONPATH=source on the TEST candidate.

No repository mutation, browser access or ChatGPT calls. The PowerShell
integration executes only read-only process probes inside the test runner.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from agent_core.bounded_repair import (BoundedRepairBudget, SemanticStagnationGuard, repair_deadline_expired)
from agent_core.evidence_identity_audit import audit_terminal_references
from agent_core.prerequisite_resolution import assess_prerequisites
from agent_core.tools import _powershell_with_native_exit_propagation


def test_native_exit() -> None:
    code = _powershell_with_native_exit_propagation("python -B -c \"raise SystemExit(7)\"")
    assert "$global:LASTEXITCODE" in code
    assert "exit [int]$global:LASTEXITCODE" in code
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        print("SKIP: PowerShell executable unavailable; require Windows TEST rerun")
        return
    native = f'& "{sys.executable}" -B -c "raise SystemExit(7)"'
    p = subprocess.run(
        [shell, "-NoProfile", "-Command", _powershell_with_native_exit_propagation(native)],
        text=True, capture_output=True, timeout=30,
    )
    assert p.returncode == 7, (p.returncode, p.stdout, p.stderr)
    ok = subprocess.run(
        [shell, "-NoProfile", "-Command",
         _powershell_with_native_exit_propagation("Write-Output 'ok'")],
        text=True, capture_output=True, timeout=30,
    )
    assert ok.returncode == 0, (ok.returncode, ok.stderr)
    print("PASS: native exit 7 and shell-only success 0")


def test_repair_and_scope() -> None:
    budget = BoundedRepairBudget(max_without_evidence=4)
    assert budget.rejected_turns == 0
    assert budget.observe([{"reason": "WORKING_DIRECTORY_REJECTED"}],
                          evidence_generation=0)["status"] == "REPAIR_ALLOWED"
    assert budget.rejected_turns == 1
    guard = SemanticStagnationGuard(max_same_blocker=3)
    action = {"tool": "query_project"}
    error = {
        "result_reason": "PREFLIGHT_REJECTED",
        "result_status": "REJECTED",
        "verification_status": "UNKNOWN",
        "executed": False,
    }
    assert guard.observe(action, error)["status"] == "CONTINUE"
    assert guard.observe({"tool": "inspect_project_scope"}, error)["status"] == "CONTINUE"
    last = guard.observe(action, error)
    assert last["status"] == "EXHAUSTED"
    assert last["authorization_changed"] is False
    assert not repair_deadline_expired(2, 1000)
    assert not repair_deadline_expired(3, 599.9)
    assert repair_deadline_expired(3, 600)
    # An expected negative predicate must never be counted as infrastructure denial.
    assert guard.observe({"tool": "observe_fact"}, {
        "result_reason": "FACT_PREDICATE_FAILED",
        "result_status": "SUCCEEDED",
        "verification_status": "FAIL",
        "executed": True,
    })["status"] == "CONTINUE"


def test_project_prerequisite() -> None:
    advice = assess_prerequisites(
        "請使用正式 query_project 讀取專案索引與 snapshot_id",
        [r"C:\authorized\project"], {},
    )
    assert advice["request_class"] == "PROJECT_QUERY", advice
    ids = {row["id"] for row in advice["checks"]}
    assert {"project_root", "project_workspace", "project_snapshot"} <= ids
    assert advice["authorization_granted"] is False
    assert advice["executed"] is False
    assert advice["verification_complete"] is False
    assert "INDEX_ONLY" in advice["on_missing"]["recoverable"]


def test_receipt_reference_audit() -> None:
    ledger = {
        "a1": {"request_id": "R1", "task_epoch": "E1"},
        "a2": {"request_id": "R2", "task_epoch": "E2"},
    }
    good = audit_terminal_references(["REQUEST_ACCEPTED", "a1"], ledger, "R1", "E1")
    assert good["identity_status"] == "SCOPED"
    assert good["action_ref_count"] == 1
    bad = audit_terminal_references(["a1", "a2", "unknown"], ledger, "R1", "E1")
    assert bad["identity_status"] == "SUSPECT"
    assert bad["foreign_ref_count"] == 1
    assert bad["unknown_ref_count"] == 1


if __name__ == "__main__":
    test_native_exit()
    test_repair_and_scope()
    test_project_prerequisite()
    test_receipt_reference_audit()
    print("PASS: IT4 focused regression suite")
