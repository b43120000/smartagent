"""Deterministic success/failure classification for stage action results."""
from __future__ import annotations
import json
from typing import Any

FAIL_MARKERS=("VERIFICATION_STATUS: FAIL","VERIFICATION_STATUS: UNVERIFIED","[PROTOCOL_ERROR]","[TOOL_ENVELOPE_","[WEB_DIRECT_EDIT_FAILED]","[WEB_DIRECT_EDIT_UNAVAILABLE]","[ARTIFACT_BUNDLE_FAILED]","[ARTIFACT_DOWNLOAD_FAILED]","[LOCAL_AI_FALLBACK_BLOCKED]")
FAIL_STATUSES={"FAIL","FAILED","ROLLED_BACK","WEB_DIRECT_EDIT_FAILED","WEB_DIRECT_EDIT_UNAVAILABLE","ARTIFACT_BUNDLE_FAILED","ARTIFACT_DOWNLOAD_FAILED","DELTA_NOT_AVAILABLE","INCOMPLETE"}

def _bounded(value: object, limit: int=1200) -> str:
    text=str(value or "").replace("\x00", "")
    return text[:limit]

def classify_tool_result(tool: str, result: Any) -> dict:
    """Return a durable status; command exit success alone is insufficient."""
    text=result if isinstance(result,str) else json.dumps(result,ensure_ascii=False,default=str)
    parsed=None
    try: parsed=json.loads(text) if isinstance(text,str) else result
    except Exception: pass
    status="COMMITTED"; reason=""
    if any(marker in text for marker in FAIL_MARKERS): status="FAILED"; reason="failure_marker"
    if isinstance(parsed,dict):
        observed=str(parsed.get("status", "") or "").upper()
        strict_success={"apply_edit_plan":"APPLIED","aggregate_verification":"PASS","project_sync":"READY","update_semantic_map":"UPDATED","update_semantic_map_file":"UPDATED","propose_task_plan":"PLAN_FROZEN","propose_task_plan_file":"PLAN_FROZEN","execute_frozen_plan":"PASS"}
        if tool in strict_success and observed != strict_success[tool]:
            status="FAILED"; reason=f"status:{observed or 'missing'}"
        if observed in FAIL_STATUSES or observed.endswith("_FAILED"):
            status="FAILED"; reason=f"status:{observed}"
        if tool=="aggregate_verification" and observed!="PASS": status="FAILED"; reason=f"aggregate:{observed or 'missing'}"
        if tool=="apply_edit_plan" and observed in {"ROLLED_BACK","FAILED"}: status="FAILED"; reason=f"edit:{observed}"
        # Common run_command return shape includes evidence in a JSON object.
        verification=str(parsed.get("verification_status", parsed.get("VERIFICATION_STATUS", "")) or "").upper()
        if verification in {"FAIL","UNVERIFIED"}: status="FAILED"; reason=f"verification:{verification}"
    return {"status":status,"reason":reason,"evidence":_bounded(text)}

def safe_stage_summary(tool: str, result: Any, status: str) -> dict:
    """Privacy-safe Stage3 evidence; never include text/path/command bytes."""
    parsed=result if isinstance(result,dict) else None
    if parsed is None:
        try: parsed=json.loads(str(result))
        except Exception: parsed={}
    parsed=parsed if isinstance(parsed,dict) else {}
    observed=str(parsed.get("status", "") or "").upper()
    verification=str(parsed.get("verification_status", parsed.get("VERIFICATION_STATUS", "")) or "").upper()
    reason_code="OK" if status=="COMMITTED" else "TOOL_FAILURE"
    if verification in {"FAIL","UNVERIFIED"}: reason_code="VERIFICATION_"+verification
    elif observed in {"ROLLED_BACK","FAILED"}: reason_code="EDIT_"+observed
    elif observed and status!="COMMITTED": reason_code="STATUS_"+observed[:48]
    rows=parsed.get("results", []); rows=rows if isinstance(rows,list) else []
    exit_codes=[item.get("exit_code") for item in rows if isinstance(item,dict) and type(item.get("exit_code")) is int][:16]
    timed_out=any(bool(item.get("timed_out")) for item in rows if isinstance(item,dict))
    safe={"tool":str(tool),"status":str(status),"reason_code":reason_code}
    if verification: safe["verification_status"]=verification
    if exit_codes: safe["exit_codes"]=exit_codes
    if rows: safe["timed_out"]=timed_out; safe["command_count"]=min(len(rows),16)
    if observed=="ROLLED_BACK": safe["rolled_back"]=True
    if tool=="apply_edit_plan" and type(parsed.get("applied_count")) is int: safe["applied_count"]=parsed["applied_count"]
    return safe
