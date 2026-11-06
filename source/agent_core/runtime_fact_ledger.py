"""Request/epoch-bound provenance for real tool executions.

This module records the Planner proposal, the post-normalization admitted
Action, the exact dispatched Action, and the observed tool return separately.
A successful *observation* is never a successful *task postcondition*.
All Planner-facing projections are bounded; raw commands/output remain in the
Runtime-owned ledger and are not reflected into model context.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from collections.abc import Mapping

from .tool_result_contract import decode_result

SCHEMA = "SMARTAGENT_ACTION_PROVENANCE_V1"
FACT_SCHEMA = "SMARTAGENT_RUNTIME_FACT_V1"
PUBLIC_FIELDS = (
    "tool", "action_id", "path", "paths", "working_directory",
    "workspace", "project_root", "operation", "verifies_action_id",
)
MAX_PUBLIC_STRING = 1024


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(
        obj, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _public_action(action: Mapping | None) -> dict:
    if not isinstance(action, Mapping):
        return {}
    out = {}
    for key in PUBLIC_FIELDS:
        value = action.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:MAX_PUBLIC_STRING]
        elif key == "paths" and isinstance(value, list):
            out[key] = [str(x)[:MAX_PUBLIC_STRING] for x in value[:8]]
    if isinstance(action.get("command"), str):
        out["command_sha256"] = hashlib.sha256(
            action["command"].encode("utf-8")).hexdigest()
        out["command_redacted"] = True
    if "queries" in action:
        out["queries_sha256"] = _digest(action["queries"])
        queries = action.get("queries")
        out["query_count"] = len(queries) if isinstance(queries, list) else 1
    return out


def _fact_from_native_return(action: Mapping, result: object,
                             envelope: Mapping) -> dict | None:
    """Extract only mechanically supported facts; never interpret model prose."""
    if (envelope.get("executed") is not True
            or envelope.get("status") != "SUCCEEDED"):
        return None
    tool = str(action.get("tool", ""))
    reason = str(envelope.get("reason", "") or "")
    if tool != "inspect_directory":
        return None
    paths = action.get("paths")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        return None
    # The native producer has already distinguished completed inspection from
    # failed access. No absence fact is inferred from a denied list_directory.
    if reason == "DIRECTORY_ABSENCE_OBSERVED":
        value = False
    elif reason == "DIRECTORY_INSPECTED":
        try:
            body = json.loads(str(result))
        except (ValueError, TypeError):
            return None
        rows = body.get("paths") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        if type(rows[0].get("exists")) is not bool:
            return None
        value = rows[0]["exists"]
    else:
        return None
    return {
        "schema": FACT_SCHEMA,
        "predicate": "directory.exists",
        "value": value,
        "observation_status": "OBSERVED",
        "subject": paths[0][:MAX_PUBLIC_STRING],
        "source_tool": tool,
        "goal_verified": False,
    }


def build_action_provenance(original_action: Mapping,
                            canonical_action: Mapping,
                            dispatched_action: Mapping | None,
                            raw_result: object | None, *,
                            request_id: str, task_epoch: str,
                            round_id: int) -> dict:
    """Immutable snapshots. No result is invented for a rejected admission."""
    original = deepcopy(dict(original_action))
    canonical = deepcopy(dict(canonical_action))
    dispatched = deepcopy(dict(dispatched_action)) if dispatched_action is not None else None
    action_id = str(canonical.get("action_id", "") or "")
    if not action_id or not request_id or not task_epoch:
        raise ValueError("provenance_identity_missing")
    if (original.get("action_id") != action_id
            or (dispatched is not None and dispatched.get("action_id") != action_id)):
        raise ValueError("provenance_action_identity_mismatch")
    if dispatched is None:
        if raw_result is not None:
            raise ValueError("undispatched_action_cannot_have_tool_result")
        return {
            "schema": SCHEMA, "action_id": action_id,
            "request_id": request_id, "task_epoch": task_epoch, "round": round_id,
            "original_action": original, "canonical_action": canonical,
            "dispatched_action": None, "actual_result": None, "fact": None,
            "admission_status": "NOT_DISPATCHED",
        }
    envelope = decode_result(raw_result) or {}
    status = str(envelope.get("status", "UNKNOWN") or "UNKNOWN")
    executed = envelope.get("executed")
    if type(executed) is not bool:
        executed = None
    # A missing/malformed producer receipt remains UNKNOWN. Tool output text
    # is never promoted to an execution or verification success assertion.
    actual = {
        "status": status,
        "reason": str(envelope.get("reason", "RESULT_ENVELOPE_UNAVAILABLE"))[:120],
        "executed": executed,
        "raw_result_sha256": hashlib.sha256(
            str(raw_result).encode("utf-8")).hexdigest(),
    }
    fact = _fact_from_native_return(dispatched, raw_result, envelope)
    return {
        "schema": SCHEMA, "action_id": action_id,
        "request_id": request_id, "task_epoch": task_epoch, "round": round_id,
        "original_action": original,
        "canonical_action": canonical,
        "dispatched_action": dispatched,
        "actual_result": actual,
        "fact": fact,
        "admission_status": "DISPATCHED",
    }


def planner_projection(record: Mapping, *, request_id: str,
                       task_epoch: str) -> dict | None:
    """Bounded model view, scoped to this request/epoch, never raw stdout."""
    if (not isinstance(record, Mapping) or record.get("schema") != SCHEMA
            or record.get("request_id") != request_id
            or record.get("task_epoch") != task_epoch):
        return None
    result = record.get("actual_result")
    safe_result = None
    if isinstance(result, Mapping):
        safe_result = {
            "status": str(result.get("status", "UNKNOWN"))[:40],
            "reason": str(result.get("reason", ""))[:120],
            "executed": result.get("executed"),
            "raw_result_sha256": str(result.get("raw_result_sha256", ""))[:64],
        }
    fact = record.get("fact")
    safe_fact = None
    if isinstance(fact, Mapping) and fact.get("schema") == FACT_SCHEMA:
        safe_fact = {k: fact.get(k) for k in (
            "predicate", "value", "observation_status", "subject",
            "source_tool", "goal_verified",
        )}
    return {
        "action_id": str(record.get("action_id", ""))[:160],
        "original_action": _public_action(record.get("original_action")),
        "canonical_action": _public_action(record.get("canonical_action")),
        "dispatched_action": _public_action(record.get("dispatched_action"))
            if record.get("dispatched_action") is not None else None,
        "admission_status": record.get("admission_status"),
        "actual_result": safe_result,
        "fact": safe_fact,
        "complete_result_in_runtime_ledger": True,
    }


def decision_action_consistency(decision: str, operational: list,
                                *, runtime_transition: str) -> str:
    """No-action CONTINUE is not an executable strategy."""
    if str(decision).upper() != "CONTINUE":
        return "NOT_CONTINUE"
    if operational:
        return "ACTION_PRESENT"
    if runtime_transition == "WAIT_RUNTIME":
        return "RUNTIME_WAIT"
    return "ACTION_REQUIRED"
