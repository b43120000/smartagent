"""Bounded, evidence-linked Runtime -> Planner decision context.

This is a read-only projection of existing Runtime ledgers. It never admits an
Action, changes the request scope, invents evidence, or selects a strategy.
Do not include raw tool output, commands, credentials or model prose here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .semantic_capability_resolution import CAPABILITY_TO_TOOL, TOOL_TO_CAPABILITY

SCHEMA = "SMARTAGENT_PLANNER_CONTEXT_V1"
MAX_RECENT_ACTIONS = 16
MAX_OLDER_BLOCKERS = 8
MAX_PROTOCOL_DIAGNOSTICS = 6
_SCOPE_CODES = (
    "resolved_scope_expands_beyond_explicit_path",
    "command_working_directory_outside_request_scope",
    "command_working_directory_not_directory",
    "path_independent_command_cwd_outside_bound_workspace",
    "explicit_path_scope_requires_workspace",
    "explicit_path_scope_requires_paths",
    "explicit_path_scope_requires_path",
    "explicit_path_scope_requires_root",
    "path_outside_authorized_roots",
    "request_write_ceiling_unresolved",
    "request_write_ceiling_tool_not_authorized",
    "request_write_ceiling_command_unverified",
    "semantic_capability_not_selected",
    "semantic_capability_binding_identity_mismatch",
)
_TOKEN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,119}$")


def _code(value: object, default: str = "") -> str:
    """Do not propagate arbitrary error text or untrusted ToolResult prose."""
    s = str(value or "").strip()
    return s if _TOKEN.fullmatch(s) else default


def _scope_code(*values: object) -> str:
    for value in values:
        content = str(value or "")
        for code in _SCOPE_CODES:
            if code in content:
                return code
    return ""


def _record(action_id: str, row: Mapping, action_row: Mapping) -> dict:
    tool = _code(row.get("tool") or (action_row.get("action") or {}).get("tool"))
    reason = _code(row.get("result_reason") or action_row.get("result_reason"))
    raw = str(action_row.get("result", "") or "")
    result_status = _code(row.get("result_status") or action_row.get("result_status"), "UNKNOWN")
    execution_status = _code(row.get("execution_status") or action_row.get("execution_status"), "UNKNOWN")
    scope_error = _scope_code(reason, raw)
    return {
        "action_id": str(action_id)[:160],
        "tool": tool,
        "produced_round": row.get("produced_round"),
        "executed": row.get("executed", action_row.get("executed")),
        "result_status": result_status,
        "execution_status": execution_status,
        "verification_status": _code(row.get("effective_verification_status") or row.get("verification_status"), "UNKNOWN"),
        "result_reason": reason or ("TOOL_SCOPE_REJECTED" if "[TOOL_SCOPE_REJECTED]" in raw else ""),
        "scope_error_code": scope_error,
        "evidence_status": _code(row.get("evidence_status"), "ACTIVE"),
        "superseded_by_action_id": str(row.get("superseded_by_action_id", "") or "")[:160],
        "condition_id": str(row.get("condition_id", "") or "")[:160],
    }


def _diagnostic(row: Mapping) -> dict:
    issues = []
    for issue in list(row.get("field_issues") or [])[:8]:
        if isinstance(issue, Mapping):
            issues.append({
                "field": _code(issue.get("field")),
                "code": _code(issue.get("code")),
                "expected": str(issue.get("expected", "") or "")[:120],
            })
    repair = row.get("capability_repair")
    safe_repair = {}
    if isinstance(repair, Mapping):
        shape = repair.get("proposal_shape")
        shape = shape if isinstance(shape, Mapping) else {}
        safe_shape_keys = (
            "present", "object_type_valid", "key_set_exact",
            "extra_key_count", "schema_present", "schema_matches",
            "capabilities_is_list", "capability_count",
            "unknown_or_invalid_count", "duplicate_count",
        )
        safe_shape = {
            k: v for k in safe_shape_keys for v in [shape.get(k)]
            if type(v) is bool or (type(v) is int and 0 <= v <= 100000)
        }
        candidate = repair.get("canonical_candidate")
        if (isinstance(candidate, Mapping)
                and candidate.get("schema") == "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1"
                and isinstance(candidate.get("capabilities"), list)
                and 1 <= len(candidate["capabilities"]) <= 12
                and all(type(x) is str and x in CAPABILITY_TO_TOOL
                        for x in candidate["capabilities"])):
            safe_candidate = {
                "schema": "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1",
                "capabilities": sorted(set(candidate["capabilities"])),
            }
        else:
            safe_candidate = None
        safe_repair = {
            "canonical_candidate": safe_candidate,
            "candidate_applied": False,
            "authorization_granted": False,
            "repair_target": "report_progress.capability_resolution",
            "expected_keys": ["schema", "capabilities"],
            "canonical_schema": "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1",
            "proposal_shape": safe_shape,
            "previously_selected": [
                _code(x) for x in list(repair.get("previously_selected") or [])[:12]
            ],
            "required_for_submitted_actions": [
                _code(x) for x in list(repair.get("required_for_submitted_actions") or [])[:12]
            ],
        }
    context_repair = row.get("context_request_repair")
    safe_context = {}
    if isinstance(context_repair, Mapping) and context_repair.get("schema") == (
            "SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1"):
        safe_context = {
            "schema": "SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1",
            "repair_scope": "CONTEXT_REQUEST_SCHEMA",
            "allowed_fields": ["kind", "detail", "blocking_action_id"],
            "allowed_kinds": ["BLOCKED", "RUNTIME_READ", "USER_INPUT"],
            "field_issues": issues,
            "objective_blocker_ids": [
                str(x)[:160] for x in list(context_repair.get("objective_blocker_ids") or [])[:8]
                if isinstance(x, str)
            ],
            "blocked_requires_runtime_receipt": True,
            "runtime_read_requires_continue": True,
        }
    command_repair = row.get("command_repair")
    safe_command = {}
    if (isinstance(command_repair, Mapping)
            and command_repair.get("schema") == "SMARTAGENT_COMMAND_REPAIR_READINESS_V1"):
        from .command_repair_readiness import ALLOWLISTED_FIELDS
        mode = str(command_repair.get("mode", ""))
        if mode in {"FIELD_REPAIR", "ACTION_RECONSTRUCTION"}:
            command_issues = []
            for item in list(command_repair.get("field_issues") or [])[:16]:
                if not isinstance(item, Mapping):
                    continue
                field = _code(item.get("field"))
                if field in ALLOWLISTED_FIELDS:
                    command_issues.append({
                        "field": field,
                        "code": _code(item.get("code"), "UNKNOWN"),
                    })
            fields = [
                _code(item) for item in list(command_repair.get("required_fields") or [])[:16]
                if _code(item) in ALLOWLISTED_FIELDS
            ]
            safe_command = {
                "schema": "SMARTAGENT_COMMAND_REPAIR_READINESS_V1",
                "mode": mode,
                "action_id": str(command_repair.get("action_id", "") or "")[:160],
                "issue_count": len(command_issues),
                "field_issues": command_issues,
                "required_fields": fields,
                "full_admission_required": True,
                "authorization_granted": False,
                "execution_inferred": False,
            }
    ccrc = row.get("cross_contract_repair")
    safe_ccrc = {}
    if (isinstance(ccrc, Mapping)
            and ccrc.get("schema") == "SMARTAGENT_CROSS_CONTRACT_REPAIR_V1"):
        mode = _code(ccrc.get("mode"))
        if mode in {"FIELD_REPAIR", "ACTION_RECONSTRUCTION", "REPLAN_REQUIRED"}:
            safe_ccrc = {
                "schema": "SMARTAGENT_CROSS_CONTRACT_REPAIR_V1",
                "mode": mode,
                "field_rounds": min(2, max(0, int(ccrc.get("field_rounds", 0) or 0))),
                "reconstruction_rounds": min(1, max(0, int(ccrc.get("reconstruction_rounds", 0) or 0))),
                "replan_required": mode == "REPLAN_REQUIRED",
                "full_admission_required": True,
                "authorization_granted": False,
                "budget_reset": False,
            }
    return {
        "reason": _code(row.get("reason"), "UNKNOWN"),
        "field_issues": issues,
        "repair_scope": _code(row.get("repair_scope")),
        "command_repair": safe_command,
        "cross_contract_repair": safe_ccrc,
        "context_request_repair": safe_context,
        "capability_repair": safe_repair,
        "preserved_action_ids": [str(x)[:160] for x in list(row.get("preserved_action_ids") or [])[:16]],
    }


def build_planner_context_snapshot(runtime) -> dict:
    """Project current authoritative state into one bounded Planner context.

    Failure status is not proof that the requested fact is false. In particular,
    REJECTED/NOT_EXECUTED and UNKNOWN retain their own distinct meanings.
    """
    evidence = dict(getattr(runtime, "action_result_ledger", {}) or {})
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    ids = list(evidence)
    recent = ids[-MAX_RECENT_ACTIONS:]
    older_blockers = [aid for aid in ids[:-MAX_RECENT_ACTIONS]
                     if str((evidence.get(aid) or {}).get("evidence_status", "ACTIVE")) == "ACTIVE"
                     and (str((evidence.get(aid) or {}).get("result_status", "UNKNOWN"))
                          in {"FAILED", "REJECTED", "UNKNOWN", "PARTIAL"}
                          or str((evidence.get(aid) or {}).get("execution_status", "UNKNOWN"))
                          in {"FAILED", "UNKNOWN", "PARTIAL", "QUEUED", "RUNNING"})]
    retained = list(dict.fromkeys(older_blockers[-MAX_OLDER_BLOCKERS:] + recent))
    rows = [_record(aid, evidence[aid], actions.get(aid) or {}) for aid in retained]
    from .runtime_fact_ledger import planner_projection
    traces = [planner_projection(
        (actions.get(aid) or {}).get("provenance"),
        request_id=str(getattr(runtime, "run_id", "")),
        task_epoch=str(getattr(runtime, "task_epoch", "")),
    ) for aid in retained]
    traces = [item for item in traces if item is not None]
    omitted_ids = [aid for aid in older_blockers[:-MAX_OLDER_BLOCKERS] if aid not in retained]

    resolution = getattr(getattr(runtime, "tools", None), "_runtime_semantic_capability_resolution", None)
    matched = (isinstance(resolution, Mapping)
               and resolution.get("request_id") == getattr(runtime, "run_id", None)
               and resolution.get("task_epoch") == getattr(runtime, "task_epoch", None))
    selected = sorted(set(resolution.get("capabilities", ()))) if matched else []
    old_binding_mismatch = isinstance(resolution, Mapping) and not matched
    als = dict(getattr(runtime, "action_loop_state", {}) or {})
    candidate_tools = list(dict.fromkeys(str(t) for t in list(als.get("allowed_next_actions") or []) if t))
    tool_readiness = []
    for tool in candidate_tools:
        capability = TOOL_TO_CAPABILITY.get(tool)
        needs_selection = bool(matched and capability and capability not in selected)
        from .action_readiness import tool_readiness_contract
        tool_readiness.append({
            "tool": tool,
            "action_fields": tool_readiness_contract(tool),
            "read_capability": capability or "",
            "capability_state": ("BINDING_IDENTITY_MISMATCH" if old_binding_mismatch else
                                 "SELECTION_REQUIRED" if needs_selection else
                                 "SELECTED" if capability and matched else
                                 "COMPATIBILITY_UNBOUND" if capability else "NOT_SCR_GATED"),
            "action_admission": "REQUIRES_RUNTIME_ADMISSION",
            "selection_can_expand_authorization": False,
        })
    strategy = dict(getattr(getattr(runtime, "progress_ledger", None), "strategy_state", {}) or {})
    progress = getattr(runtime, "progress_ledger", None)
    try:
        caps = runtime._context_capabilities()
    except (AttributeError, ValueError, TypeError, RuntimeError):
        caps = {}
    objective_blockers = []
    for entry in list(caps.get("objective_blockers") or [])[-16:]:
        if isinstance(entry, Mapping):
            objective_blockers.append({
                "action_id": str(entry.get("action_id", "") or "")[:160],
                "reason": _code(entry.get("reason"), "UNKNOWN"),
            })
    recovery = dict(getattr(runtime, "pending_result_recovery", {}) or {})
    diagnostics = [_diagnostic(row) for row in list(getattr(runtime, "_planner_context_diagnostics", []) or [])[-MAX_PROTOCOL_DIAGNOSTICS:]
                   if isinstance(row, Mapping)]
    from .goal_strategy_transition import build_transition_readiness
    from .action_readiness import current_action_readiness
    goal_transition_readiness = build_transition_readiness(runtime)
    from .progress_reply_contract import dynamic_progress_contract
    payload = {
        "schema": SCHEMA,
        "dynamic_progress_contract": dynamic_progress_contract(
            progress,
            runtime_transition=str(als.get("required_transition", "")),
            action_history_count=len(actions),
        ),
        "goal_transition_readiness": goal_transition_readiness,
        "goal": {
            "status": "INCOMPLETE" if getattr(progress, "decision", "CONTINUE") == "CONTINUE" else
                      _code(getattr(progress, "decision", ""), "UNKNOWN"),
            "strategy_revision": int(strategy.get("revision", 1) or 1),
            "current_step": getattr(progress, "current_step", 0),
            "total_steps": getattr(progress, "total_steps", 0),
        },
        "scope": {
            "authorized_paths": [str(p)[:1024] for p in list(getattr(runtime, "authorized_paths", []) or [])[:16]],
            "scope_expansion_authorized": False,
            "scope_admission_still_required": True,
        },
        "recent_action_evidence": rows,
        "recent_action_provenance": traces,
        "observed_runtime_facts": [row["fact"] for row in traces if row.get("fact")],
        "scope_preflight_readiness": current_action_readiness(evidence, actions),
        "evidence_total": len(ids),
        "omitted_older_action_count": len(ids) - len(retained),
        "omitted_active_blocker_count": len(omitted_ids),
        "omitted_active_blocker_refs": [str(x)[:160] for x in omitted_ids[-24:]],
        "critical_context_omitted": bool(omitted_ids),
        "latest_protocol_diagnostics": diagnostics,
        "environment_facts": [
            {"evidence_ref": str(ref)[:160], "fact_state": _code((record.get("fact") or {}).get("state"), "UNKNOWN"),
             "probe_status": _code((record.get("fact") or {}).get("probe_status"), "UNKNOWN")}
            for ref, record in list(dict(getattr(runtime, "_environment_fact_refs", {}) or {}).items())[:20]
            if isinstance(record, Mapping)
        ],
        "condition_evidence": [
            {"condition_id": str(item.get("condition_id", "") or "")[:160],
             "action_id": str(item.get("action_id", "") or "")[:160],
             "status": _code(item.get("status"), "UNKNOWN")}
            for item in list(dict(getattr(runtime, "condition_result_ledger", {}) or {}).values())[-16:]
            if isinstance(item, Mapping)
        ],
        "runtime_transition": _code(als.get("required_transition"), "UNKNOWN"),
        "action_loop_blockers": [
            {"blocking_action_id": str(b.get("blocking_action_id", "") or "")[:160],
             "reason": _code(b.get("reason"), "UNKNOWN"),
             "release_kind": _code((b.get("release_conditions") or [{}])[0].get("kind"), "")}
            for b in list(als.get("blockers") or [])[-16:] if isinstance(b, Mapping)
        ],
        "context_acquisition": {
            "candidate_read_tools": list(caps.get("read_tools") or [])[:16],
            "candidate_tools_are_not_admission_grants": True,
            "objective_blockers": objective_blockers,
        },
        "runtime_capability_state": {
            "binding_present": bool(matched),
            "binding_identity_mismatch": bool(old_binding_mismatch),
            "selected_capabilities": selected,
            "finite_registry": dict(CAPABILITY_TO_TOOL),
            "tool_readiness": tool_readiness,
            "cumulative_selection_required": bool(matched),
        },
        "recovery": {
            "reason": _code(recovery.get("reason")),
            "cross_contract_repair": {
                "schema": "SMARTAGENT_CROSS_CONTRACT_REPAIR_V1",
                "active": bool(getattr(getattr(runtime, "_ccrc", None), "active", False)),
                "mode": _code(getattr(getattr(runtime, "_ccrc", None), "mode", "")),
                "field_rounds": min(2, max(0, int(getattr(getattr(runtime, "_ccrc", None), "field_rounds", 0)))),
                "reconstruction_rounds": min(1, max(0, int(getattr(getattr(runtime, "_ccrc", None), "reconstruction_rounds", 0)))),
                "replan_required": bool(getattr(getattr(runtime, "_ccrc", None), "replan_required", False)),
                "goal_anchor_sha256": str(getattr(getattr(runtime, "_ccrc", None), "goal_digest", ""))[:64],
                "authorization_granted": False,
                "budget_reset": False,
            },
            "pending_command_repairs": [
                {
                    "action_id": str(action_id)[:160],
                    "mode": (
                        "ACTION_RECONSTRUCTION" if item.get("status") ==
                        "ACTION_RECONSTRUCTION_REQUIRED" else "FIELD_REPAIR"
                    ),
                    "issue_count": min(16, len((item.get("readiness") or {}).get("field_issues") or [])),
                    "required_fields": [
                        _code(field)
                        for field in list((getattr(runtime, "pending_command_field_repairs", {})
                            or {}).get(action_id, {}).get("required_fields") or [])[:16]
                        if _code(field) in {
                            "operation", "verify", "success_criteria", "timeout",
                            "working_directory", "expected_failure", "expected_verification",
                        }
                    ],
                    "executed": False,
                    "authorization_granted": False,
                    "full_admission_required": True,
                }
                for action_id, item in list(
                    dict(getattr(runtime, "deferred_action_repairs", {}) or {}).items()
                )[-16:]
                if isinstance(item, Mapping) and item.get("tool") == "run_command"
                and action_id not in actions
            ],
            "strategy_revision_applies_to_future_actions_only": True,
            "budget_not_reset_by_revision": True,
            "required_action": _code(recovery.get("required_action")),
            "active": bool(recovery.get("active")),
            "pending_progress_action_ids": [str(a.get("action_id", ""))[:160]
                                            for a in list(getattr(runtime, "pending_progress_repair_actions", []) or [])[:16]
                                            if isinstance(a, Mapping)],
        },
        "decision_authority": "PLANNER",
        "evidence_and_admission_authority": "RUNTIME",
        "raw_tool_results_included": False,
        "actual_commands_and_raw_results_retained_in_runtime_ledger": True,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["context_digest"] = digest
    payload["context_snapshot_id"] = "CTX-" + digest[:20].upper()
    return payload


def render_planner_context_snapshot(runtime) -> tuple[str, dict]:
    payload = build_planner_context_snapshot(runtime)
    return ("[SMARTAGENT_PLANNER_CONTEXT_V1]\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            + "\n[/SMARTAGENT_PLANNER_CONTEXT_V1]\n"
            "This is a Runtime-owned, read-only decision snapshot. Candidate tools are NOT grants. "
            "Honor executed/verification states and do not treat UNKNOWN as absence. "
            "Goal Transition Readiness only classifies evidence: Planner decides any changed strategy. "
            "An optional report_progress.goal_transition must bind a declared Goal condition "
            "to a current Runtime evidence_ref; no such proposal authorizes tools. "
            "A denied read is not proof of a missing directory. For a project workspace "
            "preflight workspace_not_found_or_not_directory, do not reissue "
            "inspect_project_scope against an unavailable directory; select a new "
            "authorized observation or prerequisite strategy. "
            "Use ACTION_REPAIR for missing tool fields, EVIDENCE_ACQUISITION for "
            "unverified workspace state, and do not repeat an identically denied read "
            "without new evidence or changed parameters. "
            "runtime_capability_state describes observed state, NOT a report_progress field. "
            "Only report_progress.capability_resolution is the Planner proposal, with exactly "
            "{schema: SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1, capabilities: [canonical keys]}. "
            "To add a selected SCR capability, propose a cumulative capability_resolution "
            "and pass all ordinary Tool Admission checks; do not re-run "
            "rejected actions merely under new IDs.", payload)
