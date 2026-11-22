"""Git71: full Executor-only sidecar with actual Windows subprocess/MIN-EV.

Only Planner decisions and OS-policy inputs are fixture-provided. Runtime
dispatch, bounded_process, Win32 taskkill tree, MIN-EV, Matcher, Blackboard,
Finish V2 and collect.bat are production implementations. No Telegram API.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.auto_approve_mode import auto_approve_snapshot
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.runtime_evidence_store import read_runtime_action_result
from agent_core.tool_result_contract import decode_result
from agent_core.tools import execute_tool
from WebAgent.pokeclaw_executor_loop import run_pokeclaw_executor
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
from WebAgent.tests.test_git70_open_goal_evidence_it import _ci_policy

EXPECTED_TIMEOUT={"contract_version":1,"aggregation":"ALL",
    "assertions":[{"field":"data.timed_out","operator":"eq","value":True},
                  {"field":"data.process_started","operator":"eq","value":True},
                  {"field":"data.exit_code","operator":"is_null","value":None}]}


def _specs(workspace:Path, goal:Path, *, expected_goal:bool=True, declaration:bool=True):
    timed={
        "tool":"run_command","operation":"TEST","shell_mode":"DIRECT",
        "working_directory":str(workspace),"timeout":1,
        "command":str(Path(sys.executable).resolve()),
        "argv":["-c","import time; time.sleep(4)"],
        "success_criteria":"Observe the bounded Timeout and then independently confirm the scoped goal directory",
        "verify":[{"action":"file_exists","path":str(goal),"expect":True}],
        "expected":EXPECTED_TIMEOUT,
    }
    if declaration:
        timed["reconciliation"]={"version":1,"checks":[{
            "tool":"observe_fact","path":str(goal),
            "expected_exists":expected_goal,
        }]}
    observed={
        "tool":"observe_fact","path":str(goal),
        "expected":{"contract_version":1,"aggregation":"ALL",
                    "assertions":[{"field":"data.observations.0.value",
                                   "operator":"eq","value":expected_goal}]},
    }
    return timed,observed


def _run_real_sidecar(workspace:Path, monkeypatch, *, with_reconciliation:bool=True,
                      expected_exists:bool=True):
    install=Path(__file__).resolve().parents[3]
    _ci_policy(monkeypatch,workspace)
    state=auto_approve_snapshot(workspace)
    assert {k:state[k] for k in ["acl","turbo_requested","turbo_effective","auto_approve_effective"]}=={
        "acl":"OFF","turbo_requested":"OFF","turbo_effective":"OFF","auto_approve_effective":"ON"}
    goal=workspace/"tracked_goal_directory"
    goal.mkdir(parents=True)
    # Single-directory task has a real source-bound frozen Goal Contract;
    # the Timeout is a negative-test action, not the desired directory state.
    request="請確認資料夾 "+str(goal)+" 是否存在"
    loop,_=_loop(install,monkeypatch,[],request=request)
    loop.task_id="TASK-G71IT-"+uuid.uuid4().hex.upper()
    loop.run_id="RR-G71IT-"+uuid.uuid4().hex.upper()
    loop.task_epoch="EPOCH-G71IT-"+uuid.uuid4().hex.upper()
    loop.workspace=workspace
    loop.progress_root=install
    loop.tools.workspace_root=workspace
    loop.max_turns=8
    timed,observed=_specs(
        workspace,goal,expected_goal=expected_exists,
        declaration=with_reconciliation)
    agent=SimpleNamespace(
        workspace_root=workspace,workspace=workspace,
        current_task_id=loop.task_id,current_request_id=loop.run_id,
        current_task_epoch=loop.task_epoch,
        interface_name="runtime_goal_evidence_it",_models_registry={},
        last_verification_status="UNVERIFIED",
    )
    def real_tool(action):
        loop.executed.append(dict(action))
        return execute_tool(action,agent=agent)
    loop._execute_action=real_tool
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        round_no=len(loop.prompts)
        if round_no==1:
            return json.dumps({"type":"tool_call","name":"run_command","arguments":{k:v for k,v in timed.items() if k!="tool"}})
        if round_no==2:
            if not with_reconciliation:
                pytest.fail("legacy Timeout incorrectly continued")
            assert "GIT71_TIMEOUT_RECONCILIATION_REQUIRED" in prompt, prompt[-1800:]
            return json.dumps({"type":"tool_call","name":"observe_fact","arguments":{k:v for k,v in observed.items() if k!="tool"}})
        if round_no==3:
            if not expected_exists:
                pytest.fail("mismatched observation incorrectly accepted")
            # An independent post-reconciliation Action proves the Executor
            # can resume normal planning (not merely run a recovery helper).
            # The Goal compiler independently checks path_kind=DIRECTORY.
            return json.dumps({
                "type":"tool_call","name":"observe_fact",
                "arguments":{"path":str(goal),
                    "expected":{"contract_version":1,"aggregation":"ALL",
                        "assertions":[
                            {"field":"data.observations.0.value","operator":"eq","value":True},
                            {"field":"data.observations.0.path_kind","operator":"eq","value":"DIRECTORY"}]}}
            },ensure_ascii=False)
        if round_no==4:
            last_ref=loop.action_result_ledger[loop.executed[-1]["action_id"]]["result_id"]
            return json.dumps({
                "type":"finish","contract_version":2,
                "summary":"已由真實 Runtime 驗證目標資料夾存在且類型正確；預期 Timeout 已安全補證。",
                "goal_checks":[{"criterion":"目標資料夾存在",
                   "evidence_ref":last_ref,
                   "field":"data.observations.0.value",
                   "operator":"eq","expected":True}],
            },ensure_ascii=False)
        report=read_executor_run(install,task_id=loop.task_id,
                                  request_id=loop.run_id,task_epoch=loop.task_epoch) or {}
        proposals=[(v.get("kind"),v.get("comparator_reason_code"),
                    v.get("reason_code"),v.get("finish_diagnostic"))
                   for v in report.get("events",[]) if v.get("kind") in {
                        "MODEL_FINISH_PROPOSAL","MODEL_FINISH_REPAIR",
                        "MODEL_FINISH_CONTRACT_INVALID"}]
        pytest.fail("unexpected planner round "+str(round_no)+
                    " comparator="+repr(proposals[-4:])+
                    " prompt_tail="+prompt[-1900:])
    loop.planner=planner
    reply=run_pokeclaw_executor(loop,request)
    board=read_executor_run(install,task_id=loop.task_id,
                            request_id=loop.run_id,task_epoch=loop.task_epoch)
    assert board and board["events"]
    return loop,board,reply,goal


@pytest.mark.skipif(os.name!="nt",reason="Actual Win32 taskkill tree and filesystem.stat")
def test_git70_pause_reproduced_by_real_timeout_without_reconciliation(tmp_path,monkeypatch):
    ws=tmp_path/"baseline_workspace"
    ws.mkdir()
    loop,board,reply,goal=_run_real_sidecar(
        ws,monkeypatch,with_reconciliation=False)
    assert len(loop.executed)==1,loop.executed
    assert loop.terminal_outcome=="UNKNOWN",reply
    last=[e for e in board["events"] if e.get("kind")=="TOOL_RESULT"][-1]
    assert last["runtimeEvidence"]["status"]=="UNKNOWN"
    m=last["expected_observed_match"]
    assert m["match_state"]=="MATCH"
    assert m["safety_decision"]=="PAUSE_AND_RECONCILE"
    assert board["terminal"]["kind"]=="EXECUTION_OUTCOME_UNKNOWN"


@pytest.mark.skipif(os.name!="nt",reason="Actual Windows Timeout to Finish V2 pipeline")
def test_recovery_real_executor_next_action_finish_v2_collect(tmp_path,monkeypatch):
    ws=tmp_path/"full_pipeline_workspace"
    ws.mkdir()
    loop,board,reply,goal=_run_real_sidecar(ws,monkeypatch)
    assert len(loop.executed)==3,loop.executed
    assert loop.terminal_outcome=="SUCCESS",reply
    assert loop.terminal_acceptance["final_verdict"]=="PASS",reply
    originals=[e for e in board["events"] if e.get("kind")=="TOOL_RESULT"]
    assert len(originals)==3
    timeout=originals[0]
    observed=originals[1]
    assert timeout["runtimeEvidence"]["status"]=="UNKNOWN"
    source=timeout["runtimeEvidence"]["evidence_ref"]
    independent=observed["runtimeEvidence"]["evidence_ref"]
    assert timeout["expected_observed_match"]["match_state"]=="MATCH"
    assert timeout["expected_observed_match"]["safety_decision"]=="PAUSE_AND_RECONCILE"
    assert observed["expected_observed_match"]["match_state"]=="MATCH"
    assert originals[2]["expected_observed_match"]["match_state"]=="MATCH"
    stages=[e for e in board["events"]
            if e.get("kind")=="TIMEOUT_RECONCILIATION_COMPLETED"]
    assert len(stages)==1
    stage=stages[0]
    assert stage["source_evidence_ref"]==source
    assert stage["verification_evidence_ref"]==independent
    assert stage["process_finality_verified"] is True
    assert stage["side_effect_scope"]=="DECLARED_PATHS_ONLY"
    assert stage["replay_original_action"] is False
    timeout_id=loop.executed[0]["action_id"]
    raw=read_runtime_action_result(
        Path(__file__).resolve().parents[3],
        task_id=loop.task_id,request_id=loop.run_id,task_epoch=loop.task_epoch,
        action_id=timeout_id,evidence_ref=source)
    receipt=decode_result(raw)
    assert receipt["status"]=="UNKNOWN"
    assert receipt["data"]["possible_partial_execution"] is True
    assert receipt["data"]["process_state"]=="UNKNOWN"
    assert receipt["data"]["timeout_termination"]["tree_termination_verdict"]=="VERIFIED"

    install=Path(__file__).resolve().parents[3]
    proc=subprocess.run(["cmd.exe","/d","/c",str(install/"collect.bat"),
                         loop.task_id,loop.run_id],
                        cwd=install,text=True,stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,errors="replace",timeout=150)
    assert proc.returncode==0,proc.stderr[-1800:]+proc.stdout[-1800:]
    zip_line=next((x for x in proc.stdout.splitlines()
                    if x.startswith("[COLLECT] ZIP: ")),"")
    assert zip_line,proc.stdout[-1800:]
    archive=Path(zip_line.split("[COLLECT] ZIP: ",1)[1].strip())
    try:
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            matrix=json.loads(z.read("git70_expected_observed_matrix.json"))
            recovery=json.loads(z.read("timeout_reconciliation.json"))
            assert matrix["record_count"]==3,matrix
            assert recovery["status"]=="IDENTITY_VERIFIED",recovery
            assert recovery["record_count"]>=3,recovery
            assert recovery["task_id"]==loop.task_id
            assert recovery["request_id"]==loop.run_id
            assert any(x["state"]=="RECONCILED" for x in recovery["records"])
            assert any(x["verification_evidence_ref"]==independent
                       for x in recovery["records"])
            assert any(x["source_evidence_ref"]==source for x in recovery["records"])
            assert all(not x["replay_original_action"] for x in recovery["records"])
            artifact_dir=install/"git71-ci-evidence"
            artifact_dir.mkdir(parents=True,exist_ok=True)
            (artifact_dir/(loop.task_id+"_timeout_reconciliation.json")).write_bytes(
                z.read("timeout_reconciliation.json"))
    finally:
        archive.unlink(missing_ok=True)


@pytest.mark.skipif(os.name!="nt",reason="Actual Windows Timeout and real mismatched observation")
def test_mismatched_recovery_never_finishes(tmp_path,monkeypatch):
    ws=tmp_path/"mismatch_workspace"
    ws.mkdir()
    loop,board,reply,_=_run_real_sidecar(
        ws,monkeypatch,expected_exists=False)
    assert len(loop.executed)==2
    assert loop.terminal_outcome!="SUCCESS"
    assert "GIT71_RECONCILIATION_OBSERVATION_UNVERIFIED" in reply
    stages=[x for x in board["events"] if x.get("kind")=="TIMEOUT_RECONCILIATION_COMPLETED"]
    assert not stages
