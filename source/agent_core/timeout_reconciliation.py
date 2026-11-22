"""Git71: bounded, provenance-checked recovery of a timed-out Runtime action.

An expected Timeout is not a completed task and never means a mutation
succeeded. A process-finality receipt may permit ONE predeclared, task-bound,
read-only observation. Only that observed postcondition can reconcile the
specific historical UNKNOWN action for Finish V2.

These functions are pure except for append_timeout_reconciliation: no
command replay, privilege elevation or undocumented ToolResult rewriting.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "SMARTAGENT_GIT71_TIMEOUT_RECONCILIATION_V1"
PROCESS_SCHEMA = "SMARTAGENT_GIT71_PROCESS_TERMINATION_V1"
_REF = re.compile(r"^MIN-EV-[0-9a-f]{24}$")
_ID = re.compile(r"^([A-Z]+-[A-Za-z0-9-]{8,128})$")
MAX_CHECKS = 8


class TimeoutReconciliationError(ValueError):
    pass


def _hash(x: object) -> str:
    return hashlib.sha256(json.dumps(
        x, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise TimeoutReconciliationError("RECONCILIATION_PATH_INVALID")
    # Must be an absolute Windows path on the target runtime. POSIX unit
    # tests also exercise this structure without authorizing any filesystem IO.
    path = ntpath.normpath(value)
    drive, tail = ntpath.splitdrive(path)
    if not drive or not tail.startswith("\\") or not re.fullmatch("[A-Za-z]:", drive):
        raise TimeoutReconciliationError("RECONCILIATION_ABSOLUTE_PATH_REQUIRED")
    if ".." in value.replace("/", "\\").split("\\"):
        raise TimeoutReconciliationError("RECONCILIATION_PARENT_TRAVERSAL")
    return ntpath.normcase(path)


def validate_reconciliation(action: object) -> dict:
    """Strictly bounded pre-dispatch check-set. Never grants read/write scope."""
    if not isinstance(action, Mapping) or action.get("tool") != "run_command":
        raise TimeoutReconciliationError("RECONCILIATION_WRONG_TOOL")
    spec = action.get("reconciliation")
    if not isinstance(spec, dict) or set(spec) != {"version", "checks"} or spec["version"] != 1:
        raise TimeoutReconciliationError("RECONCILIATION_SCHEMA_INVALID")
    rows = spec.get("checks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CHECKS:
        raise TimeoutReconciliationError("RECONCILIATION_CHECK_COUNT_INVALID")
    cwd = _path(action.get("working_directory"))
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"tool", "path", "expected_exists"}:
            raise TimeoutReconciliationError("RECONCILIATION_CHECK_SCHEMA_INVALID")
        if row["tool"] != "observe_fact" or type(row["expected_exists"]) is not bool:
            raise TimeoutReconciliationError("RECONCILIATION_CHECK_OPERATOR_INVALID")
        path = _path(row["path"])
        # Do not let the Planner make recovery observe arbitrary drive roots.
        if not path.startswith(cwd.rstrip("\\") + "\\"):
            raise TimeoutReconciliationError("RECONCILIATION_CHECK_OUTSIDE_CWD")
        if any(old["path"] == path for old in normalized):
            raise TimeoutReconciliationError("RECONCILIATION_CHECK_DUPLICATE")
        normalized.append({"tool":"observe_fact", "path":path,
                           "expected_exists":row["expected_exists"]})
    return {"version":1,"checks":normalized}


def reconciliation_digest(action: object) -> str:
    return _hash(validate_reconciliation(action))


def _process_finality(data: Mapping) -> tuple[bool, str]:
    term = data.get("timeout_termination")
    if not isinstance(term, Mapping) or term.get("schema") != PROCESS_SCHEMA:
        return False, "PROCESS_TERMINATION_RECEIPT_MISSING"
    if not (term.get("owned_handle_exit_verified") is True and
            term.get("owned_process_state") == "EXITED" and
            term.get("tree_termination_verdict") == "VERIFIED" and
            term.get("tree_termination_source") == "WIN32_TASKKILL_TREE" and
            type(term.get("tree_kill_exit_code")) is int and
            term["tree_kill_exit_code"] == 0 and
            term.get("final_identity_source") == "WIN32_WAIT_FOR_SINGLE_OBJECT" and
            type(term.get("pid")) is int and term["pid"] > 0 and
            data.get("pid") == term["pid"]):
        return False, "PROCESS_OR_TREE_FINALITY_NOT_VERIFIED"
    # This DOES NOT attest that arbitrary side effects never happened.
    return True, "PROCESS_AND_TREE_FINALITY_VERIFIED"


def begin_timeout_reconciliation(action: object, receipt: object,
                                 match: object, evidence_ref: str) -> dict:
    """Start only for exact predeclared Timeout, verified MIN-EV & OS proof."""
    base = {"schema":SCHEMA, "state":"BLOCKED", "reason":"RECONCILIATION_NOT_ELIGIBLE",
            "action_id":str(action.get("action_id","")) if isinstance(action,Mapping) else "",
            "source_evidence_ref":evidence_ref,"replay_original_action":False,
            "side_effect_scope":"DECLARED_PATHS_ONLY"}
    if not (_REF.fullmatch(str(evidence_ref)) and isinstance(action, Mapping)
            and isinstance(receipt, Mapping) and isinstance(match, Mapping)):
        return base
    if (receipt.get("status")!="UNKNOWN" or
            receipt.get("executed") is not True or
            match.get("match_state")!="MATCH" or
            match.get("observed_integrity")!="VERIFIED" or
            match.get("evidence_ref")!=evidence_ref or
            match.get("action_id")!=action.get("action_id")):
        base["reason"]="UNTRUSTED_OR_UNMATCHED_SOURCE"
        return base
    data=receipt.get("data")
    if not isinstance(data,Mapping) or data.get("timed_out") is not True or data.get("process_started") is not True:
        base["reason"]="NOT_AN_OBSERVED_STARTED_TIMEOUT"
        return base
    # Planner's expected must actually match a Timeout assertion, not just
    # some unrelated field like data.shell_mode.
    assertions=(action.get("expected") or {}).get("assertions", ())
    if not any(isinstance(a,Mapping) and a.get("field")=="data.timed_out"
               and a.get("operator")=="eq" and a.get("value") is True
               for a in assertions):
        base["reason"]="EXPECTED_TIMEOUT_NOT_PREDECLARED"
        return base
    try:
        checks=validate_reconciliation(action)
    except TimeoutReconciliationError as exc:
        base["reason"]=str(exc)
        return base
    final, reason=_process_finality(data)
    if not final:
        base["reason"]=reason
        return base
    return {**base,"state":"OBSERVATION_REQUIRED", "reason":reason,
            "reconciliation_sha256":_hash(checks),
            "checks":checks["checks"],
            "completed_check_paths":[],
            "process_finality_verified":True}


def check_reconciliation_observation(state: object, action: object,
                                     receipt: object, match: object,
                                     evidence_ref: str) -> dict:
    """Grant a specific declared path check from a *new* task-bound MIN-EV."""
    result={"schema":SCHEMA,"state":"BLOCKED","reason":"OBSERVATION_NOT_VERIFIED",
            "source_evidence_ref":state.get("source_evidence_ref") if isinstance(state,Mapping) else "",
            "verification_evidence_ref":evidence_ref,
            "replay_original_action":False}
    if not (isinstance(state,Mapping) and state.get("state")=="OBSERVATION_REQUIRED"
            and isinstance(action,Mapping) and isinstance(receipt,Mapping)
            and isinstance(match,Mapping) and _REF.fullmatch(str(evidence_ref))
            and evidence_ref!=state.get("source_evidence_ref")):
        return result
    if action.get("tool")!="observe_fact" or receipt.get("status")!="SUCCEEDED" or receipt.get("executed") is not True:
        result["reason"]="OBSERVATION_NOT_A_CONFIRMED_READ"
        return result
    if (match.get("match_state")!="MATCH" or match.get("observed_integrity")!="VERIFIED"
            or match.get("action_id")!=action.get("action_id")
            or match.get("evidence_ref")!=evidence_ref):
        result["reason"]="OBSERVATION_EXPECTED_MISMATCH"
        return result
    data=receipt.get("data")
    if not isinstance(data,Mapping) or data.get("observation_status")!="CONFIRMED":
        result["reason"]="OBSERVATION_VALUE_UNCONFIRMED"
        return result
    observations=data.get("observations")
    if not isinstance(observations,list) or len(observations)!=1 or not isinstance(observations[0],Mapping):
        result["reason"]="OBSERVATION_PROVENANCE_INVALID"
        return result
    fact=observations[0]
    if (fact.get("observation_status")!="CONFIRMED" or
            fact.get("source")!="filesystem.stat" or
            fact.get("field")!="file.exists" or
            fact.get("freshness")!="CURRENT"):
        result["reason"]="OBSERVATION_PRODUCER_INVALID"
        return result
    if receipt.get("resolved_path") is None or not isinstance(action.get("path"),str):
        result["reason"]="OBSERVATION_PATH_UNRESOLVED"
        return result
    try:
        observed_path=_path(action["path"])
        resolved_path=_path(receipt["resolved_path"])
    except TimeoutReconciliationError:
        result["reason"]="OBSERVATION_PATH_UNRESOLVED"
        return result
    if observed_path!=resolved_path:
        result["reason"]="OBSERVATION_PATH_CHANGED"
        return result
    try:
        if _path(fact.get("scope")) != observed_path:
            result["reason"]="OBSERVATION_SCOPE_CHANGED"
            return result
    except TimeoutReconciliationError:
        result["reason"]="OBSERVATION_SCOPE_CHANGED"
        return result
    check=next((c for c in state.get("checks",())
                if c.get("path")==observed_path and
                   c.get("path") not in state.get("completed_check_paths",())),None)
    if check is None or type(fact.get("value")) is not bool or fact["value"]!=check["expected_exists"]:
        result["reason"]="DECLARED_POSTCONDITION_NOT_MET"
        return result
    # The Planner had to predeclare and match the exact postcondition.
    specs=(action.get("expected") or {}).get("assertions",())
    if not any(isinstance(v,Mapping) and v.get("field")=="data.observations.0.value"
               and v.get("operator")=="eq" and v.get("value") is check["expected_exists"]
               for v in specs):
        result["reason"]="OBSERVATION_EXPECTED_NOT_PREDECLARED"
        return result
    completed=list(state.get("completed_check_paths",())) + [check["path"]]
    final=len(completed)==len(state["checks"])
    return {**result,"action_id":state["action_id"],
            "state":"RECONCILED" if final else "OBSERVATION_REQUIRED",
            "reason":"SCOPED_POSTCONDITIONS_VERIFIED" if final else "MORE_OBSERVATIONS_REQUIRED",
            "source_action_id":state["action_id"],
            "verification_action_id":action["action_id"],
            "reconciliation_sha256":state["reconciliation_sha256"],
            "checks":state["checks"],"completed_check_paths":completed,
            "process_finality_verified":True,
            "side_effect_scope":"DECLARED_PATHS_ONLY"}


def append_reconciliation_report(root: str | Path, task_id: str, request_id: str,
                                 state: Mapping) -> Path:
    if not (isinstance(task_id,str) and _ID.fullmatch(task_id) and
            isinstance(request_id,str) and _ID.fullmatch(request_id)):
        raise ValueError("INVALID_RECONCILIATION_IDENTITY")
    out=Path(root)/"localdata"/"runtime"/"timeout_reconciliation"/(task_id+".jsonl")
    out.parent.mkdir(parents=True,exist_ok=True)
    data={"task_id":task_id,"request_id":request_id, **state}
    with out.open("a",encoding="utf-8",newline="\n") as f:
        f.write(json.dumps(data,ensure_ascii=False,sort_keys=True)+"\n")
        f.flush()
        os.fsync(f.fileno())
    return out


def read_reconciliation_resolution(events: list, history: list[dict]) -> set[str]:
    """Verify a completed event with fresh source and successor ToolReceipts.

    A self-reported RECOVERED marker without the actual underlying matching
    MIN-EVs is not proof. Only a later, scoped, read-only observation resolves
    the exact source UNKNOWN for Finish V2.
    """
    lookup={h["action"]["action_id"]:h for h in history}
    by_ref={h["evidence_ref"]:h for h in history}
    completed=set()
    sources_seen=set()
    for index,event in enumerate(events):
        if event.get("kind")!="TIMEOUT_RECONCILIATION_COMPLETED":
            continue
        a_id=event.get("source_action_id")
        b_id=event.get("verification_action_id")
        a=lookup.get(a_id)
        b=lookup.get(b_id)
        if not a or not b or a_id in sources_seen:
            continue
        if a["evidence_ref"]!=event.get("source_evidence_ref") or b["evidence_ref"]!=event.get("verification_evidence_ref"):
            continue
        am=a.get("expected_match")
        bm=b.get("expected_match")
        initial=begin_timeout_reconciliation(a["action"],a["receipt"],am,a["evidence_ref"])
        if initial["state"]!="OBSERVATION_REQUIRED":
            continue
        if initial.get("reconciliation_sha256")!=event.get("reconciliation_sha256"):
            continue
        if a.get("sequence",0)>=b.get("sequence",0):
            continue
        # Complete all declared observations via separate later verified
        # actions, ordered in tool history and recorded in immutable receipts.
        stage=initial
        witness_actions=event.get("witness_action_ids")
        if not isinstance(witness_actions,list) or len(witness_actions)!=len(initial["checks"]):
            continue
        ok=True
        for witness in witness_actions:
            item=lookup.get(witness)
            if item is None or item.get("sequence",0)<=a.get("sequence",0):
                ok=False;break
            stage=check_reconciliation_observation(stage,item["action"],item["receipt"],
                                                  item.get("expected_match"),item["evidence_ref"])
            if stage.get("state") not in {"OBSERVATION_REQUIRED","RECONCILED"}:
                ok=False;break
        if ok and stage.get("state")=="RECONCILED" and event.get("verification_action_id")==witness_actions[-1]:
            completed.add(a["evidence_ref"])
            sources_seen.add(a_id)
    return completed
