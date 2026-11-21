"""Windows sidecar IT for Git69 OPEN mode: real Runtime, mocked policy input only.

The fixture simulates installed ACL OFF / Turbo OFF / Auto Approve ON
because GitHub runners do not carry the operator's SmartAgent machine policy.
No execute_tool, broker admission, command_security, bounded_process,
subprocess, evidence storage, approval decision or collector is mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from agent_core.auto_approve_mode import auto_approve_snapshot
from agent_core.runtime_open_fidelity_it import run_open_it
from agent_core.runtime_evidence_store import RuntimeEvidenceError, read_runtime_evidence


def _ci_policy(monkeypatch, workspace: Path):
    policy = {"revision": 69, "turbo_requested": False,
              "auto_approve_requested": True, "denied_write_roots": []}
    access = {"policy": policy, "writable_workspaces": (str(workspace),),
              "read_only_roots": (), "denied_write_roots": (),
              "turbo_roots": ()}
    # Policy inputs only. All actual execution and command authorization remain live.
    monkeypatch.setattr("agent_core.workspace_access.access_snapshot", lambda: access)
    monkeypatch.setattr("agent_core.turbo_sandbox.access_snapshot", lambda: access)
    monkeypatch.setattr("agent_core.turbo_sandbox.acl_mode", lambda: "off")
    monkeypatch.setattr("agent_core.auto_approve_mode.access_snapshot", lambda: access)
    monkeypatch.setattr("agent_core.auto_approve_mode.acl_mode", lambda: "off")
    return access


@pytest.mark.skipif(os.name != "nt", reason="real Windows CMD and PowerShell required")
def test_open_mode_real_runtime_processes_and_immutable_evidence(tmp_path, monkeypatch):
    workspace = tmp_path / "git69_fixture_workspace"
    workspace.mkdir()
    _ci_policy(monkeypatch, workspace)
    effective = auto_approve_snapshot(workspace)
    assert effective["acl"] == "OFF"
    assert effective["turbo_requested"] == "OFF"
    assert effective["turbo_effective"] == "OFF"
    assert effective["auto_approve_effective"] == "ON"
    assert effective["executor_backend"] == "WINDOWS_EXECUTOR"

    report = run_open_it(workspace, policy_source="CI_INJECTED_POLICY")
    assert report["planner_invoked"] is False
    assert report["process_backend_mocked"] is False
    assert report["runtime_dispatch_shared"] is True
    assert report["policy_source"] == "CI_INJECTED_POLICY"
    assert len(report["cases"]) == 10, json.dumps(report, indent=2, ensure_ascii=False)
    assert report["status"] == "PASS", json.dumps(report, indent=2, ensure_ascii=False)
    assert report["passed"] == 10
    assert {x["shell_mode"] for x in report["cases"]} == {
        "DIRECT", "CMD", "POWERSHELL"
    }
    root = Path(__file__).resolve().parents[3]
    for row in report["cases"]:
        assert row["checks"]["evidence_readback"]
        assert row["checks"]["evidence_persisted"]
        assert row["checks"]["not_native"]
        assert row["raw_result_sha256"]
        assert row["evidence_ref"].startswith("MIN-EV-")
        assert row["action_id"].startswith("ACT-G69-")
        assert row["status"] == "PASS"
    first = report["cases"][0]
    with pytest.raises(RuntimeEvidenceError):
        read_runtime_evidence(
            root, task_id=report["task_id"], request_id="RR-FOREIGN",
            task_epoch=report["task_epoch"],
            evidence_ref=first["evidence_ref"], section="stdout",
        )

    # Same collector entrypoint as a user, but a gateway-specific report:
    # never mislabel gateway-only evidence as Planner-produced Blackboard.
    done = subprocess.run(
        ["cmd.exe", "/d", "/c", str(root / "collect.bat"),
         report["task_id"], report["request_id"]],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=120)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    line = next((x for x in done.stdout.splitlines()
                 if x.startswith("[COLLECT] ZIP: ")), "")
    assert line, done.stdout[-3000:]
    archive = Path(line.split("[COLLECT] ZIP: ", 1)[1].strip())
    try:
        with zipfile.ZipFile(archive) as zf:
            assert zf.testzip() is None
            assert "open_execution_fidelity_it.json" in zf.namelist()
            collected = json.loads(zf.read(
                "open_execution_fidelity_it.json").decode("utf-8-sig"))
        assert collected["task_id"] == report["task_id"]
        assert collected["request_id"] == report["request_id"]
        assert collected["policy_source"] == "CI_INJECTED_POLICY"
        assert collected["passed"] == report["passed"]
        assert [r["case_id"] for r in collected["cases"]] == [
            r["case_id"] for r in report["cases"]]
    finally:
        archive.unlink(missing_ok=True)


def test_open_mode_gateway_refuses_execution_without_opt_in_and_valid_host(monkeypatch, tmp_path):
    import agent_core.runtime_open_fidelity_it as it
    monkeypatch.setattr(it.os, "name", "nt")
    monkeypatch.setattr("agent_core.auto_approve_mode.auto_approve_snapshot",
                        lambda workspace: {"acl": "ON"})
    with pytest.raises(RuntimeError, match="POLICY_NOT_VERIFIED"):
        it.run_open_it(tmp_path, policy_source="CI_INJECTED_POLICY")
