"""Runtime-owned dynamic Project Sync and environment preflight.

A snapshot is a candidate and never a permission, immutable ToolResult,
or replacement for per-action execution Admission. Only admitted, observed
actions can advance project-index state.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path
from collections.abc import Mapping
from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_UNIFIED_PREFLIGHT_V1"
TASK_ID = re.compile(r"^TASK-[A-Za-z0-9_-]{5,90}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
MUTATING = frozenset({"create_directory","write_file","delete_path",
                      "apply_edit_plan","install_toolchain","web_edit_file"})
MUTATING_OPERATIONS = frozenset({"MUTATE","BUILD","GIT_MUTATE","TRANSFER"})
ENV_ERRORS = frozenset({"EXECUTABLE_NOT_FOUND","COMMAND_NOT_FOUND",
    "TOOLCHAIN_UNAVAILABLE","PYTHON_UNAVAILABLE","GIT_UNAVAILABLE",
    "WORKSPACE_NOT_FOUND","PATH_NOT_FOUND","MISSING_DEPENDENCY"})

def root_dir(install):
    return Path(install).expanduser().resolve() / "localdata" / "runtime" / "unified_preflight"

def _id(workspace):
    return hashlib.sha256(str(Path(workspace).resolve()).replace("\\","/").casefold().encode("utf-8")).hexdigest()[:32]

def _paths(install,workspace,task):
    if not TASK_ID.fullmatch(str(task)): raise ValueError("preflight_task_id_invalid")
    root=root_dir(install);wid=_id(workspace)
    return root,root/("WORKSPACE-"+wid+".json"),root/"tasks"/(task+".json"),root/("WORKSPACE-"+wid+".lock")

def _read(path):
    try:
        row=read_json_retry(path)
        return row if isinstance(row,dict) else {}
    except (ValueError, OSError, TypeError): return {}

def _signature(install):
    from .runtime_environment import runtime_tool_paths
    facts={}
    for name,path in sorted(runtime_tool_paths(install).items()):
        try:
            st=path.stat()
            facts[name]=(str(path),st.st_size,st.st_mtime_ns)
        except OSError:
            facts[name]=(str(path),"MISSING")
    head=Path(install)/".git"/"HEAD"
    try:
        entry=head.read_text(encoding="utf-8").strip()[:150]
        if entry.startswith("ref: "):
            candidate=Path(install)/".git"/entry[5:]
            entry+="|"+(candidate.read_text(encoding="utf-8").strip()[:64]
                        if candidate.is_file() else "UNRESOLVED")
        facts["source"]=entry
    except OSError:
        facts["source"]="NO_GIT_HEAD"
    # Fingerprint installed protocol material as well as bundled executables.
    # A packed Git ref or manual source update must not preserve stale hints.
    for relative in ("config/protocol_manifest.json","source/agent_core/unified_preflight.py"):
        candidate=Path(install)/relative
        try:
            st=candidate.stat()
            facts[relative]=(st.st_size,st.st_mtime_ns)
        except OSError:
            facts[relative]="MISSING"
    return hashlib.sha256(json.dumps(facts,sort_keys=True).encode("utf-8")).hexdigest()

def _pointer(workspace):
    from .path_security import reject_reparse_components, PathSecurityError
    root=Path(workspace)/".agents"/"project_context"
    try:
        reject_reparse_components(root/"current.json",Path(workspace))
    except (PathSecurityError,OSError,ValueError):
        return {"snapshot_id":"","status":"INVALID_REPARSE_PATH"}
    item=_read(root/"current.json")
    sid=str(item.get("snapshot_id") or "").lower()
    if not SHA.fullmatch(sid):return {"snapshot_id":"","status":"NOT_INDEXED"}
    try:
        if Path(str(item.get("workspace_root"))).resolve()!=Path(workspace).resolve():
            return {"snapshot_id":"","status":"INVALID_POINTER"}
        path=root/"snapshots"/(sid+".json")
        if not path.is_file() or path.is_symlink():
            return {"snapshot_id":"","status":"INVALID_POINTER"}
    except (OSError, ValueError):
        return {"snapshot_id":"","status":"INVALID_POINTER"}
    return {"snapshot_id":sid,"status":"CACHED_INDEX_HINT"}

def _effective(workspace):
    from .auto_approve_mode import auto_approve_snapshot
    row=auto_approve_snapshot(workspace,working_directory=workspace)
    return {k:row.get(k) for k in ("acl","turbo_requested","turbo_effective",
        "auto_approve_requested","auto_approve_effective","executor_backend",
        "policy_revision","reason_code")}

def _probe(install, probe=None):
    if probe is None:
        from .runtime_environment import planner_environment_snapshot
        probe=planner_environment_snapshot
    facts=probe(install)
    if not isinstance(facts,Mapping) or not isinstance(facts.get("tools"),Mapping):
        raise ValueError("preflight_probe_invalid")
    return dict(facts)

def _digest(row):
    basis={k:row.get(k) for k in ("workspace_root","revision",
          "environment_source","runtime_signature","effective_state","project_sync","scoped_projects")}
    return hashlib.sha256(json.dumps(basis,sort_keys=True,default=str).encode("utf-8")).hexdigest()

def _write(base,cache,taskfile,row):
    base.mkdir(parents=True,exist_ok=True)
    write_json_atomic(cache,{k:v for k,v in row.items() if k not in ("task_id","request_id")})
    write_json_atomic(taskfile,row)

def _event(base,row,kind,**facts):
    base.mkdir(parents=True,exist_ok=True)
    payload={"schema":SCHEMA,"event":kind,"task_id":row.get("task_id",""),
        "request_id":row.get("request_id",""),"preflight_id":row.get("preflight_id",""),
        "revision":row.get("revision",0),"workspace_id":_id(row["workspace_root"]),
        "at":time.time(),**facts}
    with exclusive_process_lock(base/"events.lock",label="unified-preflight-events"):
        with (base/"events.jsonl").open("a",encoding="utf-8") as out:
            out.write(json.dumps(payload,ensure_ascii=False,sort_keys=True)+"\n")

def _scoped_hints(workspace, previous):
    """Check existing exact Project Sync pointers, never crawl broad roots."""
    results={}
    for raw,prior in list(sorted((previous or {}).items()))[:8]:
        try:
            target=Path(raw).resolve()
            if (target==workspace or not target.is_relative_to(workspace)
                    or not target.is_dir() or target.is_symlink()):
                continue
            current=_pointer(target)
            if current["snapshot_id"]==(prior or {}).get("snapshot_id") and (
                    (prior or {}).get("status")=="STALE_REINDEX_REQUIRED"):
                current["status"]="STALE_REINDEX_REQUIRED"
            elif current["snapshot_id"]!=(prior or {}).get("snapshot_id"):
                current["status"]="INDEX_CHANGED_EXTERNALLY"
            results[str(target)]=current
        except (ValueError,OSError,TypeError):
            continue
    return results


def _ready(workspace,effective):
    return bool(
        workspace.is_dir()
        and effective.get("acl") in {"OFF","ON"}
        and effective.get("executor_backend") in {
            "WINDOWS_EXECUTOR","RESTRICTED_EXECUTOR","NATIVE_APPCONTAINER_BROKER"}
        and effective.get("reason_code") not in {
            "ACL_UNVERIFIED","POLICY_OR_SCOPE_UNVERIFIED","POLICY_UNVERIFIED",
            "WORKSPACE_NOT_AUTHORIZED_WRITABLE","WORKSPACE_NOT_BOUND"}
    )


def begin_task(install,workspace,*,task_id,request_id,probe=None):
    """Read a single combined baseline; reuse binary probes across tasks."""
    install=Path(install).resolve();workspace=Path(workspace).resolve()
    base,cache,taskfile,lock=_paths(install,workspace,task_id)
    base.mkdir(parents=True,exist_ok=True)
    with exclusive_process_lock(lock,label="unified-preflight"):
        old=_read(cache)
        signature=_signature(install)
        cached=(old.get("schema")==SCHEMA and
            old.get("workspace_root")==str(workspace) and
            old.get("runtime_signature")==signature and
            isinstance((old.get("runtime") or {}).get("tools"),dict) and
            not old.get("reprobe_required"))
        runtime=old["runtime"] if cached else _probe(install,probe)
        project=_pointer(workspace)
        old_project=old.get("project_sync") or {}
        if (old_project.get("snapshot_id")==project["snapshot_id"] and
                old_project.get("status")=="STALE_REINDEX_REQUIRED"):
            project["status"]="STALE_REINDEX_REQUIRED"
        elif old_project.get("snapshot_id")!=project["snapshot_id"] and old_project.get("snapshot_id"):
            project["status"]="INDEX_CHANGED_EXTERNALLY"
        scoped=_scoped_hints(workspace,old.get("scoped_projects"))
        effective=_effective(workspace)
        row={"schema":SCHEMA,"workspace_root":str(workspace),
            "task_id":task_id,"request_id":str(request_id),
            "runtime_signature":signature,"runtime":runtime,
            "environment_source":("IDENTITY_MATCHED_CACHE" if cached else "RUNTIME_PROBED"),
            "effective_state":effective,"project_sync":project,
            "scoped_projects":scoped,
            "revision":int(old.get("revision",0) or 0)+1,
            "reprobe_required":False,"last_action_id":"","last_evidence_ref":"",
            "updated_at":time.time(),"authorization_granted":False,
            "source_snapshot_is_current_byte_evidence":False,
            "status":("READY_FOR_ADMISSION" if _ready(workspace,effective)
                else "PRECHECK_UNVERIFIED")}
        row["preflight_id"]=_digest(row)
        _write(base,cache,taskfile,row)
        _event(base,row,"PREFLIGHT_TASK_STARTED",
            environment_source=row["environment_source"],
            preflight_status=row["status"],project_status=project["status"],
            project_snapshot_id=project["snapshot_id"],
            effective_acl=effective.get("acl"),
            effective_backend=effective.get("executor_backend"))
        return row

def task_fact(install,task_id):
    if not TASK_ID.fullmatch(str(task_id)):return {}
    return _read(root_dir(install)/"tasks"/(task_id+".json"))

def summary(row):
    effective=row.get("effective_state") or {}
    runtime=row.get("runtime") or {}
    tools=runtime.get("tools") or {}
    return {"schema":SCHEMA,"preflight_id":row.get("preflight_id",""),
        "revision":row.get("revision",0),
        "status":row.get("status","UNVERIFIED"),
        "environment_source":row.get("environment_source","UNVERIFIED"),
        "effective":dict(effective),"project_sync":dict(row.get("project_sync") or {}),
        "scoped_projects":dict(list((row.get("scoped_projects") or {}).items())[:8]),
        "tools":{k:{"status":v.get("status"),"path":v.get("path"),
            "version":str(v.get("version",""))[:100]}
            for k,v in tools.items() if isinstance(v,Mapping)},
        "authorization_granted":False,"source_snapshot_is_current_byte_evidence":False,
        "next":"NORMAL_ACTION_ADMISSION_ONLY"}

def inventory(install,workspace,row):
    from .project_environment import planner_environment_hint
    from .runtime_software_inventory import build_runtime_software_inventory
    from .software_registry import software_registry_snapshot
    inv=build_runtime_software_inventory(
        row.get("runtime"),planner_environment_hint(workspace),installation_root=install)
    if row.get("environment_source")=="IDENTITY_MATCHED_CACHE":
        for item in inv.get("bundled_tool_probes",[]):
            if item.get("availability")=="CURRENT_AVAILABLE":
                item["availability"]="CACHED_PROBE_RECHECK_ON_EXECUTION"
                item["execution_authorized"]=False
    scoped=[]
    for raw in list((row.get("scoped_projects") or {}))[:4]:
        try:
            hint=planner_environment_hint(raw)
            if hint.get("index_status")=="PRESENT":
                scoped.append({"project_root":raw,"candidate_index":hint})
        except (ValueError,OSError,RuntimeError,TypeError):
            continue
    inv["scoped_project_environment_indexes"]=scoped
    inv["external_software_registry"]=software_registry_snapshot()
    return inv

def action_observed(install,workspace,*,task_id,request_id,action,receipt,
                    match_report=None,evidence_ref="",probe=None):
    """Update state only after actual ToolResult; never replay side effects."""
    install=Path(install).resolve();workspace=Path(workspace).resolve()
    base,cache,taskfile,lock=_paths(install,workspace,task_id)
    with exclusive_process_lock(lock,label="unified-preflight"):
        row=_read(taskfile)
        if (row.get("schema")!=SCHEMA or
            row.get("request_id")!=request_id or
            row.get("workspace_root")!=str(workspace)):
            raise ValueError("preflight_action_not_bound_to_task")
        action_id=str(action.get("action_id",""))
        newest=_read(cache)
        if int(newest.get("revision",0) or 0)>int(row.get("revision",0) or 0):
            # Another task in this workspace may have updated a sibling index.
            common=dict(row.get("scoped_projects") or {})
            common.update(newest.get("scoped_projects") or {})
            row["scoped_projects"]=common
            if ((newest.get("project_sync") or {}).get("status")
                    =="STALE_REINDEX_REQUIRED" and
                (newest.get("project_sync") or {}).get("snapshot_id")
                    ==(row.get("project_sync") or {}).get("snapshot_id")):
                row["project_sync"]=dict(newest["project_sync"])
            row["revision"]=newest["revision"]
        if receipt.get("executed") is not True:
            _event(base,row,"PREFLIGHT_NONEXECUTED_ADMISSION",
                   action_id=action_id,reason_code=str(receipt.get("reason",""))[:80])
            return row
        tool=str(action.get("tool",""))
        operation=str(action.get("operation","")).upper()
        state=str(receipt.get("status",""))
        reason=str(receipt.get("reason",""))[:80]
        match=str((match_report or {}).get("match_state",""))
        drift=(match in {"MISMATCH","RECOLLECT_EVIDENCE"} or
               (state in {"FAILED","UNKNOWN","REJECTED"} and reason in ENV_ERRORS)
               or (tool=="install_toolchain" and state=="SUCCEEDED"))
        changed=(tool in MUTATING or
                 tool=="run_command" and operation in MUTATING_OPERATIONS)
        project=_pointer(workspace)
        previous=row.get("project_sync") or {}
        scoped=_scoped_hints(workspace,row.get("scoped_projects"))
        selected=workspace
        if tool in {"project_sync","query_project","inspect_project_scope"}:
            try:
                target=Path(str(action.get("project_root") or workspace)).resolve()
                if target.is_dir() and target.is_relative_to(workspace):
                    selected=target
            except (ValueError,OSError):
                pass
        if selected!=workspace and len(scoped)<8:
            scoped[str(selected)]=_pointer(selected)
        target_state=project if selected==workspace else scoped.get(str(selected),{})
        prior=previous if selected==workspace else (row.get("scoped_projects") or {}).get(str(selected),{})
        indexed=(tool=="project_sync" and
                 str(action.get("strategy") or "INDEX_ONLY").upper()=="INDEX_ONLY"
                 and state=="SUCCEEDED")
        new_id=(target_state.get("snapshot_id") and
                target_state["snapshot_id"]!=prior.get("snapshot_id"))
        if ((indexed or new_id and tool=="query_project") and
                target_state.get("snapshot_id")):
            target_state["status"]="INDEX_REFRESHED_BY_ADMITTED_ACTION"
        elif (prior.get("status")=="STALE_REINDEX_REQUIRED" and
              prior.get("snapshot_id")==target_state.get("snapshot_id")):
            target_state["status"]="STALE_REINDEX_REQUIRED"
        if changed or drift:
            try:
                touched=Path(str(action.get("working_directory") or
                                 action.get("path") or workspace)).resolve()
            except (OSError,ValueError):
                touched=workspace
            for key,old_project in [(str(workspace),previous),*list(
                    (row.get("scoped_projects") or {}).items())]:
                candidate=project if key==str(workspace) else scoped.get(key)
                project_root=Path(key)
                overlaps=(drift or touched==workspace or
                          touched.is_relative_to(project_root) or
                          project_root.is_relative_to(touched))
                if (overlaps and candidate and candidate.get("snapshot_id")
                        and candidate["snapshot_id"]==old_project.get("snapshot_id")):
                    candidate["status"]="STALE_REINDEX_REQUIRED"
        if drift:
            try:
                row["runtime"]=_probe(install,probe)
                row["environment_source"]="RUNTIME_REPROBED_AFTER_CONTRADICTION"
                row["reprobe_required"]=False
            except (ValueError,RuntimeError,OSError,TypeError) as err:
                row["reprobe_required"]=True
                row["environment_source"]="RUNTIME_REPROBE_FAILED"
                _event(base,row,"PREFLIGHT_RUNTIME_REPROBE_FAILED",
                       action_id=action_id,error_class=type(err).__name__)
        row["effective_state"]=_effective(workspace)
        row["project_sync"]=project
        row["scoped_projects"]=scoped
        row["last_action_id"]=action_id
        row["last_evidence_ref"]=str(evidence_ref)[:100]
        row["revision"]=int(row.get("revision",0))+1
        row["updated_at"]=time.time()
        row["status"]=("REPROBE_REQUIRED" if row.get("reprobe_required")
                       else "READY_FOR_ADMISSION" if _ready(workspace,row["effective_state"])
                       else "PRECHECK_UNVERIFIED")
        row["preflight_id"]=_digest(row)
        _write(base,cache,taskfile,row)
        _event(base,row,"PREFLIGHT_REPROBED_AFTER_CONTRADICTION" if drift
              else "PREFLIGHT_ACTION_OBSERVED",
              action_id=action_id,tool=tool,operation=operation,
              evidence_ref=str(evidence_ref)[:100],match_state=match,
              execution_status=state,project_status=project["status"],
              project_snapshot_id=project["snapshot_id"],
              scoped_project_count=len(scoped),
              environment_source=row["environment_source"])
        return row
