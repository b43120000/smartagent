"""Git70 real Windows sidecar: policy mocked only, processes and MIN-EV real."""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from agent_core.auto_approve_mode import auto_approve_snapshot
from agent_core.runtime_goal_evidence_it import run_goal_it


def _ci_policy(monkeypatch, workspace):
    policy={"revision":70,"turbo_requested":False,
            "auto_approve_requested":True,"denied_write_roots":[]}
    access={"policy":policy,"writable_workspaces":(str(workspace),),
            "read_only_roots":(),"denied_write_roots":(),"turbo_roots":()}
    monkeypatch.setattr("agent_core.workspace_access.access_snapshot",lambda:access)
    monkeypatch.setattr("agent_core.turbo_sandbox.access_snapshot",lambda:access)
    monkeypatch.setattr("agent_core.turbo_sandbox.acl_mode",lambda:"off")
    monkeypatch.setattr("agent_core.auto_approve_mode.access_snapshot",lambda:access)
    monkeypatch.setattr("agent_core.auto_approve_mode.acl_mode",lambda:"off")


@pytest.mark.skipif(os.name!="nt",reason="Windows real Process/CMD/PowerShell")
def test_windows_real_runtime_matcher_collector(tmp_path,monkeypatch):
    ws=tmp_path/"git70_real_workspace"
    ws.mkdir()
    _ci_policy(monkeypatch,ws)
    state=auto_approve_snapshot(ws)
    assert {key:state[key] for key in ["acl","turbo_requested","turbo_effective",
                                       "auto_approve_effective"]}=={
                                           "acl":"OFF","turbo_requested":"OFF",
                                           "turbo_effective":"OFF","auto_approve_effective":"ON"}
    report=run_goal_it(ws,policy_source="CI_INJECTED_POLICY")
    assert report["status"]=="PASS",json.dumps(report,indent=2,ensure_ascii=False)
    assert report["passed"]==14
    assert report["failed"]==0
    assert all(report["negative_probes"].values())
    assert all(row["match_state"]=="MATCH" for row in report["cases"])
    assert all(row["frozen_expected_sha256"] for row in report["cases"])
    missing=next(row for row in report["cases"] if row["case_id"]=="DIRECT_MISSING")
    assert missing["status"]=="PASS"
    assert missing["safety_decision"]=="CONTINUE_OR_REPLAN"
    timeout=next(row for row in report["cases"] if row["case_id"]=="DIRECT_TIMEOUT")
    assert timeout["status"]=="PASS"
    assert timeout["safety_decision"]=="PAUSE_AND_RECONCILE"

    install=Path(__file__).resolve().parents[3]
    result=subprocess.run(["cmd.exe","/d","/c",str(install/"collect.bat"),
                           report["task_id"],report["request_id"]],
                          cwd=install,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,errors="replace",timeout=150)
    assert result.returncode==0,result.stdout[-1400:]+result.stderr[-1400:]
    zip_line=next((line for line in result.stdout.splitlines()
                   if line.startswith("[COLLECT] ZIP: ")),"")
    assert zip_line,result.stdout[-1700:]
    archive=Path(zip_line.split("[COLLECT] ZIP: ",1)[1].strip())
    try:
        with zipfile.ZipFile(archive) as zf:
            assert zf.testzip() is None
            assert "git70_expected_observed_matrix.json" in zf.namelist()
            exported=json.loads(zf.read("git70_expected_observed_matrix.json"))
            assert exported["status"]=="IDENTITY_VERIFIED",exported
            assert exported["record_count"]==14
            assert exported["task_id"]==report["task_id"]
            assert exported["request_id"]==report["request_id"]
            assert not exported["source_encrypted"]
            assert not exported["raw_tool_output_included"]
            assert all(row["match_state"]=="MATCH" for row in exported["records"])
            assert {row["action_id"] for row in exported["records"]}=={
                row["action_id"] for row in report["cases"]}
            assert {row["evidence_ref"] for row in exported["records"]}=={
                row["evidence_ref"] for row in report["cases"]}
            assert all(row["frozen_expected_sha256"] for row in exported["records"])
            for row in exported["records"]:
                assert row["assertions"]
                assert all(x["match_state"]=="MATCH" for x in row["assertions"])
                assert row["replay_original_action"] is False
            # Readable, sanitized RCA file is persisted as CI artifact.
            out=install/"git70-ci-evidence"
            out.mkdir(parents=True,exist_ok=True)
            (out/(report["task_id"]+"_collected.json")).write_bytes(
                zf.read("git70_expected_observed_matrix.json"))
    finally:
        archive.unlink(missing_ok=True)


@pytest.mark.skipif(os.name!="nt",reason="Windows installed-host policy guard")
def test_git70_gateway_checks_actual_effective_policy(monkeypatch,tmp_path):
    import agent_core.runtime_open_fidelity_it as old
    monkeypatch.setattr("agent_core.auto_approve_mode.auto_approve_snapshot",
                        lambda _:{"acl":"ON"})
    with pytest.raises(RuntimeError,match="POLICY_NOT_VERIFIED"):
        run_goal_it(tmp_path,policy_source="CI_INJECTED_POLICY")
