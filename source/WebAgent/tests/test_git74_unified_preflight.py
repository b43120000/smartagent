"""Git74.1: cross-task P0 preflight, source invalidation and real GAR IT."""
from __future__ import annotations
import json
import os
import re
import subprocess
import uuid
import zipfile
from pathlib import Path
import pytest
from agent_core import unified_preflight as pf

def fake_mode(monkeypatch):
    monkeypatch.setattr(pf,"_effective",lambda w:{
      "acl":"OFF","turbo_requested":"OFF","turbo_effective":"OFF",
      "auto_approve_requested":"ON","auto_approve_effective":"ON",
      "executor_backend":"WINDOWS_EXECUTOR","policy_revision":3,
      "reason_code":"ACL_OFF_WORKSPACE_POLICY_OPT_IN"})

def fake_probe(history):
    def probe(root):
        history.append(str(root))
        return {"tools":{"python":{"status":"AVAILABLE","path":str(root/"python.exe"),
                 "version":"3.13","probe_exit_code":0}}}
    return probe

def action(tool="observe_fact",operation="INSPECT",name="ACT-1"):
    return {"tool":tool,"operation":operation,"action_id":name}

def receipt(status="SUCCEEDED",reason="OK",executed=True):
    return {"status":status,"reason":reason,"executed":executed,"data":{}}

def test_second_task_uses_cache_without_probing(tmp_path,monkeypatch):
    fake_mode(monkeypatch);app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    history=[]
    a=pf.begin_task(app,ws,task_id="TASK-ABC-12345",request_id="RR-A",probe=fake_probe(history))
    b=pf.begin_task(app,ws,task_id="TASK-ABC-99999",request_id="RR-B",probe=fake_probe(history))
    assert len(history)==1
    assert a["environment_source"]=="RUNTIME_PROBED"
    assert b["environment_source"]=="IDENTITY_MATCHED_CACHE"
    assert b["revision"]==a["revision"]+1
    assert pf.task_fact(app,"TASK-ABC-12345")["request_id"]=="RR-A"
    assert pf.summary(b)["authorization_granted"] is False

def test_tool_binary_drift_requires_probe(tmp_path,monkeypatch):
    fake_mode(monkeypatch);app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    history=[]
    pf.begin_task(app,ws,task_id="TASK-DRIFT-12345",request_id="RR-A",probe=fake_probe(history))
    from agent_core import runtime_environment
    exe=app/"python.exe"
    monkeypatch.setattr(runtime_environment,"runtime_tool_paths",lambda root:{"python":exe})
    exe.write_bytes(b"different")
    row=pf.begin_task(app,ws,task_id="TASK-DRIFT-99999",request_id="RR-B",
                      probe=fake_probe(history))
    assert len(history)==2
    assert row["environment_source"]=="RUNTIME_PROBED"

def test_mutation_stales_sync_until_admitted_reindex(tmp_path,monkeypatch):
    fake_mode(monkeypatch);app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    idx=ws/".agents"/"project_context";snap=idx/"snapshots";snap.mkdir(parents=True)
    a="a"*64;b="b"*64
    (snap/(a+".json")).write_text("{}");(snap/(b+".json")).write_text("{}")
    def point(sid):
        (idx/"current.json").write_text(json.dumps(
            {"snapshot_id":sid,"workspace_root":str(ws)}),encoding="utf-8")
    point(a)
    task="TASK-INDEX-12345";request="RR-INDEX"
    pf.begin_task(app,ws,task_id=task,request_id=request,probe=fake_probe([]))
    old=pf.action_observed(app,ws,task_id=task,request_id=request,
        action=action("run_command","BUILD"),receipt=receipt(),
        evidence_ref="MIN-EV-BUILD",probe=fake_probe([]))
    assert old["project_sync"]["status"]=="STALE_REINDEX_REQUIRED"
    new=pf.begin_task(app,ws,task_id="TASK-INDEX-99999",request_id="RR-N",probe=fake_probe([]))
    assert new["project_sync"]["status"]=="STALE_REINDEX_REQUIRED"
    point(b)
    refreshed=pf.action_observed(app,ws,task_id="TASK-INDEX-99999",request_id="RR-N",
        action=action("project_sync","INSPECT","ACT-SYNC"),receipt=receipt(),
        evidence_ref="MIN-EV-SYNC",probe=fake_probe([]))
    assert refreshed["project_sync"]=={
        "snapshot_id":b,"status":"INDEX_REFRESHED_BY_ADMITTED_ACTION"}

def test_expected_contradiction_reprobes_without_replaying(tmp_path,monkeypatch):
    fake_mode(monkeypatch);app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    history=[]
    task="TASK-MISMATCH-12345";request="RR-M"
    pf.begin_task(app,ws,task_id=task,request_id=request,probe=fake_probe(history))
    row=pf.action_observed(app,ws,task_id=task,request_id=request,
        action=action(),receipt=receipt(),match_report={"match_state":"MISMATCH"},
        evidence_ref="MIN-EV-IMMUTABLE",probe=fake_probe(history))
    assert len(history)==2
    assert row["last_evidence_ref"]=="MIN-EV-IMMUTABLE"
    assert row["environment_source"]=="RUNTIME_REPROBED_AFTER_CONTRADICTION"
    assert row["authorization_granted"] is False

def test_expected_negative_and_admission_denial_no_reprobe(tmp_path,monkeypatch):
    fake_mode(monkeypatch);app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    history=[]
    task="TASK-NEGATIVE-12345";request="RR-N"
    first=pf.begin_task(app,ws,task_id=task,request_id=request,probe=fake_probe(history))
    pf.action_observed(app,ws,task_id=task,request_id=request,
        action=action("run_command","TEST"),receipt=receipt("FAILED","EXIT_CODE_7"),
        match_report={"match_state":"MATCH"},probe=fake_probe(history))
    after=pf.action_observed(app,ws,task_id=task,request_id=request,
        action=action("run_command","BUILD"),receipt=receipt("REJECTED","SCOPE",False),
        probe=fake_probe(history))
    assert len(history)==1
    assert after["revision"]==first["revision"]+1

@pytest.mark.skipif(os.name!="nt",reason="Windows collector requires cmd.exe")
def test_real_collect_bat_contains_task_scoped_plaintext_evidence(tmp_path,monkeypatch):
    fake_mode(monkeypatch);root=Path(__file__).resolve().parents[3]
    tag=uuid.uuid4().hex[:12].upper();task="TASK-P0C-"+tag;req="RR-P0C-"+tag
    ws=tmp_path/"ws";ws.mkdir()
    path=pf.root_dir(root)/"tasks"/(task+".json");archives=[]
    try:
        pf.begin_task(root,ws,task_id=task,request_id=req,probe=fake_probe([]))
        pf.action_observed(root,ws,task_id=task,request_id=req,
          action=action(),receipt=receipt(),evidence_ref="MIN-EV-COLLECT",
          probe=fake_probe([]))
        done=subprocess.run(["cmd.exe","/d","/c",str(root/"collect.bat"),
              task,req],cwd=root,capture_output=True,text=True,
              errors="replace",timeout=180)
        assert done.returncode==0,done.stdout[-4000:]+done.stderr[-4000:]
        marker=re.search(r"^\[COLLECT\] ZIP:\s*(.+)$",done.stdout,flags=re.M)
        assert marker,done.stdout[-4000:]
        archive=Path(marker.group(1).strip());archives.append(archive)
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            state=json.loads(z.read("unified_preflight_task.json"))
            events=json.loads(z.read("unified_preflight_events.json"))
            assert state["task_id"]==task
            assert state["request_id"]==req
            assert state["last_evidence_ref"]=="MIN-EV-COLLECT"
            assert [e["event"] for e in events]==[
                 "PREFLIGHT_TASK_STARTED","PREFLIGHT_ACTION_OBSERVED"]
    finally:
        path.unlink(missing_ok=True)
        for archive in archives:archive.unlink(missing_ok=True)


def test_nested_indexes_reused_and_only_touched_project_invalidated(tmp_path,monkeypatch):
    fake_mode(monkeypatch)
    install=tmp_path/"app";install.mkdir();ws=tmp_path/"ws";ws.mkdir()
    p1=ws/"camera";p2=ws/"isp";p1.mkdir();p2.mkdir()
    for project,sha in ((p1,"a"*64),(p2,"b"*64)):
        root=project/".agents"/"project_context";snap=root/"snapshots"
        snap.mkdir(parents=True)
        (snap/(sha+".json")).write_text("{}",encoding="utf-8")
        (root/"current.json").write_text(json.dumps(
           {"snapshot_id":sha,"workspace_root":str(project)}),encoding="utf-8")
    task="TASK-SUBROOT-12345";req="RR-SUBROOT"
    pf.begin_task(install,ws,task_id=task,request_id=req,probe=fake_probe([]))
    for i,project in enumerate((p1,p2)):
        r=pf.action_observed(install,ws,task_id=task,request_id=req,
            action={"tool":"project_sync","strategy":"INDEX_ONLY",
                    "project_root":str(project),"action_id":f"ACT-INDEX-{i}"},
            receipt=receipt(),probe=fake_probe([]))
    assert set(r["scoped_projects"])=={str(p1),str(p2)}
    task2="TASK-SUBROOT-99999";req2="RR-SUBROOT-2"
    previous=pf.begin_task(install,ws,task_id=task2,request_id=req2,probe=fake_probe([]))
    assert previous["scoped_projects"][str(p1)]["snapshot_id"]=="a"*64
    result=pf.action_observed(install,ws,task_id=task2,request_id=req2,
        action={"tool":"run_command","operation":"BUILD",
                "working_directory":str(p1),"action_id":"ACT-CAMERA-BUILD"},
        receipt=receipt(),evidence_ref="MIN-EV-CAMERA",probe=fake_probe([]))
    assert result["scoped_projects"][str(p1)]["status"]=="STALE_REINDEX_REQUIRED"
    assert result["scoped_projects"][str(p2)]["status"]!="STALE_REINDEX_REQUIRED"
    result=pf.action_observed(install,ws,task_id=task2,request_id=req2,
        action={"tool":"query_project","project_root":str(p1),
                "action_id":"ACT-CAMERA-QUERY"},receipt=receipt(),
        probe=fake_probe([]))
    assert result["scoped_projects"][str(p1)]["status"]=="STALE_REINDEX_REQUIRED"


def test_unknown_effective_state_is_never_accepted_as_ready(tmp_path,monkeypatch):
    app=tmp_path/"app";app.mkdir();ws=tmp_path/"ws";ws.mkdir()
    monkeypatch.setattr(pf,"_effective",lambda work:{
        "acl":"UNKNOWN","executor_backend":"UNVERIFIED",
        "reason_code":"POLICY_UNVERIFIED"})
    row=pf.begin_task(app,ws,task_id="TASK-ACL-UNKNOWN",
                      request_id="RR-ACL",probe=fake_probe([]))
    assert row["status"]=="PRECHECK_UNVERIFIED"
    assert row["authorization_granted"] is False
