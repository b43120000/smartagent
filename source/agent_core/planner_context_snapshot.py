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
        # Safe codes are preserved for *every* admission boundary, not just
        # CommandRepair. Paths, commands and untrusted details are excluded.
        "scope_reason_code": _code(row.get("scope_reason_code")),
        "scope_category": _code(row.get("scope_category")),
        "scope_path_role": _code(row.get("scope_path_role")),
        "scope_transition": _code(row.get("scope_transition")),
        "scope_required_field": _code(row.get("scope_required_field")),
        "action_id": _code(row.get("action_id")),
        "tool": _code(row.get("tool")),
        "blocking_evidence_ref": _code(row.get("blocking_evidence_ref")),
        "failure_category": _code(row.get("failure_category")),
        "next_evaluation": _code(row.get("next_evaluation")),
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
    traces = []
    for aid in retained:
        trace = planner_projection(
            (actions.get(aid) or {}).get("provenance"),
            request_id=str(getattr(runtime, "run_id", "")),
            task_epoch=str(getattr(runtime, "task_epoch", "")),
        )
        if trace is not None:
            trace["evidence_status"] = str((evidence.get(aid) or {}).get(
                "evidence_status", "ACTIVE"))[:40]
            traces.append(trace)
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
    from .goal_gap_ledger import reconstruct_goal_gaps
    from .interrupt_readiness import interrupt_readiness, _read_attempts
    from .goal_prerequisite_resolution import resolve_goal_prerequisites
    from .source_candidate_ledger import build_source_candidate_ledger
    from .source_integrity_resolution import reconcile_source_integrity
    from .action_readiness import current_action_readiness
    goal_transition_readiness = build_transition_readiness(runtime)
    gap_view = reconstruct_goal_gaps(runtime)
    source_view = build_source_candidate_ledger(runtime, gap_view)
    artifact_view = getattr(runtime, "_source_discovery_bootstrap", None) or {}
    from .remote_artifact_access_readiness import (
        resolve_remote_artifact_access, derive_admitted_git_read_receipt,
    )
    from .runtime_software_inventory import build_runtime_software_inventory
    environment = getattr(runtime, "_planner_environment_snapshot", None) or {}
    access_receipt = derive_admitted_git_read_receipt(
        artifact_view, action_ledger=actions, result_ledger=evidence,
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
    ) or getattr(runtime, "_remote_artifact_access_runtime_receipt", None)
    remote_access_view = resolve_remote_artifact_access(
        artifact_view,
        environment=environment,
        runtime_receipt=access_receipt,
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
    )
    software_inventory = build_runtime_software_inventory(
        environment, getattr(runtime, "_project_environment_hint", None),
    )
    source_integrity_view = reconcile_source_integrity(runtime, source_view)
    interruption_view = interrupt_readiness(
        gap_view, caps, attempted_reads=_read_attempts(runtime),
        source_candidates=source_view,
    )
    prerequisite_view = resolve_goal_prerequisites(
        gap_view, interruption_view, source_view, source_integrity_view,
        artifact_view, remote_access_view,
    )
    # Precondition candidates are explicitly carried into Progress and Replan:
    # a one-time suggestion cannot become satisfied without Runtime evidence.
    from .prerequisite_transition_gate import build_prerequisite_transition
    transition_view = build_prerequisite_transition(
        artifact_view=artifact_view,
        prerequisite_view=prerequisite_view,
        remote_view=remote_access_view,
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
        bound_workspace=str(getattr(runtime, "workspace", "") or ""),
        tool_scope_preview=getattr(getattr(runtime, "tools", None), "preview_tool_scope", None),
        action_ledger=actions, result_ledger=evidence,
        guidance_issued=bool(getattr(runtime, "_remote_access_probe_guidance_issued", False)),
    )
    # Preserve failure causality independently of the last-16 Action window.
    from .failure_causal_receipt import recent_failure_receipts
    failure_receipts = recent_failure_receipts(
        action_ledger=actions, result_ledger=evidence,
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
    )
    if transition_view.get("actual_probe_attempts"):
        remote_access_view = {
            **remote_access_view,
            # Attempted tool execution is not an authorization receipt.
            "network_request_attempted": any(
                item.get("executed") is True
                for item in transition_view["actual_probe_attempts"]
            ),
            "probe_failure_evidence_ref": str(
                (transition_view.get("latest_probe_failure") or {}).get(
                    "action_id", ""
                )
            ),
        }
    from .action_resolution_trace import (
        deployment_path_prerequisites, bounded_action_trace_projection,
    )
    path_prerequisites = deployment_path_prerequisites(
        getattr(runtime, "_target_path_contract", None), gap_view,
    )
    action_resolution_traces = bounded_action_trace_projection(
        getattr(runtime, "_rejected_action_resolution_traces", None), actions,
        rewrite_ledger=getattr(runtime, "_runtime_action_rewrite_notes", None),
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
    )
    from .shared_blackboard import (
        build_runtime_capability_manifest, build_strategy_candidate_registry,
        evaluate_terminal_readiness, build_shared_blackboard,
    )
    capability_manifest = build_runtime_capability_manifest(runtime)
    from .decision_rules.directory_workspace_rule import evaluate_directory_decision
    from .semantic_capability_resolution import translate_request_action
    semantic_action = dict(
        getattr(runtime, "_semantic_action_translation", {}) or
        translate_request_action(
            str(getattr(runtime, "task_request", "") or
                getattr(getattr(runtime, "progress_ledger", None), "goal", "") or "")
        )
    )
    directory_target = dict(getattr(runtime, "_directory_target_contract", {}) or {})
    directory_decision = evaluate_directory_decision(runtime, gap_view)
    strategy_candidates = build_strategy_candidate_registry(
        runtime, capability_manifest=capability_manifest,
        prerequisite_view=prerequisite_view,
        prerequisite_transition=transition_view,
        remote_access_view=remote_access_view,
        artifact_view=artifact_view,
        goal_gap_view=gap_view,
        directory_decision=directory_decision,
    )
    terminal_readiness = evaluate_terminal_readiness(
        strategy_candidates,
        request_id=str(getattr(runtime, "run_id", "") or ""),
        task_epoch=str(getattr(runtime, "task_epoch", "") or ""),
    )
    from .progress_reply_contract import dynamic_progress_contract
    payload = {
        "schema": SCHEMA,
        "dynamic_progress_contract": dynamic_progress_contract(
            progress,
            runtime_transition=str(als.get("required_transition", "")),
            action_history_count=len(actions),
        ),
        "goal_transition_readiness": goal_transition_readiness,
        "persistent_goal_gaps": gap_view,
        "semantic_action_translation": semantic_action,
        "directory_target_binding": directory_target,
        "directory_decision_rule": directory_decision,
        "interrupt_readiness": interruption_view,
        "goal_prerequisite_resolution": prerequisite_view,
        "prerequisite_transition_gate": transition_view,
        "failure_causal_receipts": failure_receipts,
        "failure_causal_receipt_count": len(failure_receipts),
        "deployment_path_prerequisites": path_prerequisites,
        "action_resolution_trace": action_resolution_traces,
        "unified_contract_transaction": dict(getattr(runtime, "_last_unified_contract_receipt", {}) or {}),
        "canonical_contract_resolution": [
            dict(row) for row in list(getattr(runtime, "_last_canonical_contract_receipts", []) or [])[-4:]
            if isinstance(row, Mapping)
        ],
        "source_candidate_ledger": source_view,
        "source_discovery_bootstrap": artifact_view,
        "remote_artifact_access_readiness": remote_access_view,
        "runtime_software_inventory": software_inventory,
        "historical_source_bootstrap": {
            "index_status": source_view.get("historical_index_status", "UNKNOWN"),
            "bootstrap_state": source_view.get("historical_bootstrap_state", "UNKNOWN"),
            "recovered_count": source_view.get("historical_recovered_count", 0),
            "task_records_examined": source_view.get("historical_task_records_examined", 0),
            "historical_paths_are_live_evidence": False,
            "historical_paths_are_read_authorization": False,
        },
        "source_integrity_resolution": source_integrity_view,
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
        "observed_runtime_facts": [row["fact"] for row in traces
                                   if row.get("fact") and row.get("evidence_status") == "ACTIVE"],
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
    payload["runtime_capability_manifest"] = capability_manifest
    payload["strategy_candidates"] = strategy_candidates
    payload["terminal_readiness"] = terminal_readiness
    payload["shared_blackboard"] = build_shared_blackboard(runtime, payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["context_digest"] = digest
    payload["context_snapshot_id"] = "CTX-" + digest[:20].upper()
    return payload


def render_planner_context_snapshot(runtime) -> tuple[str, dict]:
    payload = build_planner_context_snapshot(runtime)
    # Planner and Runtime terminal admission consume the exact same
    # request/epoch-bound candidate registry from this snapshot.
    runtime._last_shared_blackboard_snapshot = dict(payload.get("shared_blackboard") or {})
    runtime._last_strategy_candidate_registry = dict(payload.get("strategy_candidates") or {})
    return ("[SMARTAGENT_PLANNER_CONTEXT_V1]\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            + "\n[/SMARTAGENT_PLANNER_CONTEXT_V1]\n"
            "This is a Runtime-owned, read-only decision snapshot. Candidate tools are NOT grants. "
            "Honor executed/verification states and do not treat UNKNOWN as absence. "
            "Goal Transition Readiness only classifies evidence: Planner decides any changed strategy. "
            "failure_causal_receipts are immutable Runtime projections of exact "
            "current-request Tool Results; they survive intermediate schema repairs. "
            "Use their failure.category and replan.next_evaluation before choosing "
            "a new prerequisite diagnostic, and avoid replaying identical failed "
            "actions. A failure reason is not authorization denial; an unmatched "
            "cause stays UNCLASSIFIED_FAILURE. Never guess paths, credentials "
            "or success. A suggested next_evaluation is not an executable Action "
            "or permission: full Tool Admission and Planner choice remain required. "
            "prerequisite_transition_gate is a causal constraint on Progress, Replan "
            "and INTERRUPTION. If READY_FOR_TOOL_ADMISSION, use its exact "
            "probe_action in a CONTINUE proposal and pass full Tool/Scope/Network "
            "Admission before claiming any probe result. Its existing bound "
            "working_directory is a scoped CWD candidate, not a new file read "
            "permission or network authorization. guidance_previously_issued "
            "is never Evidence. If WORKSPACE_SCOPE_REJECTED, use the exact "
            "Runtime preflight reason to request the minimal missing scope; "
            "if PROBE_EVIDENCE_AVAILABLE_UNRESOLVED, inspect real Tool Result "
            "Evidence and choose a strategy, not a repeated speculative probe. "
            "Progress must not describe an unexecuted prerequisite as completed. "
            "persistent_goal_gaps retains verified missing prerequisites across later failures; "
            "a new rejected read does not erase an earlier confirmed absence. "
            "deployment_path_prerequisites distinguishes an absent deployment "
            "destination from an executable command CWD. A Git clone destination "
            "may be absent, while its parent and write permission must be verified; "
            "remote Git inspection needs an existing admitted CWD, not the absent "
            "deployment destination. For an exact registered GitHub repository "
            "read probe, the narrow supported command form is git ls-remote "
            "https://github.com/OWNER/REPO.git HEAD with GIT_INSPECT, and the "
            "CWD must be an existing admitted directory. A Git clone is not "
            "equivalent to ls-remote and must be planned with separate "
            "destination/parent write readiness. "
            "Absence never grants creation permission. "
            "unified_contract_transaction is the shared Request/Prerequisite/Progress/"
            "Replan/Action/Action Recovery/Evidence/Authorization decision language. "
            "It is request-and-epoch bound, does not grant Scope, create Evidence, "
            "or turn a held Action into an executed operation. Every subsequent "
            "Plan revision must pass the original Runtime admission gates. "
            "canonical_contract_resolution lists exact, bounded, non-authorizing "
            "Progress contract repairs. REWRITE_SAFE_EQUIVALENT means only "
            "a proven structural equivalent was normalized before the full "
            "original admission gates; consult rewrite_rule_ids and expected_keys "
            "for future Planner submissions. NEEDS_INFORMATION requires a "
            "canonical Planner correction or genuine USER_INPUT if the original "
            "path remains ambiguous. REJECT_REPLAN denotes semantic selection "
            "conflict: never change verified paths, authority or Goal to fit "
            "a malformed response. The original scope and human approval "
            "checks are never bypassed. "
            "action_resolution_trace preserves Planner-requested, Runtime-rewritten "
            "or rejected, and actually-dispatched operations separately. "
            "Every changed Action field is a Rewrite, even when only normalized "
            "to canonical form. Consult rewrite_rule_ids, rewrite_rules and "
            "planner_canonical_form for the precise accepted field shapes. "
            "Transport-only canonicalization is also a Rewrite: use "
            "transport rewrite_rule_ids to learn the required v9 envelope, "
            "turn_commit and action_id conventions. "
            "CANONICAL_FORM_UNCLASSIFIED means a change was observed but no "
            "semantic equivalence rule has been proven; never infer a grant. "
            "A REJECTED_PREEXECUTION trace has only admission evidence: it was "
            "NOT executed. A CWD-only rewrite for an exact registered remote "
            "Git HEAD probe never authorizes network or repository access and "
            "is not an equivalent clone/download/deployment operation. "
            "Do not replay a blocked Action unchanged; use its precise "
            "disposition, rewrite details, verified blockers and next strategy. "
            "goal_prerequisite_resolution is an advisory, evidence-grounded ordering: "
            "for explicit standalone creation with confirmed target absence, propose "
            "the Goal Action for ordinary Tool/Scope/Write Admission without gratuitous "
            "source discovery. For Goals requiring an existing source, discover only "
            "within authorized read scope before proposing a deployment. "
            "No candidate grants access or verifies source integrity. "
            "source_candidate_ledger holds provenance-bound leads only; "
            "source_discovery_bootstrap may contain pinned repository artifacts; these "
            "are only E1 references and require separate remote access authorization, "
            "archive acquisition, SHA256/Git blob verification, and normal target "
            "write admission. UNKNOWN remote authorization means NOT_CHECKED, not DENIED. "
            "Use runtime_software_inventory to reuse current-probed bundled Git/Python "
            "and re-probe recorded ProjectSync tools/dependencies before use. "
            "For a known GitHub artifact and AVAILABLE Git, evaluate the existing "
            "run_command/GIT_INSPECT path and applicable Tool/Network/Scope Admission; "
            "a failed authorized probe or a runtime security receipt can identify a "
            "specific approval or denial, but Git availability alone never grants "
            "network or repository access. Do not interrupt solely because an E1 "
            "reference has no authorization receipt; first seek a safe, admitted "
            "readiness check, or cite the concrete policy blocker. "
            "Use an explicitly requested target in preference to the default .venv; "
            "shared .venv changes still require independent authorization. "
            "A registered remote artifact is NOT a local path, "
            "does NOT authorize network access and does NOT satisfy D4 observation. "
            "historical_source_bootstrap reports INDEX_NOT_FOUND versus INDEX_EMPTY, "
            "INDEX_INVALID, INDEX_HIT or recovered leads. Prior E1 user-labeled "
            "paths and E2 historic Runtime receipts are NOT current observations; "
            "they require this Task's independent scoped re-probe. "

            "prior task paths and parent listing names are NOT live source integrity. "
            "Prefer exact scoped candidate reads and validate their normal Tool Admission; "
            "if outside scope, request precise source authorization, never infer parent scope. "
            "source_integrity_resolution requires committed same-epoch source listing "
            "for STRUCTURE_VERIFIED and never infers content hashes or deployment readiness. "
            "Do not choose query_project or another existing-workspace reader for a known absent root. "
            "Discovery may be a valid prerequisite step even when it does not directly complete the Goal. "
            "A staged read-only inspection does not forbid conditionally authorized later repair. "
            "An absent goal target does not authorize a parent/sibling read or a write. "
            "interrupt_readiness offers scoped read candidates only, not admission. "
            "Use USER_INPUT for genuinely missing approval/source location when no scoped read remains. "
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
            "rejected actions merely under new IDs. "
            "shared_blackboard is the canonical current-request/current-epoch read model; "
            "Directory rule DIR-001: when directory_decision_rule.decision is "
            "INSPECT_DIRECTORY_REQUIRED, inspect the exact Goal target; when it is "
            "CREATE_DIRECTORY_REQUIRED and candidate_status=UNTRIED, immediately "
            "propose exactly the create_directory Tool Action for candidate.target "
            "with candidate.strategy_id, using normal Runtime Write/Scope Admission. "
            "Do not ask for source/download before this directory-only creation "
            "unless explicitly required by the original Goal; no parent-path "
            "creation or placeholder files. If decision=ENTER_EXISTING_DIRECTORY, "
            "continue the actual task inside that existing path without repeating "
            "the missing-target probe. Never infer permission from existence or "
            "absence; on denial report the exact Runtime blocker. "
            "Creation success proves the directory exists, NOT that the whole Goal "
            "or any source integrity or file deployment has completed. "
            "each section identifies its authoritative owner and source ledger. Read the "
            "same snapshot rather than reconstructing facts from separate prose fragments. "
            "runtime_capability_manifest describes the canonical tools, schemas, operation "
            "classes and side-effect classes exposed by this interface; it is not "
            "authorization or a Scope grant. strategy_candidates contains Runtime-derived, "
            "goal-relevant alternatives. When status=UNTRIED, select its exact strategy_id "
            "and include strategy_id in the new operational Action; preserve the declared "
            "tool/operation/CWD/target boundary. NEEDS_ACTION_REPAIR means repair the same "
            "candidate's canonical Action; RECONCILIATION_REQUIRED means resolve uncertain "
            "effects first. terminal_readiness reports whether an untried candidate or "
            "unresolved Action prevents INTERRUPT. Do not interrupt while Runtime reports "
            "a runnable untried candidate. A candidate is guidance, not permission: normal "
            "Tool/Scope/Network/Approval Admission and independent Evidence Verification "
            "remain mandatory. Owner metadata is binding: Planner proposals never become "
            "Runtime observations, authorization decisions or execution evidence.", payload)
