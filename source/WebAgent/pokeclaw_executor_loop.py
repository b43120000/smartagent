"""Tool-agnostic PokeClaw Agent Loop for SmartAgent's Executor-only mode.

The model chooses a ToolCall, sequential ToolBatch or Finish each turn. The Runtime owns tool
admission, actual ToolResult, action history, side-effect replay protection,
and per-turn Shared Blackboard persistence. No Goal Driver, Prerequisite
planner, task-specific sequence grammar or speculative whole-goal PASS.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from collections.abc import Mapping

from agent_core.executor_run_blackboard import (
    _safe_arguments, begin_executor_run, append_executor_event,
    finish_executor_run, record_finish_comparison, projection, read_executor_run,
)
from agent_core.task_progress import set_runtime_state
from agent_core import path_integrity_trace as PIT
from agent_core.tool_result_contract import decode_result
from agent_core.runtime_finish_comparator import (
    compare_runtime_finish, finish_input_projection,
)
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement
from agent_core.finish_contract_adapter_v2 import adapt_finish_contract_v2

from .minimal_admission import admit_minimal_action, action_is_mutating
from .minimal_decision import MinimalDecisionError, parse_minimal_decision_with_diagnostics
from .github_stage_diagnostics import github_source_stage_evidence
from agent_core.goal75_goal_coverage import original_goal_stage_ids
from agent_core.goal75_recovery import repair_single_decision
from dataclasses import replace


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()


def _possibly_mutating(action: dict, broker_decision: str) -> bool:
    if action_is_mutating(action):
        return True
    if broker_decision != "BROKER_REQUIRED" or action.get("tool") != "run_command":
        return False
    # Keep native commands conservative, including external executables.
    # Relax replay only for already-proven direct PowerShell file reads.
    return not re.match(
        r"^(?:Get-Content|Get-Item|Test-Path)\s+-LiteralPath\s",
        str(action.get("command", "")).strip(), re.IGNORECASE,
    )


def _tool_result(status: str, reason: str, payload: object = None, *, evidence_pointer: dict | None = None) -> dict:
    """Exactly PokeClaw's three tool-facing fields; semantics are action-only."""
    if status == "SUCCEEDED":
        text = json.dumps(
            {"reason": reason, "result": payload if payload is not None else {}},
            ensure_ascii=False, default=str, separators=(",", ":"),
        )
        # A preview is only a transport projection. Every original byte in
        # the Runtime's captured ToolResult must remain addressable by ID.
        if len(text) > 6000:
            stored = bool(evidence_pointer and evidence_pointer.get("store_status")
                          == "PERSISTED_AND_READBACK_VERIFIED")
            text = json.dumps({
                "reason": reason, "data_preview": text[:4400],
                "truncated": True,
                "complete_result_available_in_blackboard": False,
                "complete_result_available_in_evidence_store": stored,
                "content_ref": (evidence_pointer or {}).get("evidence_ref") if stored else None,
                "read_tool": "read_runtime_evidence" if stored else None,
                "evidence_delivery_status": ("POINTER_AVAILABLE" if stored else "PERSIST_FAILED"),
            }, ensure_ascii=False, separators=(",", ":"))
        return {"isSuccess": True, "data": text, "error": None}
    details = ""
    if isinstance(payload, Mapping):
        process = {
            key: payload[key] for key in (
                "process_state", "exit_code", "timed_out",
                "verification_status", "expectation_status",
            )
            if key in payload
        }
        if process:
            details = "; " + json.dumps(
                process, ensure_ascii=False, separators=(",", ":"),
            )[:600]
        # Runtime-owned rejection causes must reach Planner, not disappear
        # behind the generic COMMAND_REJECTED envelope.
        diagnostic = {
            key: str(payload[key])[:400 if key == "command_error" else 64]
            for key in ("command_rejection_stage", "command_error")
            if payload.get(key)
        }
        if diagnostic:
            details += "; " + json.dumps(
                diagnostic, ensure_ascii=False, separators=(",", ":"),
            )[:520]
    # Failed commands also have stdout/stderr and must remain diagnosable.
    if isinstance(payload, Mapping):
        streams = {
            key: payload[key] for key in (
                "stdout", "stderr", "exit_code", "native_error_class",
                "stdout_bytes", "stderr_bytes", "output_complete",
            ) if key in payload
        }
        if streams:
            details += "; streams=" + json.dumps(streams, ensure_ascii=False, default=str)[:1600]
    if evidence_pointer and evidence_pointer.get("evidence_ref"):
        details += "; evidence_ref=" + str(evidence_pointer["evidence_ref"])
        if evidence_pointer.get("store_status") == "PERSISTED_AND_READBACK_VERIFIED":
            details += "; read_tool=read_runtime_evidence"
        else:
            details += "; evidence_delivery_status=PERSIST_FAILED"
    return {
        "isSuccess": False, "data": None,
        "error": (
            ("EXECUTION_OUTCOME_UNCERTAIN:" if status in {"UNKNOWN", "RUNNING", "PARTIAL"}
             else "ACTION_NOT_EXECUTED:" if status == "REJECTED"
             else "TOOL_FAILED:")
            + str(reason or "UNCLASSIFIED_RESULT")[:240] + details
        ),
    }


def _blocking_runtime_evidence(loop, board=None) -> dict:
    """Choose the latest relevant Runtime failure or *unexecuted* admission denial.

    An Admission Rejection is a Blackboard event, not a ToolResult or MIN-EV.
    Never turn its proposed command into an executed failure.
    """
    if isinstance(board, Mapping):
        for event in reversed(board.get("events") or []):
            if not isinstance(event, Mapping):
                continue
            if event.get("kind") == "ADMISSION_REJECTED":
                diagnostic = event.get("admission_diagnostic") or {}
                runtime = event.get("runtimeEvidence") or {}
                return {
                    "status": "OBSERVED_ADMISSION_REJECTION",
                    "evidence_ref": "",
                    "blackboard_sequence": event.get("sequence"),
                    "tool": event.get("toolName"),
                    "execution_status": "REJECTED",
                    "reason_code": str(runtime.get("reason_code") or
                                       diagnostic.get("reason_code") or "")[:160],
                    "admission_stage": str(diagnostic.get("stage") or "")[:80],
                    "executed": False,
                    "authorization_granted": False,
                    "exit_code": None,
                }
            if event.get("kind") == "TOOL_RESULT":
                ev = event.get("runtimeEvidence") or {}
                if ev.get("status") in {"FAILED", "REJECTED", "UNKNOWN", "PARTIAL"}:
                    return {
                        "status": "OBSERVED_RUNTIME_BLOCKER",
                        "evidence_ref": ev.get("evidence_ref") or "",
                        "blackboard_sequence": event.get("sequence"),
                        "tool": event.get("toolName"),
                        "execution_status": ev.get("status"),
                        "reason_code": str(ev.get("reason_code") or "")[:160],
                        "exit_code": (
                            (ev.get("process") or {}).get("exit_code")
                            if type((ev.get("process") or {}).get("exit_code")) is int
                            else None
                        ),
                    }
    ledger = getattr(loop, "action_result_ledger", None) or {}
    if not isinstance(ledger, Mapping):
        return {"status": "NO_RUNTIME_BLOCKER"}
    rows = sorted(
        (x for x in ledger.values() if isinstance(x, Mapping)),
        key=lambda x: int(x.get("produced_round") or 0), reverse=True,
    )
    for row in rows:
        if row.get("result_status") not in {"FAILED", "REJECTED", "UNKNOWN", "PARTIAL"}:
            continue
        receipt = decode_result(row.get("result")) or {}
        data = receipt.get("data") if isinstance(receipt.get("data"), Mapping) else {}
        return {
            "status": "OBSERVED_RUNTIME_BLOCKER",
            "evidence_ref": row.get("evidence_ref") or row.get("result_id"),
            "tool": row.get("tool"),
            "execution_status": row.get("result_status"),
            "reason_code": str(row.get("result_reason") or "")[:160],
            "exit_code": data.get("exit_code") if type(data.get("exit_code")) is int else None,
        }
    return {"status": "NO_RUNTIME_BLOCKER"}


def _lean_external_blocker(board: Mapping) -> dict | None:
    """Recent Runtime-origin authority/resource denials, not schema errors."""
    authority = {
        "ACCESS_DENIED", "PERMISSION_DENIED", "PATH_NOT_AUTHORIZED",
        "SCOPE_ACCESS_DENIED", "USER_APPROVAL_DENIED", "TURBO_OS_BROKER_BLOCKED",
        "REQUIRED_SOURCE_INACCESSIBLE", "REQUIRED_WEBSITE_UNAVAILABLE",
    }
    for row in reversed(board.get("events") or []):
        kind = row.get("kind")
        if kind in {"TURBO_SANDBOX_BLOCKED", "TURBO_NATIVE_CONFIRMATION_BLOCKED"}:
            if kind == "TURBO_NATIVE_CONFIRMATION_BLOCKED" and row.get("reason_code") not in {
                    "TURBO_NATIVE_BROKER_CWD_OUTSIDE_ROOT",
                    "TURBO_NATIVE_BROKER_CWD_UNAVAILABLE",
                    "TURBO_NATIVE_BROKER_POLICY_NOT_ADMITTED",
                    "TURBO_NATIVE_BROKER_STATE_MISSING",
                    "TURBO_NATIVE_BROKER_POLICY_REVISION_UNVERIFIED",
                    "TURBO_NATIVE_BROKER_SCOPE_UNVERIFIED"}:
                return None
            if row.get("executed") is False and row.get("authorization_granted") is False:
                return {"kind": kind, "sequence": row.get("sequence"),
                        "reason_code": row.get("reason_code")}
            return None
        if kind in {"TOOL_RESULT", "ADMISSION_REJECTED"}:
            ev = row.get("runtimeEvidence") or {}
            reason = str(ev.get("reason_code") or "")
            if reason.upper().removeprefix("SCOPE_REJECTED:") in authority:
                if kind == "ADMISSION_REJECTED" and (
                        (row.get("admission_diagnostic") or {}).get("stage") != "SCOPE_PREFLIGHT"):
                    return None
                if ev.get("status") in {"REJECTED", "FAILED"}:
                    return {"kind": kind, "sequence": row.get("sequence"),
                            "evidence_ref": ev.get("evidence_ref"),
                            "reason_code": reason}
            return None
    return None


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _safe_projection_key(key) -> bool:
    return isinstance(key, str) and not re.search(
        r"password|secret|token|api.?key|authorization|credential", key, re.I)


def _priority_key(key: str) -> tuple[int, str]:
    # Preserve business identifiers and verdicts ahead of verbose diagnostics.
    if re.search(r"(^|\.)(?:stdout|stderr|content|text|detail)$", key, re.I):
        return (3, key)
    if re.search(r"snapshot|context_ref|revision|event_count|sync_completed|changed_files|"
                 r"exit_code|exists|value|file_sha256|event_id|mtime|size|fixture|target_luma",
                 key, re.I):
        return (0, key)
    if re.search(r"status|reason|sha256|count|path|error|verification|state|event_type",
                 key, re.I):
        return (1, key)
    return (2, key)


def _scalar_projection(value, *, prefix="", budget=2800, limit=72,
                       string_limit=640) -> dict:
    """Bounded exact scalar facts, with explicit omissions; never invent fields.

    Breadth-first traversal keeps later sibling fields visible when one branch
    contains a huge payload. String previews are not exact scalar witnesses.
    """
    from collections import deque
    queue = deque([(prefix, value, 0)])
    fields = {}
    previews = {}
    omitted = False
    used = 0
    candidates = []
    scanned = 0
    while queue and scanned < 4096:
        scanned += 1
        path, item, depth = queue.popleft()
        if depth > 9:
            omitted = True
            continue
        if isinstance(item, Mapping):
            keys = [k for k in item if _safe_projection_key(k)]
            omitted |= len(keys) != len(item)
            for key in sorted(keys, key=_priority_key):
                queue.append((f"{path}.{key}" if path else key, item[key], depth+1))
            if not item and path:
                # No invented scalar for an empty object.
                continue
        elif isinstance(item, list):
            if len(item) > 16:
                omitted = True
            for i, child in enumerate(item[:16]):
                queue.append((f"{path}.{i}", child, depth+1))
        elif type(item) in (str, int, float, bool, type(None)):
            candidates.append((path, item))
        else:
            omitted = True
    for path, item in sorted(candidates, key=lambda pair: (
            isinstance(pair[1], str) and len(pair[1]) > string_limit,
            _priority_key(pair[0]))):
        is_preview = isinstance(item, str) and len(item) > string_limit
        val = item[:string_limit] if is_preview else item
        charge = len(_json_compact({path: val})) + 1
        if used + charge > budget or len(fields) + len(previews) >= limit:
            omitted = True
            continue
        used += charge
        if is_preview:
            previews[path] = val
            omitted = True
        else:
            fields[path] = val
    return {"facts": fields, "string_previews": previews,
            "omitted": bool(omitted or queue)}


def _planner_projection(receipt, raw_result, pointer):
    # The decoded Runtime receipt is authoritative. Business JSON is an
    # observation of the original tool output, NOT a new Finish field.
    persisted = pointer.get("store_status") == "PERSISTED_AND_READBACK_VERIFIED"
    # Keep the explicit evidence slice losslessly. A second truncation
    # would make offset/next_offset misleading and force additional calls.
    body = receipt.get("data")
    is_slice = (receipt.get("tool") == "read_runtime_evidence"
                and isinstance(body, Mapping)
                and isinstance(body.get("content"), str))
    projected_receipt = receipt
    if is_slice:
        projected_receipt = {**receipt, "data": {
            k: v for k, v in body.items() if k != "content"}}
    result = {"receipt": _scalar_projection(projected_receipt, budget=5200, limit=112,
                                            string_limit=2400),
              "evidence_ref": pointer.get("evidence_ref"),
              "store_status": pointer.get("store_status")}
    if is_slice:
        result["receipt"]["facts"]["data.content"] = body["content"]
    try:
        raw = json.loads(re.sub(r"^\\[(?:TOOL_RESULT|CHUNKED_WRITE_(?:SUCCESS|FAILED)|"
                                r"SECURITY_DELETE_(?:SUCCESS|FAILED|REJECTED))\\]\\s*",
                                "", str(raw_result).strip()))
    except (ValueError, TypeError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("_runtime_result"), dict):
        business = {k: v for k, v in raw.items() if k != "_runtime_result"}
        result["business_output"] = _scalar_projection(business, budget=2400,
                                                       string_limit=1600)
        known = result["receipt"]["facts"]
        result["business_output"]["facts"] = {
            k: v for k, v in result["business_output"]["facts"].items()
            if "data.project_facts."+k not in known
            or known["data.project_facts."+k] != v}
        result["business_output_note"] = "raw tool output; not a receipt field or Finish witness"
    if persisted:
        result["read_tool"] = "read_runtime_evidence"
    return result


def _compact_recent(recent):
    """Keep the latest round and a bounded index of prior verified receipts.

    Use actual scalar values instead of repeating six verbose results plus
    the old field-name-only catalog. Omitted facts remain in immutable MIN-EV.
    """
    newest_round = recent[-1].get("round") if recent else None
    latest = [row for row in recent if row.get("round") == newest_round]
    previous = [row for row in recent if row.get("round") != newest_round]
    older = []
    used = 0
    audit_tools = {"read_runtime_evidence", "read_runtime_blackboard"}
    for row in sorted(reversed(previous), key=lambda r: r.get("tool") in audit_tools):
        audit = row.get("tool") in audit_tools
        p = row.get("planner_evidence") or {}
        facts = (p.get("receipt") or {}).get("facts") or {}
        card = {"round": row.get("round"), "tool": row.get("tool"),
                "action_id": row.get("action_id"), "evidence_ref": row.get("evidence_ref"),
                "store_status": row.get("evidence_store_status"),
                "facts": {}}
        if not card["evidence_ref"]:
            continue
        # Keep short arbitrary scalar values as well as common project facts.
        # No task-specific fields are prerequisites or new Finish contracts.
        for key, value in sorted(facts.items(), key=lambda item: _priority_key(item[0])):
            if not (key in {"status", "reason", "executed"} or key.startswith("data.")):
                continue
            if isinstance(value, str) and len(value) > (160 if audit else 1200):
                continue
            trial = {**card["facts"], key: value}
            if len(_json_compact(trial)) <= (300 if audit else 1600):
                card["facts"] = trial
        card["omitted"] = len(card["facts"]) < len(facts) or bool(
            (p.get("receipt") or {}).get("omitted"))
        size = len(_json_compact(card))
        if used + size > 16000:
            continue
        older.append(card)
        used += size
    return {"recent": [{k: v for k, v in row.items() if k != "toolResult"}
                       if row.get("planner_evidence") else row for row in latest],
            "older_evidence": sorted(older, key=lambda row: row["round"] or 0),
            "older_evidence_index_omitted": len(older) < sum(
                bool(row.get("evidence_ref")) for row in previous)}


def _compact_tool_catalog(tools):
    from .minimal_core_loop import tool_catalog
    # Required/optional names are already keys in the corresponding typed
    # schema maps. Keep descriptions/enums/verify contracts without duplicates.
    return [{k: v for k, v in row.items()
             if k not in {"required", "optional"} and v not in ({}, [], "")}
            for row in tool_catalog(tools)]


def _prompt(request: str, tools, recent: list[dict], *, round_id: int,
            max_rounds: int, hint: str = "", field_catalog=None,
            evidence_requirement=None, goal_state_contract=None,
            environment_inventory=None, unified_preflight=None,
            runtime_status_catalog=None, turbo_planning_context=None,
            preexecution_rejections=None, stage_context="") -> str:
    return (
        "[POKECLAW_EXECUTOR_LOOP_V1]\n"
        "You are the Goal-driven Planner. Use real tools to complete the original "
        "request. Return exactly ONE JSON decision.\n"
        'Action: {"type":"tool_call","name":"observe_fact","arguments":{"path":"C:\\\\example"}}\n'
        'Batch: {"type":"tool_batch","actions":[{"type":"tool_call","name":"observe_fact","arguments":{"path":"C:\\\\example"}},{"type":"tool_call","name":"read_file","arguments":{"path":"C:\\\\readme.txt"}}]}\n'
        "Prefer batches of 2-6 ready actions with literal arguments; execution is sequential, "
        "each item independently admitted and recorded. No result placeholders, nested batches, "
        "Finish or Blocked inside batches. Stop on failure/rejection/uncertain effects; "
        "unexecuted tail is not evidence. Replan only unfinished work. Use one action "
        "for decisions that require inspecting earlier results. Reconciliation stays single-action.\n"
        'Finish: {"type":"finish","contract_version":2,"summary":"actual result","goal_checks":[{"criterion":"original requirement (include its P0/Case A label if any)","evidence_ref":"actual MIN-EV","field":"data.actual_field","operator":"eq","expected":"actual required value"}]}\n'
        'Blocked: {"type":"blocked","reason":"specific external constraint, exhausted lawful alternatives and concrete user action needed"}\n'
        "No mandatory StagePlan or StageCommit. Revise the plan freely within "
        "original constraints; preserve completed work. A failed attempt, schema "
        "error, rejection or missing evidence means replan, not terminate. "
        "Blocked requires actual Runtime permission/resource evidence. "
        "Do not invent facts or claim success without evidence.\n"
        "Security: every action is independently admitted. Never bypass ACL, "
        "approval, native broker, scope, or user prohibitions; no Host fallback "
        "when native Turbo is required. Never disguise mutation as read-only. "
        "Non-command observations and web_search do not need a command sandbox.\n"
        "Observe actual state; do not guess Expected on exploratory reads. "
        "Expected is optional except when needed for explicit negative tests "
        "or timeout reconciliation. Mutations still need the tool's required "
        "verification/postcondition. Do not replay uncertain mutations. "
        "For pinned source deployments verify remote commit, tree/blob and local "
        "archive hash before deletion or deployment.\n"
        "Intentional Timeout test: BOUNDED, freeze expected timed_out, process_started "
        "and exit_code is_null, and reconciliation={version:1,checks:[{tool:observe_fact,"
        "path:<absolute scoped path below working_directory>,expected_exists:true}]}. "
        "After timeout perform only frozen readonly checks; no replay. "
        "Ordinary scripts/commands default to UNTIL_EXIT in every ACL/Turbo mode, "
        "without wait confirmation. BOUNDED only for explicit timeout requirements. "
        "Waiting never grants execution permission; native Turbo stays native. "
        "Process exit does not prove detached child completion.\n"
        "For local Project Sync use INDEX_ONLY. Verify actual current bytes. "
        "Use read_runtime_evidence for truncated or older MIN-EV "
        "(raw_result/receipt/stdout/stderr/metadata); do not rerun to obtain saved output. "
        "For older events use read_runtime_blackboard.\n"
        "For compiled goals Runtime checks original conditions automatically. For "
        "uncompiled goals provide typed goal_checks covering every essential "
        "requirement, including each original explicitly numbered phase. A repeated "
        "fact cannot substitute for separate requirements. Use exact receipt facts, not raw business previews. "
        "Finish summary MUST be a nonempty string, max 3000 characters; aim below 2500. "
        "Keep essential results and requested metrics; avoid repeating evidence already in goal_checks. "
        "No need to restate all "
        "Runtime records or send expected_runtime. Pure text may finish with "
        "summary alone only when answer-only eligible.\n"
        f"Working directory (not an authorization grant): {tools.workspace_root}\n"
        "Original user request:\n" + request + "\n"
        "Original phase labels (coverage, not execution order): "
        + json.dumps(original_goal_stage_ids(request), ensure_ascii=False) + "\n"
        "Tool catalog (typed required_types/optional_types): " + _json_compact(_compact_tool_catalog(tools)) + "\n"
        "Goal conditions: " + json.dumps(goal_state_contract or {}, ensure_ascii=False,
                                          separators=(",", ":"))[:5500] + "\n"
        "Evidence requirement: " + json.dumps(evidence_requirement or {},
                                                ensure_ascii=False) + "\n"
        "Turbo: " + json.dumps(turbo_planning_context or {}, ensure_ascii=False)[:1600] + "\n"
        "Environment (advisory): " + json.dumps(environment_inventory or {},
                                                 ensure_ascii=False)[:3000] + "\n"
        "Observed results (exact receipt facts; business output and previews are not Finish fields): "
        + _json_compact(_compact_recent(recent)) + "\n"
        "Use read_runtime_evidence only if a needed exact receipt field/value is omitted. "
        "Each omitted or preview field is not proven by the projection.\n"
        "Recent native denials: " + json.dumps(preexecution_rejections or [],
                                               ensure_ascii=False)[-1500:] + "\n"
        f"Round {round_id}/{max_rounds}\n"
        + (f"Runtime feedback: {hint}\n" if hint else "")
        + "[/POKECLAW_EXECUTOR_LOOP_V1]"
    )


def _terminal_receipt(loop, summary: str, board: Mapping,
                      comparison: Mapping) -> dict:
    """Runtime-owned terminal: PASS or FAIL, never model-authorized acceptance."""
    from agent_core.tri_one_runtime_terminal import TERMINAL_SCHEMA
    verdict = comparison["verdict"]
    payload = {
        "schema": TERMINAL_SCHEMA, "admission_status": "ACCEPTED",
        "decision": "RUNTIME_FINISH_COMPARATOR_V1",
        "completion_kind": comparison.get("completion_kind", "EVIDENCE_BACKED"),
        "request_id": loop.run_id, "task_id": loop.task_id,
        "task_epoch": loop.task_epoch, "round": loop.turn_id,
        "task_outcome": "SUCCESS" if verdict == "PASS" else "FAILED",
        "runtime_state": "COMPLETED",
        "evidence_refs": list(comparison["evidence_refs"]),
        "evidence_verdict": verdict, "verification_status": verdict,
        "final_verdict": verdict,
        "goal_evidence_reason": comparison["goal_evidence_reason"],
        "matched_condition_id": comparison["goal_kind"],
        **({
            "admission_reason": "ANSWER_ONLY_RESPONSE_ADMITTED",
            "answer_sha256": comparison["answer_sha256"],
            "goal_sha256": hashlib.sha256(
                str(getattr(loop, "task_request", "") or "").encode("utf-8")
            ).hexdigest(),
        } if comparison.get("completion_kind") == "ANSWER_ONLY"
             and comparison["verdict"] == "PASS" else {}),
        "goal_coverage_basis": comparison.get("coverage_basis", "NOT_VERIFIED"),
        "comparison": {
            "reason_code": comparison["reason_code"],
            "failure_origin": comparison.get("failure_origin", ""),
            "mismatches": list(comparison["mismatches"]),
            "tool_results": list(comparison["tool_results"]),
            **{
                key: board["terminal"]["finish_comparison"][key]
                for key in ("unconfirmed_evidence", "reconciled_observations")
                if key in board["terminal"]["finish_comparison"]
            },
        },
        "handoff_id": board["handoff_id"],
        "blackboard_event_count": len(board["events"]),
        "goal_contract_sha256": comparison.get("goal_contract_sha256", ""),
        "goal_state_table": list(comparison.get("goal_state_table") or [])[:32],
        "model_goal_checks_advisory": comparison.get("model_goal_checks_advisory"),
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    payload["receipt_id"] = "TERM-" + _digest(payload)[:20].upper()
    return payload


def _finish(loop, root: Path, request: str, *, kind: str,
            summary: str, complete: bool, expected_runtime=None,
            goal_checks=None, canonical_finish=None, comparison=None) -> str:
    # Only the decision loop's verified open-ledger verdict may close success.
    if complete and (not isinstance(comparison, Mapping)
                     or comparison.get("verdict") != "PASS"):
        raise ValueError("lean_finish_requires_verified_open_ledger")
    board = finish_executor_run(
        root, loop, terminal_kind=kind, summary=summary,
        reported_outcome="UNKNOWN",
    )
    if complete:
        if (board["terminal"]["tool_evidence_refs"] != comparison["evidence_refs"]
                or board["terminal"]["summary_sha256"] != hashlib.sha256(
                    summary.encode("utf-8")).hexdigest()):
            raise ValueError("lean_finish_evidence_changed_during_commit")
        board = record_finish_comparison(root, loop, comparison)
    loop.executor_blackboard_terminal = board
    blocking_evidence = _blocking_runtime_evidence(loop, board) if not complete else None
    loop.executor_result = {
        "schema": "SMARTAGENT_POKECLAW_EXECUTOR_RESULT_V1",
        "outcome": kind, "executor_state": board["executor_state"],
        "terminal_status": kind,
        "goal_verified": bool(comparison and comparison["verdict"] == "PASS"),
        "handoff_id": board["handoff_id"],
        "tool_evidence_refs": list(board["terminal"]["tool_evidence_refs"]),
        **({"blocked_runtime_evidence": blocking_evidence} if blocking_evidence else {}),
    }
    if complete:
        loop.terminal_acceptance = _terminal_receipt(loop, summary, board, comparison)
        loop.terminal_outcome = loop.terminal_acceptance["task_outcome"]
        loop.protocol_state = "COMPLETED"
        set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
    else:
        loop.terminal_acceptance = {}
        loop.terminal_outcome = "BLOCKED" if kind == "BLOCKED" else "UNKNOWN"
        loop.protocol_state = "BLOCKED" if kind != "MAX_ROUNDS" else "INTERRUPTED"
        set_runtime_state(
            loop.task_id, "INTERRUPTED" if kind == "MAX_ROUNDS" else "PAUSED",
            reason=("EXECUTION_BUDGET_CHECKPOINT" if kind == "MAX_ROUNDS" else kind),
            root=loop.progress_root,
        )
    if hasattr(loop, "_emit"):
        loop._emit(
            "pokeclaw_executor_terminal", task_id=loop.task_id,
            request_id=loop.run_id, terminal_kind=kind,
            handoff_id=board["handoff_id"],
            model_reported_only=True,
            goal_verified=bool(comparison and comparison["verdict"] == "PASS"),
            event_count=len(board["events"]),
        )
    p = projection(board)
    terminal_line = (
        f"Runtime 最終驗收：{comparison['verdict']}（{comparison['reason_code']}）"
        if comparison else "尚未進入 Runtime 最終驗收。"
    )
    mismatch_lines = ""
    if blocking_evidence and blocking_evidence["status"] in {"OBSERVED_RUNTIME_BLOCKER", "OBSERVED_ADMISSION_REJECTION"}:
        mismatch_lines += "\n阻塞 Runtime Evidence：" + json.dumps(
            blocking_evidence, ensure_ascii=False, separators=(",", ":"),
        )
    if comparison and comparison["mismatches"]:
        mismatch_lines = "\n不一致欄位：" + json.dumps(
            comparison["mismatches"], ensure_ascii=False, separators=(",", ":"),
        )[:3000]
    unconfirmed = (comparison.get("finish_diagnostic") or {}).get("unconfirmed_evidence") if comparison else None
    if unconfirmed:
        mismatch_lines += "\n未確認的 Runtime Evidence：" + json.dumps(
            unconfirmed, ensure_ascii=False, separators=(",", ":"),
        )[:2000]
    state_report = ""
    if comparison and comparison.get("goal_state_table"):
        table = [{
            "condition_id": row.get("condition_id"),
            "condition_type": row.get("condition_type"),
            "expected": row.get("expected"), "actual": row.get("actual"),
            "verdict": row.get("verdict"), "reason_code": row.get("reason_code"),
            "evidence_refs": row.get("evidence_refs", []),
        } for row in comparison["goal_state_table"][:32]]
        serialized = json.dumps(
            table, ensure_ascii=False, separators=(",", ":"), default=str
        )
        state_report = (
            "\nRuntime Goal State Table（來源：實際 ToolResult）："
            + serialized[:5500]
            + ("\n[GOAL_STATE_TABLE_BOUNDED_PREVIEW；完整內容存於 Blackboard]"
               if len(serialized) > 5500 else "")
        )
        advisory = comparison.get("model_goal_checks_advisory") or {}
        if advisory and not advisory.get("model_checks_match_actual_values", True):
            state_report += (
                "\nPlanner 額外 Goal Check 與 Runtime 實值不符；"
                "已記錄為非權威提案，不覆寫凍結的 Goal Contract。"
            )
    failure_origin = (
        "\n驗收失敗來源：" + str(comparison.get("failure_origin") or "UNKNOWN")
        if comparison and comparison.get("verdict") == "FAIL" else ""
    )
    trailer = (
        "\n\nExecutor Blackboard（已寫入並回讀）：\n"
        f"Executor：{p['executor_state']}\n"
        f"Goal Driver：{p['goal_driver_state']}\n"
        f"Prerequisite：{p['prerequisite_state']}\n"
        f"Handoff ID：{p['handoff_id']}\n"
        f"Tool Evidence：{', '.join(p['tool_evidence_refs']) or '無'}\n"
        f"Parent Dispatch：{p['parent_dispatch_state']}\n"
        f"Parent Consumption：{p['parent_consumption_state']}\n"
        f"整體目標：{terminal_line}{failure_origin}{mismatch_lines}{state_report}"
    )
    if comparison and comparison["verdict"] == "PASS" and (
        comparison.get("completion_kind") == "ANSWER_ONLY"
    ):
        # A creative/text response is the artifact. Do not append internal
        # Blackboard logs to the actual user's joke/translation/answer.
        return str(summary)
    return str(summary) + trailer


def run_pokeclaw_executor(loop, request: str) -> str:
    """Execute arbitrary registered tools; Blackboard is the only recorder."""
    from agent_core.executor_command_rca import (
        capture_command_rca, capture_command_result_rca,
    )
    root = Path(loop.progress_root or loop.workspace)
    if root.name.casefold() == "source" and (root.parent / "collect.bat").is_file():
        root = root.parent
    requirement = assess_finish_evidence_requirement(
        request,
        attachments=bool(getattr(loop, "_tri_one_has_attached_context", False)),
        authorized_paths=bool(getattr(loop.tools, "_authorized_local_paths", ())),
        image_or_artifact_request=bool(getattr(loop, "image_delivery_plan", None)),
        continuation=bool(getattr(loop, "continuation_source", None)),
    )
    board = begin_executor_run(
        root, loop, request, evidence_requirement=requirement,
    )
    goal_state_contract = dict(board.get("goal_state_contract") or {})
    loop.executor_blackboard_terminal = board
    loop.executor_result = {}
    loop.terminal_acceptance = {}
    loop.terminal_outcome = "UNKNOWN"
    recent = []
    admission_repair_counts = {}
    consecutive_admission_rejections = 0
    hint = ""
    source = getattr(loop, "continuation_source", None) or {}
    if isinstance(source, Mapping) and source:
        # Trusted continuation context is *not* authority; only new human
        # approval can authorize an expired UNKNOWN_COMMAND proposal.
        candidates = [
            row for row in (source.get("pending_actions") or [])[:5]
            if isinstance(row, Mapping) and row.get("approval_required")
        ]
        if candidates:
            hint = (
                "接續先前尚未執行的 UNKNOWN_COMMAND 授權；"
                "若軟體層已完成前置決策，先精確驗證本次接續指令；成功執行後僅同一任務安全同類指令可以沿用；"
                "未預先批准的指令仍須重新詢問。"
                "歷史 command 摘要（不可當授權或執行證據）："
                + json.dumps(candidates, ensure_ascii=False, default=str)[:1200]
            )
    seen_mutations = set()
    pending_unknown_timeout = None
    pending_timeout_reconciliation = None
    timeout_reconciliation_witnesses = []
    native_preexecution_rejections = []
    native_denied_command_hashes = set()
    max_native_precheck_replans = 3
    # Git74.1: unified preflight, owned by Runtime. This is advisory data;
    # every Action still passes original per-action Security/Tool Admission.
    from agent_core.unified_preflight import (
        begin_task as begin_unified_preflight,
        inventory as unified_inventory,
        summary as unified_summary,
        action_observed as unified_action_observed,
    )
    # Lazy advisory inventory, not a prerequisite for reads or text.
    preflight = {"status": "DEFERRED"}
    environment_inventory = {}
    max_rounds = min(int(loop.max_turns), 1000)
    for turn in range(1, max_rounds + 1):
        loop._check_cancelled()
        loop.turn_id = turn
        loop.tools._protocol_turn_seq = turn
        turbo_context = (
            __import__("agent_core.turbo_execution_policy",
                       fromlist=["turbo_planning_snapshot"])
            .turbo_planning_snapshot(loop.workspace)
        )
        if turbo_context.get("turbo_requested") == "ON":
            append_executor_event(root, loop, {
                "round": turn, "kind": "TURBO_PLANNER_CONTEXT",
                "task_working_directory": str(loop.workspace),
                "authorization_workspace": str(
                    turbo_context.get("authorization_workspace") or ""),
                "policy_revision": int(turbo_context.get("policy_revision") or 0),
                "turbo_status": str(turbo_context.get("status") or ""),
                "turbo_reason_code": str(turbo_context.get("reason_code") or ""),
                "configured_roots": list(turbo_context.get("configured_roots") or ()),
                "native_broker_tools": list(turbo_context.get("native_broker_tools") or ()),
                "executed": False, "authorization_granted": False,
            })
        prompt = _prompt(
            request, loop.tools, recent, round_id=turn, max_rounds=max_rounds,
            hint=hint,
            evidence_requirement=requirement, goal_state_contract=goal_state_contract,
            environment_inventory=environment_inventory,
            turbo_planning_context=turbo_context,
            preexecution_rejections=native_preexecution_rejections[-3:],
        )
        hint = ""
        attachments = loop.tools.take_pending_attachments()
        loop.protocol_state = "DECIDING"
        # Diagnostic-only observations. The exact selected Minimal path is
        # different from the legacy protocol_loop planner/parser branch.
        # A browser-extracted model response is NOT server/network raw bytes.
        PIT.record("08_minimal_prompt", prompt, evidence_status="PLANNER_PROMPT_STRING", extra={"round": turn})
        append_executor_event(root, loop, {
            "round": turn, "kind": "PLANNER_CONTEXT_PREPARED",
            "prompt_chars": len(prompt), "prompt_utf8_bytes": len(prompt.encode("utf-8")),
            "context_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "evidence_total": len(loop.action_result_ledger),
            "executed": False, "authorization_granted": False,
        })
        # Model DOM transport interruption may have sent a turn. Do not retry
        # the transport without its established correlation protocol.
        response = loop.planner(prompt, None, attachments)
        PIT.record("11_minimal_planner_response", response, evidence_status="BROWSER_EXTRACTED_MODEL_RESPONSE", extra={"round": turn})
        PIT.record("11_minimal_parser_input", response, evidence_status="PARSER_INPUT_STRING", extra={"round": turn})
        # Lossless schema alias normalization is local and strictly
        # deterministic. It does NOT modify a tool command/path, authorize
        # an action, or manufacture evidence; avoid a browser round trip.
        repair = repair_single_decision(response)
        parser_input = repair[0] if repair is not None else response
        try:
            decision, metadata = parse_minimal_decision_with_diagnostics(
                parser_input, original_goal=request, allow_batch=True,
            )
            if repair is not None:
                PIT.record(
                    "12_git75_local_schema_repair",
                    repair[1],
                    evidence_status="DETERMINISTIC_NONAUTHORIZING_REPAIR",
                    extra={"round": turn},
                )
                append_executor_event(root, loop, {
                    "round": turn, "kind": "GOAL75_LOCAL_SCHEMA_REPAIR",
                    "changes": repair[1]["changes"],
                    "original_sha256": repair[1]["original_sha256"],
                    "canonical_sha256": repair[1]["canonical_sha256"],
                    "planner_round_saved": 1,
                    "authorization_granted": False, "executed": False,
                })
            PIT.record("12_minimal_decision", {
                "kind": decision.kind, "tool": decision.tool,
                "arguments": decision.arguments, "message": decision.message,
                "expected_runtime": decision.expected_runtime,
                "goal_checks": decision.goal_checks,
                "batch_action_count": len(decision.batch_calls),
                "normalization": metadata.get("normalization") if isinstance(metadata, dict) else None,
            }, evidence_status="PARSED_DECISION_VALUE", extra={"round": turn})
        except MinimalDecisionError as exc:
            PIT.record("12_minimal_parse_error", {
                "error_code": exc.code, "raw_response_sha256": hashlib.sha256(
                    str(response).encode("utf-8")
                ).hexdigest(),
            }, evidence_status="PARSE_REJECTED", extra={"round": turn})
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_DECISION_REJECTED",
                "decision_sha256": _digest(str(response)),
                "error_code": exc.code, "executed": False,
                "decision_diagnostic": {k: exc.diagnostic[k] for k in (
                    "message_field","message_type","message_chars","message_limit",
                    "message_issue","response_chars","response_bytes") if k in exc.diagnostic},
            })
            hint = "DECISION_REPAIR: " + exc.code + ". Return one valid JSON decision."
            if exc.code == "decision_message_invalid":
                hint += (" Finish summary must be a nonempty string of at most 3000 characters. "
                         "Retain all goal_checks; shorten redundant prose only. "
                         "Do not repeat tool actions already completed. Diagnosis=" +
                         _json_compact({k: exc.diagnostic.get(k) for k in
                            ("message_issue","message_type","message_chars","message_limit")}))
            continue
        if decision.kind in {"stage_plan", "stage_commit"}:
            append_executor_event(root, loop, {
                "round": turn, "kind": "LEAN_LEGACY_STAGE_ADVISORY",
                "decision_kind": decision.kind, "executed": False,
                "authorization_granted": False,
            })
            hint = "Stages are advisory. Proceed with the next useful Action or Finish."
            continue
        if decision.kind == "finish":
            if pending_timeout_reconciliation is not None:
                hint = ("RECONCILIATION_REQUIRED: finish cannot hide uncertain effects; "
                        "perform the exact frozen readonly checks first.")
                continue
            # Prior approval timeout does not veto independently proven Goal.
            # Transport version is Runtime-owned. No invented checks.
            if decision.contract_version != 2 and (
                getattr(loop, "action_result_ledger", None)
                or not requirement.get("answer_only_eligible", False)
            ):
                if decision.expected_runtime not in (None, []):
                    hint = "FINISH: omit legacy expected_runtime; reuse actual goal_checks."
                    continue
                decision = replace(decision, contract_version=2)
            canonical_finish = (adapt_finish_contract_v2(
                decision, task_id=loop.task_id, request_id=loop.run_id,
                task_epoch=loop.task_epoch, frozen_goal_contract=goal_state_contract,
            ) if decision.contract_version == 2 else None)
            preview = compare_runtime_finish(
                loop, root, request, decision.expected_runtime,
                goal_checks=decision.goal_checks, allow_open=True,
                answer=decision.message, canonical_finish=canonical_finish,
                lean_goal=True,
            )
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_FINISH_PROPOSAL",
                "finish_input": finish_input_projection(
                    decision.expected_runtime, decision.goal_checks),
                "comparator_reason_code": preview["reason_code"],
                "verdict": preview["verdict"], "executed": False,
            })
            if preview["verdict"] != "PASS":
                hint = ("GOAL_NOT_YET_VERIFIED: " + preview["reason_code"]
                        + "; missing/mismatch="
                        + json.dumps(preview.get("finish_diagnostic") or
                                     preview.get("mismatches") or {},
                                     ensure_ascii=False, default=str)[:3500]
                        + ". Continue with a useful admitted Action or correct Finish. "
                          "Do not invent evidence, weaken the original goal or replay "
                          "an unresolved mutation.")
                continue
            return _finish(loop, root, request, kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
                           summary=decision.message, complete=True, comparison=preview)
        if decision.kind == "blocked":
            board = read_executor_run(root, task_id=loop.task_id,
                request_id=loop.run_id, task_epoch=loop.task_epoch) or {}
            blocker = _lean_external_blocker(board)
            append_executor_event(root, loop, {
                "round": turn, "kind": "LEAN_BLOCKER_ASSESSED",
                "blocker": blocker, "executed": False,
                "authorization_granted": False,
            })
            if (blocker or pending_unknown_timeout) and pending_timeout_reconciliation is None:
                return _finish(loop, root, request, kind="BLOCKED",
                    summary="WAITING_EXTERNAL: " + decision.message,
                    complete=False)
            hint = ("BLOCKED_WITHOUT_EXTERNAL_PROOF: ordinary Runtime errors, failed "
                    "commands and exhausted strategies are not terminal. Reassess "
                    "the original Goal and choose another lawful action; preserve evidence.")
            continue
        batch = decision.batch_calls or (decision,)
        if len(batch) > 1 and pending_timeout_reconciliation is not None:
            hint = "RECONCILIATION_REQUIRED: use one exact frozen observation."
            continue
        if len(batch) > 1:
            append_executor_event(root, loop, {
                "round": turn, "kind": "PLANNER_BATCH_PROPOSED",
                "action_count": len(batch), "executed": False,
                "authorization_granted": False,
            })
        batch_observed = 0
        for batch_index, decision in enumerate(batch, 1):
            loop._check_cancelled()
            proposed = {"tool": decision.tool, **dict(decision.arguments or {})}
            PIT.record("13_minimal_admission_input", proposed, evidence_status="PROPOSED_ACTION_VALUE", extra={"round": turn})
            action, rejected = admit_minimal_action(decision, loop.tools)
            PIT.record("13_minimal_admission_output", {
                "admitted_action": action, "rejection_reason": str(rejected or ""),
                "original_proposed_action": proposed,
            }, evidence_status="ACTION_ADMISSION_RESULT", extra={"round": turn})
            # Git51: admission is ACTION-scoped, never task/global-scoped.
            # News, ordinary prose, web research and read-only observations must
            # behave like ACL OFF even if Turbo is requested or unavailable.
            # Only an actual admitted OS mutation/command requires a native
            # AppContainer Broker. Its per-action OS proof remains mandatory.
            from agent_core.turbo_broker_admission import admit_action
            broker_gate = admit_action(loop.workspace, action)
            PIT.record("13_minimal_broker_gate", {
                "action": action,
                "decision": broker_gate.get("decision"),
                "reason_code": broker_gate.get("reason_code"),
            }, evidence_status="OS_BROKER_ADMISSION_RESULT", extra={"round": turn})
            if broker_gate["decision"] == "BLOCKED":
                receipt = dict(broker_gate["evidence"])
                reason = str(broker_gate["reason_code"] or "SANDBOX_UNAVAILABLE")
                append_executor_event(root, loop, {
                    **receipt, "round": turn, "kind": "TURBO_SANDBOX_BLOCKED",
                    "toolName": decision.tool,
                    "arguments_sha256": _digest(decision.arguments),
                    "admission_status": "ADMITTED" if action else "REJECTED",
                    "admission_reason_code": str(rejected or "")[:128],
                    "action_requires_broker": True,
                })
                hint = ("NATIVE_BROKER_UNAVAILABLE: " + reason[:120]
                        + "; action not executed. Select a lawful alternative or "
                          "report the concrete external dependency. Never fall back to Host.")
                break
            unknown_approval = None
            risk_class = ""
            turbo = None
            auto_confirm = False
            if decision.tool == "run_command":
                # Persist EVERY Turbo decision, including disabled/mismatched
                # Workspace and pre-execution Admission rejections. Never only
                # record successful auto approvals: Planner and RCA require cause.
                from agent_core.command_risk import classify_command
                from agent_core.turbo_execution_policy import (
                    evaluate_turbo_confirmation, evaluate_native_broker_confirmation,
                    native_precheck_replan_eligible,
                )
                if action:
                    from agent_core.shell_execution import effective_action_for_policy
                    risk_class = classify_command(effective_action_for_policy(action))
                    # Git52: only the verified Native Broker admission can remove
                    # Turbo's second human consent prompt for UNKNOWN commands.
                    # Execution remains OS-attested, per-action and fail-closed.
                    turbo = (
                        evaluate_native_broker_confirmation(loop, action, broker_gate)
                        if broker_gate["decision"] == "BROKER_REQUIRED"
                        else evaluate_turbo_confirmation(loop, action)
                    )
                else:
                    risk_class = "NOT_ADMITTED"
                    turbo = {
                        "schema": "SMARTAGENT_TURBO_DECISION_V2",
                        "auto_confirm": False,
                        "reason_code": "PRIOR_ACTION_ADMISSION_REJECTED",
                        "policy_revision": 0,
                        "turbo_root": "",
                        "workspace_root": str(loop.workspace),
                        "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
                        "authorization_granted": False,
                    }
                auto_confirm = bool(
                    action and broker_gate["decision"] == "BROKER_REQUIRED"
                    and turbo.get("auto_confirm") is True
                )
                native_precheck_blocked = bool(
                    action and broker_gate["decision"] == "BROKER_REQUIRED"
                    and not turbo.get("auto_confirm")
                )
                decision_reason = (
                    str(turbo.get("reason_code") or "TURBO_REASON_MISSING")
                    if native_precheck_blocked or risk_class == "UNKNOWN" or not action
                    else "TURBO_SECOND_GATE_NOT_REQUIRED_" + risk_class
                )
                append_executor_event(root, loop, {
                    "round": turn, "kind": "TURBO_DECISION",
                    "schema": "SMARTAGENT_TURBO_DECISION_V2",
                    "toolName": "run_command",
                    "action_id": str((action or {}).get("action_id") or ""),
                    "arguments_sha256": _digest(decision.arguments),
                    "command_sha256": hashlib.sha256(
                        str((decision.arguments or {}).get("command") or "")
                        .encode("utf-8")
                    ).hexdigest(),
                    "command_rca": capture_command_rca(
                        decision.arguments, action,
                    ),
                    "risk_class": risk_class,
                    "admission_status": "ADMITTED" if action else "REJECTED_BEFORE_EXECUTION",
                    "admission_reason_code": str(rejected or "")[:128],
                    "auto_confirm": auto_confirm,
                    "turbo_policy_qualified": bool(turbo.get("auto_confirm")),
                    "decision": (
                        "NATIVE_PRECHECK_BLOCKED" if native_precheck_blocked
                        else "AUTO_CONFIRM" if auto_confirm
                        else "NORMAL_ADMISSION" if action and risk_class != "UNKNOWN"
                        else "HUMAN_CONFIRMATION_REQUIRED" if action
                        else "ADMISSION_REJECTED"
                    ),
                    "reason_code": decision_reason,
                    "command_security_code": str(turbo.get("command_security_code") or ""),
                    "command_security_approval_eligible": bool(
                        turbo.get("command_security_approval_eligible", False)
                    ),
                    "command_security_target_kind": str(
                        turbo.get("command_security_target_kind") or ""
                    ),
                    "command_security_detail_sha256": str(
                        turbo.get("command_security_detail_sha256") or ""
                    ),
                    "broker_decision": str(broker_gate.get("decision") or ""),
                    "workspace_root": str(loop.workspace),
                    "working_directory": str(
                        (action or decision.arguments or {}).get("working_directory")
                        or loop.workspace
                    )[:1024],
                    "turbo_root": str(turbo.get("turbo_root") or ""),
                    "policy_revision": int(turbo.get("policy_revision") or 0),
                    "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
                    "executed": False, "authorization_granted": False,
                })
            if action and decision.tool == "run_command":
                if native_precheck_blocked:
                    # Never run or fake a ToolResult for a denied proposal.
                    # A source-frozen diagnostic may explicitly allow either
                    # execution or a policy rejection for absolute --version.
                    # This exception only concerns completion evidence, NEVER
                    # sandbox admission or operating system permissions.
                    from agent_core.requirement_goal_state import _step
                    optional_version_step = ""
                    if (
                        goal_state_contract.get("status") == "COMPILED"
                        and goal_state_contract.get("goal_kind")
                        == "DIAGNOSTIC_EXECUTABLE_RESOLUTION"
                        and isinstance(goal_state_contract.get("executable_targets"), Mapping)
                    ):
                        candidate_step = _step(
                            str(action.get("command") or ""),
                            goal_state_contract["executable_targets"],
                        )
                        if candidate_step in {"git_version", "python_version"}:
                            optional_version_step = candidate_step
                    reason = str(turbo.get("reason_code") or "TURBO_NATIVE_SCOPE_DENIED")
                    command_hash = hashlib.sha256(
                        str(action.get("command") or "").encode("utf-8")
                    ).hexdigest()
                    recoverable = bool(
                        broker_gate["decision"] == "BROKER_REQUIRED"
                        and native_precheck_replan_eligible(turbo)
                    )
                    distinct = command_hash not in native_denied_command_hashes
                    budget_available = (
                        len(native_preexecution_rejections) < max_native_precheck_replans
                        and turn < max_rounds
                    )
                    replan = bool(
                        recoverable and distinct and budget_available
                        and not optional_version_step
                    )
                    rejection = {
                        "round": turn, "action_id": str(action["action_id"]),
                        "reason_code": reason,
                        "command_security_code": str(
                            turbo.get("command_security_code") or ""
                        ),
                        "command_security_target_kind": str(
                            turbo.get("command_security_target_kind") or ""
                        ),
                        "command_sha256": command_hash,
                        "broker_decision": "BROKER_REQUIRED",
                        "executed": False, "authorization_granted": False,
                    }
                    status = (
                        "REPLAN_REQUESTED" if replan
                        else "REPEATED_COMMAND" if recoverable and not distinct
                        else "REPLAN_BUDGET_EXHAUSTED" if recoverable and not budget_available
                        else "HARD_OR_POLICY_DENIAL"
                    )
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "TURBO_NATIVE_CONFIRMATION_BLOCKED",
                        "toolName": "run_command", "action_id": action["action_id"],
                        "goal_diagnostic_step": optional_version_step,
                        "risk_class": risk_class, "reason_code": reason,
                        "command_sha256": command_hash,
                        "arguments_sha256": _digest(decision.arguments),
                        "command_security_code": rejection["command_security_code"],
                        "command_security_approval_eligible": bool(
                            turbo.get("command_security_approval_eligible", False)
                        ),
                        "command_security_target_kind": rejection["command_security_target_kind"],
                        "command_security_detail_sha256": str(
                            turbo.get("command_security_detail_sha256") or ""
                        ),
                        "broker_decision": str(broker_gate.get("decision") or ""),
                        "replan_eligible": replan,
                        "replan_attempt": len(native_preexecution_rejections) + 1,
                        "replan_limit": max_native_precheck_replans,
                        "replan_status": status,
                        "executed": False, "authorization_granted": False,
                    })
                    if replan:
                        native_denied_command_hashes.add(command_hash)
                        native_preexecution_rejections.append(rejection)
                        append_executor_event(root, loop, {
                            "round": turn, "kind": "TURBO_NATIVE_REPLAN_REQUESTED",
                            "toolName": "run_command", "action_id": action["action_id"],
                            "command_sha256": command_hash,
                            "reason_code": reason,
                            "command_security_code": rejection["command_security_code"],
                            "replan_attempt": len(native_preexecution_rejections),
                            "replan_limit": max_native_precheck_replans,
                            "executed": False, "authorization_granted": False,
                        })
                        hint = (
                            "Native PREEXECUTION REJECTION (not a ToolResult): "
                            "command_security_code=" + rejection["command_security_code"]
                            + "; target_kind=" + rejection["command_security_target_kind"]
                            + "; action_id=" + rejection["action_id"]
                            + "; executed=false; no permission granted. Do NOT "
                            "repeat this command. Propose a different legal action "
                            "and independently verify any mutations."
                        )
                        break
                    if optional_version_step:
                        # This exact step is optional by the original request:
                        # policy denial is a valid diagnostic *outcome*. Keep
                        # going to the next independent command or final report.
                        # The denied action is never inserted into ToolResult
                        # history or treated as successful executable loading.
                        native_denied_command_hashes.add(command_hash)
                        hint = (
                            "Native AppContainer policy REJECTED optional "
                            + optional_version_step
                            + " --version before execution. "
                            + "reason_code=" + reason[:120]
                            + "; The frozen diagnostic contract accepts this "
                            "as a POLICY_REJECTED observation, not a ToolResult. "
                            "Do not retry or fallback to Host CMD. Continue "
                            "remaining independent diagnostics and explicitly "
                            "report the rejection in Finish."
                        )
                        break
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "TURBO_NATIVE_REPLAN_TERMINAL",
                        "action_id": action["action_id"],
                        "command_sha256": command_hash,
                        "reason_code": reason,
                        "replan_exhausted": bool(recoverable),
                        "executed": False, "authorization_granted": False,
                    })
                    hint = ("NATIVE_ACTION_DENIED: choose a different authorized "
                            "native strategy or report the verified permission dependency; "
                            "never fallback to Host.")
                    break
                if auto_confirm:
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "TURBO_AUTO_ADMITTED",
                        "toolName": "run_command", "action_id": action["action_id"],
                        "policy_revision": turbo["policy_revision"],
                        "reason_code": turbo["reason_code"],
                        "turbo_root": turbo["turbo_root"],
                        "risk_class": risk_class,
                        "arguments_sha256": _digest(decision.arguments),
                        "executed": False, "authorization_granted": False,
                    })
                elif risk_class == "UNKNOWN":
                    if pending_unknown_timeout:
                        return _finish(
                            loop, root, request, kind="BLOCKED",
                            summary="FAIL：UNKNOWN_COMMAND_APPROVAL_TIMEOUT；替代指令"
                                    "仍無法在既有權限下安全下發。接續任務需重新授權，"
                                    "不會自動執行過期指令。",
                            complete=False,
                        )
                    from agent_core.unknown_command_approval import (
                        request_unknown_command_approval,
                    )
                    from agent_core.resume_command_approval import ResumeCommandGate
                    from agent_core.command_family_approval import TaskCommandFamilyGrants
                    from agent_core.auto_approve_mode import evaluate_auto_approve
                    auto_approval = evaluate_auto_approve(
                        loop.workspace, __import__(
                            "agent_core.shell_execution", fromlist=["execution_contract"]
                        ).execution_contract(action)["policy_command"],
                        working_directory=action.get("working_directory") or loop.workspace,
                    )
                    family_gate = TaskCommandFamilyGrants(root)
                    family_match = (
                        None if auto_approval["approved"]
                        else family_gate.match(loop=loop, action=action)
                    )
                    preapproval = (
                        None if auto_approval["approved"] or family_match is not None
                        else ResumeCommandGate(root).consume_for_action(loop=loop, action=action)
                    )
                    append_executor_event(root, loop, {
                        "round": turn, "kind": (
                            "AUTO_APPROVAL_EVALUATED" if auto_approval["approved"]
                            else "HUMAN_APPROVAL_REUSED" if family_match is not None
                            else "HUMAN_APPROVAL_REQUESTED"
                        ),
                        "toolName": "run_command",
                        "action_id": action["action_id"],
                        "arguments_sha256": _digest(decision.arguments),
                        "risk_class": "UNKNOWN", "executed": False,
                        "auto_approval": {
                            "approved": auto_approval["approved"],
                            "reason_code": auto_approval["reason_code"],
                            "acl": auto_approval["acl"],
                            "turbo_requested": auto_approval["turbo_requested"],
                            "turbo_effective": auto_approval["turbo_effective"],
                            "auto_approve_requested": auto_approval["auto_approve_requested"],
                            "auto_approve_effective": auto_approval["auto_approve_effective"],
                            "authorization_scope": auto_approval["authorization_scope"],
                            "policy_revision": auto_approval["policy_revision"],
                            "executor_backend": auto_approval["executor_backend"],
                            "command_sha256": auto_approval["command_sha256"],
                            "executed": False,
                        },
                    })
                    approval = (
                        {
                            "state": "APPROVED", "reason": "auto_approve_policy",
                            "approval_id": "", "command_sha256": auto_approval["command_sha256"],
                        } if auto_approval["approved"] else
                        family_match if family_match is not None
                        else preapproval if preapproval is not None
                        else request_unknown_command_approval(loop, action)
                    )
                    unknown_approval = approval
                    append_executor_event(root, loop, {
                        "round": turn,
                        "kind": "AUTO_APPROVAL_DECIDED" if auto_approval["approved"]
                                else "HUMAN_APPROVAL_DECIDED",
                        "toolName": "run_command",
                        "action_id": action["action_id"],
                        "state": approval["state"],
                        "reason_code": approval["reason"],
                        "approval_id": approval["approval_id"],
                        "command_sha256": approval["command_sha256"],
                        "executed": False, "evidence_ref": "",
                    })
                    if approval["state"] != "APPROVED":
                        recent.append({
                            "round": turn, "tool": "run_command",
                            "admission_verdict": "HUMAN_APPROVAL_" + approval["state"],
                            "approval_id": approval["approval_id"],
                            "risk_class": "UNKNOWN",
                            "executed": False,
                        })
                        if approval["state"] == "DENIED":
                            return _finish(
                                loop, root, request, kind="BLOCKED",
                                summary="FAIL：使用者拒絕這條未知命令："
                                        + approval["reason"] + "。接續任務須重新授權。",
                                complete=False,
                            )
                        pending_unknown_timeout = approval
                        if getattr(loop, "continuation_store", None):
                            # Save exact command only in protected approval state.
                            # The general continuation/Blackboard stores a digest
                            # rather than sensitive command text.
                            ResumeCommandGate(root).remember(
                                loop=loop, action=action,
                                old_approval_id=approval["approval_id"],
                            )
                            # Persist only non-executed metadata, never the raw
                            # command or an authorizing permit. A resumed model
                            # re-proposes an Action and obtains a new approval.
                            loop.continuation_store.pending_batch(loop.task_id, [{
                                "tool": "run_command",
                                "action_id": action["action_id"],
                                "approval_required": True,
                                "approval_kind": "UNKNOWN_COMMAND",
                                "approval_id": approval["approval_id"],
                                "command_sha256": approval["command_sha256"],
                                "operation": str(action.get("operation") or ""),
                                "executed": False,
                            }])
                        hint = (
                            "未知風險命令尚未執行，使用者授權逾時或不可用（"
                            + approval["reason"]
                            + "）；這份 Approval 無法重用。"
                            "可另選一個已明確授權、並通過安全檢查的替代工具或命令。"
                            "若無法取得相同完成條件的證據，回傳 blocked；"
                            "可在新接續任務重新發起一次性授權。"
                        )
                        break
            if action and _possibly_mutating(action, broker_gate["decision"]):
                fingerprint = _digest({
                    key: value for key, value in action.items() if key not in {
                        "action_id", "expected", "success_criteria", "verify",
                        "verifies_action_id", "condition_id", "reconciliation"}

                })
                if fingerprint in seen_mutations:
                    action = None
                    rejected = "MUTATION_REPLAY_DENIED_WITHOUT_INDEPENDENT_PROOF"
            if action is None:
                diagnosis = dict(getattr(
                    loop.tools, "_last_minimal_admission_diagnostic", {},
                ) or {})
                # This is a rejected proposal, NEVER an executed ToolResult.
                # Keep bounded typed field diagnostics for model repair and RCA.
                diagnostic = {
                    "schema": str(diagnosis.get("schema") or "SMARTAGENT_ACTION_ADMISSION_DIAGNOSTIC_V1")[:100],
                    "stage": str(diagnosis.get("stage") or "ADMISSION")[:60],
                    "tool": decision.tool,
                    "reason_code": str(rejected)[:120],
                    "repair_class": str(diagnosis.get("repair_class") or "ACTION_REASSESSMENT")[:60],
                    "field_issues": list(diagnosis.get("field_issues") or [])[:16],
                    "executed": False,
                    "authorization_granted": False,
                    "scope_failure_stage": str(diagnosis.get("scope_failure_stage") or "")[:80],
                    "scope_cause_digest": str(diagnosis.get("scope_cause_digest") or "")[:64],
                }
                postcondition_repair = diagnostic["stage"] == "POSTCONDITION_CONTRACT"
                if postcondition_repair:
                    diagnostic.update({
                        "operation": str((decision.arguments or {}).get("operation") or "")[:40],
                        "command_risk_class": str(diagnosis.get("command_risk_class") or "")[:40],
                        "command_read_only_proven": diagnosis.get("command_read_only_proven"),
                    })
                # Scope by literal command for run_command. Distinct commands
                # must not share the same issue counter. A separate consecutive
                # bound prevents endless command changes from evading repair.
                # Secondary postcondition diagnostics do not reset the counter
                # while the primary rejection is still unresolved.
                proposal_identity = (
                    str((decision.arguments or {}).get("command", ""))
                    if decision.tool == "run_command"
                    else _digest(decision.arguments)
                )
                repair_key = _digest({
                    "tool": decision.tool,
                    "proposal_identity_sha256": hashlib.sha256(
                        proposal_identity.encode("utf-8")
                    ).hexdigest(),
                    "reason": str(rejected),
                    "issue_fields": [
                        {"field": item.get("field"), "reason": item.get("reason"),
                         "constraint_code": item.get("constraint_code", "")}
                        for item in (diagnostic["field_issues"][:1] if postcondition_repair
                                     else diagnostic["field_issues"])
                        if isinstance(item, Mapping)
                    ],
                })
                admission_repair_counts[repair_key] = (
                    admission_repair_counts.get(repair_key, 0) + 1
                )
                repairs_for_same_issue = admission_repair_counts[repair_key]
                consecutive_admission_rejections += 1
                optional_scope_version = ""
                if (
                    decision.tool == "run_command"
                    and diagnosis.get("stage") == "SCOPE_PREFLIGHT"
                    and str(rejected or "").startswith("scope_rejected:")
                    and goal_state_contract.get("status") == "COMPILED"
                    and goal_state_contract.get("goal_kind")
                    == "DIAGNOSTIC_EXECUTABLE_RESOLUTION"
                    and isinstance(goal_state_contract.get("executable_targets"), Mapping)
                ):
                    from agent_core.requirement_goal_state import _step
                    candidate_step = _step(
                        str((decision.arguments or {}).get("command") or ""),
                        goal_state_contract["executable_targets"],
                    )
                    if candidate_step in {"git_version", "python_version"}:
                        optional_scope_version = candidate_step
                row = append_executor_event(root, loop, {
                    "round": turn, "kind": "ADMISSION_REJECTED",
                    "toolName": decision.tool,
                    "goal_diagnostic_step": optional_scope_version,
                    "arguments": _safe_arguments(decision.arguments),
                    "arguments_sha256": _digest(decision.arguments),
                    **({"command_rca": capture_command_rca(
                        decision.arguments,
                    )} if decision.tool == "run_command" else {}),
                    "admission_diagnostic": diagnostic,
                    "admission_repair_attempt": repairs_for_same_issue,
                    "admission_rejection_streak": consecutive_admission_rejections,
                    "runtimeEvidence": {
                        "status": "REJECTED", "executed": False,
                        "reason_code": str(rejected)[:160],
                        "scope_failure_stage": diagnosis.get("scope_failure_stage", ""),
                        "scope_cause_digest": diagnosis.get("scope_cause_digest", ""),
                        "directory_preconditions": diagnosis.get("directory_preconditions", {}),
                        "evidence_ref": "",
                    },
                })
                recent.append({
                    "round": turn, "tool": decision.tool,
                    "admission_verdict": "REJECTED_BEFORE_EXECUTION",
                    "admission_diagnostic": diagnostic,
                })
                if optional_scope_version:
                    hint = (
                        "Runtime SCOPE_PREFLIGHT policy REJECTED optional "
                        + optional_scope_version
                        + " absolute --version; no ToolResult was produced. "
                        "The frozen diagnostic contract records this as "
                        "POLICY_REJECTED, not executed; do NOT use Host CMD or "
                        "replay the denied command. Finish remaining observations "
                        "and report the rejection and missing Exit Code/stdout/stderr."
                    )
                    break
                hint = (
                    "Tool Admission 拒絕（沒有執行）："
                    + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))[:2500]
                    + "。依 field_issues 修正 JSON 型別、合法 Enum 或契約不一致；"
                    "實際 command 和 operation 的語意仍由模型判斷。"
                    "需要權限的 Action 不能藉改名為唯讀或改 operation 繞過。"
                    "修正後重新提交完整 ToolCall；不要重送既有執行成功的 Action。"
                )
                if postcondition_repair:
                    hint += (
                        " This command is not proven read-only, even when operation is INSPECT "
                        "or VERIFY. Correct all missing fields in one proposal. Provide a "
                        "nonempty success_criteria string describing the intended observable "
                        "result and a nonempty verify object or array of allowed checks from "
                        "the tool catalog. For an expected output file, use file_exists with "
                        "its authorized literal path. For observation-only work, use an "
                        "appropriate structured observation tool if available. Keep BUILD, "
                        "MUTATE or other actual operation semantics. If no valid check or "
                        "safe observation is possible, report that blocker instead of "
                        "resubmitting the rejected proposal."
                    )
                if repairs_for_same_issue >= 3 or consecutive_admission_rejections >= 10:
                    hint += " Repeated rejection: change approach rather than repeat the same proposal."
                break

            # The only follow-up while a timed-out mutation is unresolved is
            # its predeclared, read-only filesystem observation. The Planner
            # cannot redefine this after seeing the Timeout or silently replay it.
            if pending_timeout_reconciliation is not None:
                from agent_core.timeout_reconciliation import _path, TimeoutReconciliationError
                checks = pending_timeout_reconciliation["checks"]
                permitted_paths = {
                    c["path"] for c in checks
                    if c["path"] not in pending_timeout_reconciliation["completed_check_paths"]
                }
                try:
                    scoped_path = _path(action.get("path")) if action.get("tool") == "observe_fact" else ""
                except TimeoutReconciliationError:
                    scoped_path = ""
                if action.get("tool") != "observe_fact" or scoped_path not in permitted_paths:
                    append_executor_event(root, loop, {
                        "round":turn, "kind":"TIMEOUT_RECONCILIATION_ACTION_BLOCKED",
                        "source_action_id":pending_timeout_reconciliation["action_id"],
                        "source_evidence_ref":pending_timeout_reconciliation["source_evidence_ref"],
                        "action_id":action.get("action_id"),"reason_code":"DECLARED_READ_ONLY_OBSERVATION_REQUIRED",
                        "executed":False,"authorization_granted":False,
                    })
                    hint = ("RECONCILIATION_SCOPE_REQUIRED: submit only "
                            "the frozen read-only observation; no replay.")
                    break
            # An admitted action is independent progress. Its actual ToolResult
            # will determine what to do next; no prior rejection grants proof.
            consecutive_admission_rejections = 0
            admission_repair_counts.clear()
            loop._canonical_action_proposals[action["action_id"]] = dict(action)
            loop._planner_original_actions[action["action_id"]] = {
                **proposed, "action_id": action["action_id"],
            }
            mutating = _possibly_mutating(action, broker_gate["decision"])
            if preflight.get("status") == "DEFERRED" and action["tool"] in {
                    "run_command", "project_sync", "query_project", "install_toolchain"}:
                try:
                    preflight = begin_unified_preflight(
                        root, loop.workspace, task_id=loop.task_id, request_id=loop.run_id)
                    environment_inventory = unified_inventory(root, loop.workspace, preflight)
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "UNIFIED_PREFLIGHT",
                        "preflight": unified_summary(preflight),
                        "executed": False, "authorization_granted": False,
                    })
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    preflight = {"status": "UNAVAILABLE", "error_class": type(exc).__name__}
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "UNIFIED_PREFLIGHT_UNAVAILABLE",
                        "error_class": type(exc).__name__, "authorization_granted": False,
                    })
            # Git70: freeze the Planner's action assertions before execution.
            # The digest is recorded independently of subsequent ToolResults.
            from agent_core.goal_evidence_matching import frozen_expected_sha256
            from agent_core.timeout_reconciliation import reconciliation_digest
            reconciliation_digest_value = (
                reconciliation_digest(action) if action.get("reconciliation") is not None else ""
            )
            expected_contract = action.get("expected")
            expected_digest = (frozen_expected_sha256(expected_contract)
                               if isinstance(expected_contract, dict) else "")
            append_executor_event(root, loop, {
                "round": turn, "kind": "ACTION_ADMITTED",
                "toolName": action["tool"], "action_id": action["action_id"],
                "arguments": _safe_arguments(decision.arguments),
                "arguments_sha256": _digest(decision.arguments),
                **({"command_rca": capture_command_rca(
                    decision.arguments, action,
                )} if action["tool"] == "run_command" else {}),
                "mutating": mutating,
                "expected_contract_sha256": expected_digest,
                "expected_contract_present": bool(expected_digest),
                "reconciliation_contract_sha256": reconciliation_digest_value,
                "reconciliation_contract_present": bool(reconciliation_digest_value),
                "turbo_decision": (
                    "NOT_APPLICABLE_TYPED_ACTION" if action["tool"] != "run_command"
                    else ("AUTO_CONFIRM" if auto_confirm
                          else "NORMAL_ADMISSION" if risk_class != "UNKNOWN"
                          else "HUMAN_CONFIRMATION_REQUIRED")
                ),
                "command_risk_class": risk_class,
            })
            # Register only actually dispatched mutations, not rejected proposals.
            if mutating:
                seen_mutations.add(fingerprint)
            PIT.record("13_minimal_pre_dispatch_action", action, evidence_status="PRE_DISPATCH_ACTION_VALUE", extra={"round": turn})
            loop.protocol_state = "EXECUTING"
            try:
                dispatch = getattr(loop, "_execute_runtime_action", loop._execute_action)
                raw_result = dispatch(action)
            except Exception as exc:
                # Never replay a possibly side-effecting command.
                from agent_core.tool_result_contract import encode_result
                raw_result = encode_result(
                    "UNKNOWN", "TOOL_EXCEPTION_OUTCOME_UNKNOWN",
                    tool=action["tool"], executed=True,
                    data={"exception_class": type(exc).__name__},
                )
            PIT.record("15_minimal_tool_result_raw", raw_result,
                       evidence_status="RUNTIME_TOOL_RESULT_RETURN", extra={"round": turn})
            receipt = decode_result(raw_result) or {
                "status": "UNKNOWN", "reason": "UNPARSEABLE_TOOL_RESULT",
                "executed": False, "data": {"possible_partial_execution": mutating},
            }
            PIT.record("15_minimal_tool_result_decoded", receipt,
                       evidence_status="DECODED_RUNTIME_TOOL_RESULT", extra={"round": turn})
            runtime_status = str(receipt.get("status") or "UNKNOWN")
            runtime_reason = str(receipt.get("reason") or "UNCLASSIFIED_TOOL_RESULT")
            after_approval_timeout = bool(pending_unknown_timeout)
            digest = hashlib.sha256(str(raw_result).encode("utf-8")).hexdigest()
            evidence_ref = "MIN-EV-" + hashlib.sha256(
                (action["action_id"] + "\0" + digest).encode("utf-8")
            ).hexdigest()[:24]
            result_data = receipt.get("data")
            # Persist before projecting/truncating anything for the Planner or
            # Blackboard. This write is a Runtime-owned evidence operation only.
            from agent_core.runtime_evidence_store import persist_runtime_evidence
            try:
                evidence_pointer = persist_runtime_evidence(
                    root, task_id=loop.task_id, request_id=loop.run_id,
                    task_epoch=loop.task_epoch, action_id=action["action_id"],
                    evidence_ref=evidence_ref, raw_result=str(raw_result),
                    receipt=receipt,
                )
            except (OSError, ValueError, TypeError) as exc:
                evidence_pointer = {
                    "evidence_ref": evidence_ref, "store_status": "PERSIST_FAILED",
                    "error_class": type(exc).__name__,
                }
            # Git70: observed facts are obtained by reading the exact task-bound,
            # immutable MIN-EV record, never from Planner prose or a truncated preview.
            # An unavailable/invalid record drives evidence recollection, NOT replay.
            match_report = None
            if expected_digest:
                from agent_core.goal_evidence_matching import (
                    match_expected, append_match_report,
                )
                from agent_core.runtime_evidence_store import (
                    read_runtime_action_result, RuntimeEvidenceError,
                )
                observed_receipt = None
                if evidence_pointer.get("store_status") == "PERSISTED_AND_READBACK_VERIFIED":
                    try:
                        observed_raw = read_runtime_action_result(
                            root, task_id=loop.task_id, request_id=loop.run_id,
                            task_epoch=loop.task_epoch, action_id=action["action_id"],
                            evidence_ref=evidence_ref,
                        )
                        observed_receipt = decode_result(observed_raw)
                    except (RuntimeEvidenceError, ValueError, TypeError, OSError):
                        observed_receipt = None
                match_report = match_expected(
                    expected_contract, observed_receipt, evidence_ref=evidence_ref,
                    evidence_store_status=(
                        evidence_pointer.get("store_status", "PERSIST_FAILED")
                        if observed_receipt is not None else "READBACK_UNAVAILABLE"),
                    frozen_sha256=expected_digest, action_id=action["action_id"],
                )
                try:
                    append_match_report(root, loop.task_id, loop.run_id, match_report)
                except (OSError, ValueError) as exc:
                    match_report["local_report_persist_error"] = type(exc).__name__
                append_executor_event(root, loop, {
                    "round": turn, "kind": "EXPECTED_OBSERVED_MATCH",
                    "action_id": action["action_id"], "toolName": action["tool"],
                    "evidence_ref": evidence_ref,
                    "expected_observed_match": match_report,
                    "authorization_granted": False,
                })
            tool_result = _tool_result(
                runtime_status, runtime_reason, result_data,
                evidence_pointer=evidence_pointer,
            )
            planner_evidence = _planner_projection(receipt, raw_result, evidence_pointer)
            loop.action_result_ledger[action["action_id"]] = {
                "action_id": action["action_id"], "tool": action["tool"],
                "request_id": loop.run_id, "task_id": loop.task_id,
                "task_epoch": loop.task_epoch, "produced_round": turn,
                "result_id": evidence_ref, "result": str(raw_result),
                "evidence_store": dict(evidence_pointer),
                "result_sha256": digest, "result_status": runtime_status,
                "result_reason": runtime_reason,
                "expected_contract_sha256": expected_digest,
                "reconciliation_contract_sha256": reconciliation_digest_value,
                "expected_observed_match": match_report,
                "execution_status": runtime_status,
                "verification_status": str(
                    (result_data or {}).get("verification_status", "UNVERIFIED")
                ) if isinstance(result_data, Mapping) else "UNVERIFIED",
                "executed": receipt.get("executed") is True,
                "evidence_status": "ACTIVE",
                "evidence_ref": evidence_ref,
            }
            # Link only to previously persisted ToolResult stages for this exact
            # Task / Request / Epoch. Model summaries carry no provenance.
            prior_board = read_executor_run(
                root, task_id=loop.task_id, request_id=loop.run_id,
                task_epoch=loop.task_epoch,
            ) or {}
            prior_stages = [
                row["github_source_stage_evidence"]
                for row in prior_board.get("events", ())
                if isinstance(row, Mapping)
                and row.get("kind") == "TOOL_RESULT"
                and isinstance(row.get("github_source_stage_evidence"), Mapping)
            ]
            github_stage = github_source_stage_evidence(
                action, receipt, evidence_ref, prior_stages=prior_stages,
            )
            row = append_executor_event(root, loop, {
                "round": turn, "kind": "TOOL_RESULT",
                "toolName": action["tool"], "action_id": action["action_id"],
                "toolResult": tool_result,
                "evidence_store": dict(evidence_pointer),
                **({"expected_observed_match": match_report} if match_report else {}),
                **({"github_source_stage_evidence": github_stage} if github_stage else {}),
                **({"command_execution_rca": capture_command_result_rca(
                    raw_result, receipt,
                )} if action["tool"] == "run_command" else {}),
                "runtimeEvidence": {
                    "status": runtime_status, "reason_code": runtime_reason[:160],
                    "executed": receipt.get("executed") is True,
                    "result_sha256": digest, "evidence_ref": evidence_ref,
                    "turbo_auto_confirm": bool(auto_confirm),
                    "turbo_decision_reason_code": (
                        str((turbo or {}).get("reason_code") or "")[:120]
                        if action["tool"] == "run_command" else "NOT_APPLICABLE_TYPED_ACTION"
                    ),
                    "side_effect_may_have_occurred": bool(
                        mutating and receipt.get("executed")
                    ),
                    "process": {
                        key: result_data[key] for key in (
                            "process_state", "exit_code", "timed_out",
                            "verification_status", "execution_status",
                            "expectation_status", "pid",
                        )
                        if isinstance(result_data, Mapping) and key in result_data
                    },
                    # Persist bounded, Runtime-origin diagnostics for RCA.
                    # Never record full command text or unbounded exceptions.
                    "command_rejection_stage": (
                        str(result_data.get("command_rejection_stage") or "")[:64]
                        if isinstance(result_data, Mapping)
                        and runtime_status == "REJECTED" else ""
                    ),
                    "command_error": (
                        str(result_data.get("command_error") or "")[:400]
                        if isinstance(result_data, Mapping)
                        and runtime_status == "REJECTED" else ""
                    ),
                },
            })
            # Definitively not-started commands may be retried after re-admission.
            # Never unlock completed or uncertain mutations using exit code alone.
            if (mutating and receipt.get("executed") is False
                    and runtime_status == "FAILED" and isinstance(result_data, Mapping)
                    and result_data.get("process_state") == "NOT_STARTED"
                    and result_data.get("process_started") is False
                    and result_data.get("possible_partial_execution") is False
                    and evidence_pointer.get("store_status") == "PERSISTED_AND_READBACK_VERIFIED"):
                seen_mutations.discard(fingerprint)
            # Git74.1: update the persisted cross-task state only after the
            # immutable observed ToolResult has been committed.
            if preflight.get("status") not in {"UNAVAILABLE", "DEFERRED"}:
                try:
                    old_source = preflight.get("environment_source")
                    old_project = dict(preflight.get("project_sync") or {})
                    preflight = unified_action_observed(
                        root, loop.workspace, task_id=loop.task_id,
                        request_id=loop.run_id, action=action, receipt=receipt,
                        match_report=match_report, evidence_ref=evidence_ref)
                    if (old_source != preflight.get("environment_source")
                            or old_project != preflight.get("project_sync")):
                        environment_inventory = unified_inventory(
                            root, loop.workspace, preflight)
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "UNIFIED_PREFLIGHT_UPDATED",
                        "action_id": action["action_id"],
                        "preflight_id": preflight.get("preflight_id"),
                        "preflight_revision": preflight.get("revision"),
                        "project_status": (preflight.get("project_sync") or {}).get("status"),
                        "environment_source": preflight.get("environment_source"),
                        "executed": False, "authorization_granted": False,
                    })
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "UNIFIED_PREFLIGHT_UPDATE_FAILED",
                        "error_class": type(exc).__name__,
                        "action_id": action["action_id"],
                        "executed": False, "authorization_granted": False,
                    })
            # Publish only real successful executable version probes; an install
            # command's success alone never proves an environment is usable.
            try:
                from agent_core.software_registry import register_verified_software_probe
                registered_software = register_verified_software_probe(
                    action, receipt, evidence_ref,
                )
                if registered_software is not None:
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "SOFTWARE_REGISTRY_UPDATED",
                        "toolName": action["tool"], "action_id": action["action_id"],
                        "evidence_ref": evidence_ref,
                        "software": registered_software["name"],
                        "executed": False, "authorization_granted": False,
                    })
                    environment_inventory["external_software_registry"] = (
                        __import__(
                            "agent_core.software_registry",
                            fromlist=["software_registry_snapshot"],
                        ).software_registry_snapshot()
                    )
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                append_executor_event(root, loop, {
                    "round": turn, "kind": "SOFTWARE_REGISTRY_WRITE_FAILED",
                    "error_class": type(exc).__name__,
                    "evidence_ref": evidence_ref, "executed": False,
                })
            if (unknown_approval is not None
                    and unknown_approval.get("state") == "APPROVED"
                    and unknown_approval.get("reason") not in {
                        "same_task_command_family_reused", "auto_approve_policy",
                    }
                    and runtime_status == "SUCCEEDED"
                    and receipt.get("executed") is True):
                from agent_core.command_family_approval import TaskCommandFamilyGrants
                if TaskCommandFamilyGrants(root).remember(
                        loop=loop, action=action,
                        approval_id=unknown_approval["approval_id"],
                        evidence_ref=evidence_ref):
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "COMMAND_FAMILY_GRANT_RECORDED",
                        "toolName": "run_command", "action_id": action["action_id"],
                        "approval_id": unknown_approval["approval_id"],
                        "evidence_ref": evidence_ref, "executed": False,
                    })
            # Git71: reconcile *after* ToolResult and MIN-EV have been persisted.
            # Never alter the original UNKNOWN receipt or declare a mutation
            # successful because the timeout assertion happened to match.
            from agent_core.timeout_reconciliation import (
                begin_timeout_reconciliation, check_reconciliation_observation,
                append_reconciliation_report,
            )
            reconciliation_retry = ""
            if pending_timeout_reconciliation is not None:
                observed_recovery = check_reconciliation_observation(
                    pending_timeout_reconciliation, action, receipt,
                    match_report, evidence_ref)
                append_reconciliation_report(
                    root, loop.task_id, loop.run_id, observed_recovery)
                append_executor_event(root, loop, {
                    "round":turn,"kind":"TIMEOUT_RECONCILIATION_OBSERVED",
                    "source_action_id":pending_timeout_reconciliation["action_id"],
                    "source_evidence_ref":pending_timeout_reconciliation["source_evidence_ref"],
                    "verification_action_id":action["action_id"],
                    "verification_evidence_ref":evidence_ref,
                    "state":observed_recovery["state"],"reason":observed_recovery["reason"],
                    "executed":False,"authorization_granted":False,
                })
                if observed_recovery["state"] not in {"OBSERVATION_REQUIRED", "RECONCILED"}:
                    # Preserve the frozen scope; a bad read can be corrected.
                    reconciliation_retry = str(observed_recovery["reason"])
                else:
                    timeout_reconciliation_witnesses.append(action["action_id"])
                if observed_recovery["state"]=="RECONCILED":
                    completed_recovery={
                        **observed_recovery,
                        "witness_action_ids":list(timeout_reconciliation_witnesses),
                    }
                    append_executor_event(root, loop, {
                        "round":turn,"kind":"TIMEOUT_RECONCILIATION_COMPLETED",
                        "source_action_id":pending_timeout_reconciliation["action_id"],
                        "source_evidence_ref":pending_timeout_reconciliation["source_evidence_ref"],
                        "verification_action_id":action["action_id"],
                        "verification_evidence_ref":evidence_ref,
                        "witness_action_ids":list(timeout_reconciliation_witnesses),
                        "reconciliation_sha256":pending_timeout_reconciliation["reconciliation_sha256"],
                        "state":"RECONCILED",
                        "process_finality_verified":True,
                        "side_effect_scope":"DECLARED_PATHS_ONLY",
                        "replay_original_action":False,
                        "executed":False,"authorization_granted":False,
                    })
                    append_reconciliation_report(root, loop.task_id, loop.run_id,
                                                 completed_recovery)
                    pending_timeout_reconciliation=None
                    timeout_reconciliation_witnesses=[]
                elif observed_recovery["state"]=="OBSERVATION_REQUIRED":
                    pending_timeout_reconciliation=observed_recovery
            elif (action["tool"]=="run_command" and runtime_status=="UNKNOWN"
                  and isinstance(result_data, Mapping) and
                  result_data.get("timed_out") is True and match_report):
                candidate=begin_timeout_reconciliation(
                    action,receipt,match_report,evidence_ref)
                append_reconciliation_report(root,loop.task_id,loop.run_id,candidate)
                append_executor_event(root,loop,{
                    "round":turn,"kind":"TIMEOUT_RECONCILIATION_STARTED",
                    "source_action_id":action["action_id"],
                    "source_evidence_ref":evidence_ref,
                    "state":candidate["state"],"reason":candidate["reason"],
                    "reconciliation_sha256":candidate.get("reconciliation_sha256",""),
                    "process_finality_verified":candidate.get("process_finality_verified",False),
                    "executed":False,"authorization_granted":False,
                })
                if candidate["state"]=="OBSERVATION_REQUIRED":
                    pending_timeout_reconciliation=candidate
                    timeout_reconciliation_witnesses=[]
                    hint=("GIT71_TIMEOUT_RECONCILIATION_REQUIRED: OS terminated the "
                          "supervised process tree; the original execution still has "
                          "UNKNOWN effects. Perform ONLY the frozen read-only "
                          "observe_fact checks with matching Expected, then continue. "
                          "Never replay the original command. checks="+
                          json.dumps(candidate["checks"],ensure_ascii=False))
            recent.append({
                "round": turn, "tool": action["tool"],
                "action_id": action["action_id"],
                "toolResult": tool_result, "evidence_ref": evidence_ref,
                "planner_evidence": planner_evidence,
                "evidence_store_status": evidence_pointer.get("store_status"),
                **({"expected_observed_match": match_report} if match_report else {}),
                **({"github_source_stage_evidence": github_stage} if github_stage else {}),
            })
            loop.protocol_state = "OBSERVED"
            loop.executor_result = {
                "schema": "SMARTAGENT_POKECLAW_EXECUTOR_RESULT_V1",
                "executor_state": "OBSERVED", "tool_status": runtime_status,
                "tool_result": dict(tool_result), "evidence_ref": evidence_ref,
                "goal_verified": False,
            }
            loop._emit(
                "pokeclaw_executor_tool_result", request_id=loop.run_id,
                task_id=loop.task_id, task_epoch=loop.task_epoch,
                round=turn, tool=action["tool"], evidence_ref=evidence_ref,
                is_success=tool_result["isSuccess"],
                runtime_status=runtime_status, executed=receipt.get("executed") is True,
            )
            if getattr(loop, "continuation_store", None):
                loop.continuation_store.snapshot(loop)
            if match_report:
                # Git70 recovery is a *new observation*, never an automatic retry
                # of the command that produced this immutable MIN-EV.
                if match_report["match_state"] == "RECOLLECT_EVIDENCE":
                    hint = (
                        "GIT70_EVIDENCE_RECOVERY_REQUIRED: observed facts cannot be "
                        "matched. First read_runtime_evidence for evidence_ref="
                        + evidence_ref
                        + "; then use a permitted read-only observation for fields "
                        + json.dumps(match_report["unobserved_fields"][:24])
                        + ". Do NOT replay the original Action; do not Finish yet."
                    )
                elif match_report["match_state"] == "MISMATCH":
                    hint = (
                        "GIT70_EXPECTED_OBSERVED_MISMATCH: verified observed facts "
                        "contradict this action's predeclared Expected. Replan "
                        "toward the FROZEN original user goal; do not relabel the "
                        "observed facts or declare terminal FAIL solely because "
                        "this attempt was unsuccessful. evidence_ref=" + evidence_ref
                    )
            if reconciliation_retry:
                hint = ("RECONCILIATION_READ_RETRY: " + reconciliation_retry
                        + ". Use exact frozen path and Expected; no mutation. checks="
                        + json.dumps(pending_timeout_reconciliation["checks"], ensure_ascii=False))
            if (match_report
                    and match_report["safety_decision"] == "PAUSE_AND_RECONCILE"
                    and mutating and pending_timeout_reconciliation is None):
                return _finish(
                    loop, root, request, kind="EXECUTION_OUTCOME_UNKNOWN",
                    summary="GIT70_SAFETY_RECONCILIATION_REQUIRED; original command "
                            "must not be replayed: " + evidence_ref,
                    complete=False,
                )
            if after_approval_timeout and runtime_status not in {"SUCCEEDED", "UNKNOWN", "RUNNING", "PARTIAL"}:
                hint = ("ALTERNATIVE_ACTION_FAILED: " + runtime_reason
                        + ". Prior approval unavailable; try another lawful route "
                          "or request user authorization only if indispensable.")
            if (runtime_status in {"UNKNOWN", "RUNNING", "PARTIAL"}
                    and mutating and pending_timeout_reconciliation is None):
                return _finish(
                    loop, root, request, kind="EXECUTION_OUTCOME_UNKNOWN",
                    summary="可能產生副作用的工具結果未確定，禁止重送：" + runtime_reason,
                    complete=False,
                )
            # Do not execute the remaining preplanned actions if the result
            # is not proven successful, a postcondition failed, or a recovery
            # is required. Never re-execute successful prefix actions.
            batch_observed += 1
            if (runtime_status != "SUCCEEDED"
                    or receipt.get("executed") is not True
                    or evidence_pointer.get("store_status") != "PERSISTED_AND_READBACK_VERIFIED"
                    or (isinstance(result_data, Mapping) and
                        str(result_data.get("verification_status", "")).upper()
                        in {"FAIL", "FAILED", "MISMATCH", "SPEC_INVALID"})
                    or (match_report and match_report.get("match_state") != "MATCH")
                    or pending_timeout_reconciliation is not None
                    or hint):
                break
        if len(batch) > 1:
            append_executor_event(root, loop, {
                "round": turn, "kind": "PLANNER_BATCH_RETURNED",
                "action_count": len(batch), "observed_count": batch_observed,
                "unprocessed_count": len(batch) - batch_index,
                "executed": False, "authorization_granted": False,
            })
            if batch_index < len(batch):
                hint += (" Batch stopped at item " + str(batch_index) + "/" +
                         str(len(batch)) + "; remaining items were NOT submitted "
                         "or executed. Reuse completed evidence and replan the tail.")
    return _finish(
        loop, root, request, kind="MAX_ROUNDS",
        summary="EXECUTION_BUDGET_CHECKPOINT: decision budget exhausted; Goal not verified, no task infeasibility claimed.",
        complete=False,
    )


__all__ = ["run_pokeclaw_executor"]
