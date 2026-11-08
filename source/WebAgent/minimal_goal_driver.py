"""Minimal Core v10 Goal-Driven bridge to existing Runtime Blackboard contracts.

The canonical v9 Goal Gap / Prerequisite / Strategy / Action Readiness modules
remain the calculation owners. This is a bounded, read-only projection and
causal preflight; it neither selects an action nor grants scope/permission.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_MINIMAL_GOAL_DRIVER_V1"
MAX_CONTEXT_CHARS = 6500


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _code(value: object, default: str = "UNKNOWN") -> str:
    import re
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,120}", text) else default


def _path(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 1024:
        return ""
    return text


def _summary_gap(row: Mapping) -> dict:
    return {
        "gap_id": _code(row.get("gap_id"), ""),
        "kind": _code(row.get("kind")),
        "path": _path(row.get("path")),
        "status": _code(row.get("status")),
        "observed_value": row.get("observed_value")
        if type(row.get("observed_value")) is bool else None,
        "observation_status": _code(row.get("observation_status")),
        "evidence_ref": str(row.get("observed_fact_ref") or "")[:160],
        "goal_link": _code(row.get("goal_link")),
        "resolved_by_evidence_ref": str(row.get("resolved_by_evidence_ref") or "")[:160],
    }


def build_goal_driver_context(runtime, goal: str, history: list[dict],
                              rejections: list[dict] | None = None) -> dict:
    """Project real current-epoch Runtime ledgers, with explicit provenance.

    IMPORTANT: this snapshot is not authorization, a goal completion
    certificate, or a replacement for the normal Tool/Scope admission.
    """
    from agent_core.task_requirement import assess_task_requirement
    from agent_core.conditional_goal_policy import conditional_goal_policy
    from agent_core.semantic_capability_resolution import translate_goal_action
    from agent_core.goal_prerequisite_resolution import resolve_goal_prerequisites
    from agent_core.goal_gap_ledger import reconstruct_goal_gaps

    requirement = assess_task_requirement(goal)
    metadata = {
        "schema": SCHEMA,
        "goal_anchor_sha256": _digest(goal),
        "request_id": str(getattr(runtime, "run_id", "") or ""),
        "task_epoch": str(getattr(runtime, "task_epoch", "") or ""),
        "request_route": str(requirement.get("route") or "AMBIGUOUS"),
        "strategy_authority": "PLANNER",
        "evidence_owner": "RUNTIME",
        "scope_authorization_granted": False,
        "tool_admission_required": True,
        "goal_completion_inferred": False,
        "action_history_count": len(history),
    }
    if requirement.get("route") == "ANSWER_ONLY":
        return {**metadata, "snapshot_source": "TASK_REQUIREMENT",
                "next_category": "ANSWER_ONLY_ELIGIBLE",
                "next_decision_hint": "FINISH_WITH_TEXT_NO_TOOL",
                "evidence_refs": [],
                "verified_text_only_requires_no_runtime_operations": True}

    policy = conditional_goal_policy(goal)
    semantic = translate_goal_action(goal)
    base = {
        **metadata,
        "intent": _code(policy.get("intent")),
        "read_only_first": bool(policy.get("read_only_first")),
        "conditional_remediation": bool(policy.get("conditional_remediation")),
        "restrictions": list(policy.get("restrictions") or [])[:8],
        "semantic_status": _code(semantic.get("status")),
        "semantic_tool": _code(semantic.get("canonical_tool"), ""),
    }

    # First prefer the full shared Blackboard; it preserves existing v9
    # causality and strategy candidate ownership without copying v9 transport.
    snapshot = None
    try:
        from agent_core.planner_context_snapshot import build_planner_context_snapshot
        snapshot = build_planner_context_snapshot(runtime)
    except Exception as exc:
        # A snapshot failure must NEVER be treated as a missing prerequisite,
        # scope denial or completion proof. Preserve the typed diagnostic.
        base["snapshot_error_type"] = type(exc).__name__
    if isinstance(snapshot, Mapping):
        gaps = snapshot.get("persistent_goal_gaps") or {}
        prereq = snapshot.get("goal_prerequisite_resolution") or {}
        trans = snapshot.get("prerequisite_transition_gate") or {}
        directory = snapshot.get("directory_decision_rule") or {}
        terminal = snapshot.get("terminal_readiness") or {}
        loop_decision = snapshot.get("action_loop_decision") or {}
        registry = snapshot.get("strategy_candidates") or {}
        snapshot_source = "CANONICAL_SHARED_BLACKBOARD"
        snapshot_id = str(snapshot.get("context_snapshot_id") or "")[:64]
        failure_receipts = snapshot.get("failure_causal_receipts") or []
        scope_readiness = snapshot.get("scope_preflight_readiness") or []
        goal_transition = snapshot.get("goal_transition_readiness") or {}
    else:
        # Fallback is still reconstructed from current trusted ledgers.
        # It never uses model prose or arbitrary paths as observed facts.
        gaps = reconstruct_goal_gaps(runtime)
        prereq = resolve_goal_prerequisites(gaps)
        trans, directory, terminal, loop_decision, registry = {}, {}, {}, {}, {}
        snapshot_source, snapshot_id = "DURABLE_GOAL_GAP_FALLBACK", ""
        failure_receipts, scope_readiness, goal_transition = [], [], {}

    if not isinstance(gaps, Mapping):
        gaps = {}
    if not isinstance(prereq, Mapping):
        prereq = {}
    active = [_summary_gap(row) for row in list(gaps.get("active_gaps") or [])[-5:]
              if isinstance(row, Mapping)]
    observations = [_summary_gap(row) for row in list(gaps.get("gaps") or [])[-6:]
                    if isinstance(row, Mapping)]
    # Never invent a confirmed absence from FAILED/REJECTED/UNKNOWN ToolResult.
    missing = [row for row in active
               if row["observation_status"] == "CONFIRMED"
               and row["observed_value"] is False and row["evidence_ref"]]
    next_category = _code(prereq.get("next_category"), "EVALUATE_GOAL_OR_OBSERVE")
    result = {
        **base,
        "snapshot_source": snapshot_source,
        "snapshot_id": snapshot_id,
        "goal_gap_count": int(gaps.get("active_count") or 0),
        "known_goal_facts": observations,
        "active_goal_gaps": active,
        "confirmed_missing_targets": missing,
        "source_requirement": _code(prereq.get("source_requirement")),
        "next_category": next_category,
        "prerequisite_reason_code": _code(prereq.get("reason_code")),
        "prerequisite_transition_stage": _code(
            trans.get("stage") or trans.get("transition_stage"), "UNKNOWN"),
        "prerequisite_transition_reason": _code(trans.get("reason_code"), ""),
        "action_loop_transition": _code(
            loop_decision.get("required_transition"), "UNKNOWN"),
        "allowed_next_tools": [_code(x) for x in
                               list(loop_decision.get("allowed_next_actions") or [])[:12]],
        "goal_transition_status": _code(goal_transition.get("status"), "UNKNOWN"),
        "terminal_readiness_status": _code(terminal.get("status"), "UNKNOWN"),
        "directory_decision": _code(directory.get("decision"), "UNKNOWN"),
        "recent_rejections": [
            {"tool": _code(row.get("tool")), "reason_code": _code(row.get("reason_code")),
             "repair_mode": _code(row.get("repair_mode")),
             "round": int(row.get("round") or 0)}
            for row in list(rejections or [])[-4:] if isinstance(row, Mapping)
        ],
        "scope_preflight_blockers": [
            {"tool": _code(x.get("tool")), "category": _code(x.get("category")),
             "reason_code": _code(x.get("reason_code")), "required_field":
             _code(x.get("required_field"), ""), "evidence_ref":
             str(x.get("evidence_ref") or "")[:120]}
            for x in list(scope_readiness)[-3:] if isinstance(x, Mapping)
        ],
        "last_actual_action": [
            {"tool": _code(row.get("action", {}).get("tool")),
             "status": _code(row.get("receipt", {}).get("status")),
             "reason": _code(row.get("receipt", {}).get("reason")),
             "evidence_ref": str(row.get("evidence_ref") or "")[:120]}
            for row in history[-3:] if isinstance(row, Mapping)
        ],
        "full_snapshot_used": bool(snapshot),
    }
    if isinstance(directory, Mapping):
        candidate = directory.get("candidate")
        if isinstance(candidate, Mapping):
            result["directory_candidate"] = {
                "tool": _code(candidate.get("tool"), ""),
                "target": _path(candidate.get("target")),
                "status": _code(candidate.get("status")),
                "strategy_id": _code(candidate.get("strategy_id"), ""),
                "normal_admission_required": True,
            }
    if isinstance(registry, Mapping):
        choices = registry.get("candidates") or []
        result["strategy_candidates"] = [
            {"tool": _code(row.get("tool"), ""),
             "status": _code(row.get("status")),
             "route_id": _code(row.get("route_id"), ""),
             "target": _path(row.get("target")),
             "strategy_id": _code(row.get("strategy_id"), "")}
            for row in list(choices)[:4] if isinstance(row, Mapping)
        ]
    if failure_receipts:
        result["failure_causes"] = [
            {"reason": _code((row.get("failure") or {}).get("reason_code"), ""),
             "category": _code((row.get("failure") or {}).get("category"), "")}
            for row in list(failure_receipts)[-3:] if isinstance(row, Mapping)
        ]
    return result


def compact_goal_driver_context(context: dict, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Bounded source-authored projection; no truncating mid-JSON."""
    def encoded(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = encoded(context)
    if len(raw) <= max_chars:
        return raw
    compact = dict(context)
    for key in ("known_goal_facts", "active_goal_gaps", "confirmed_missing_targets",
                "strategy_candidates", "scope_preflight_blockers", "failure_causes",
                "last_actual_action"):
        if isinstance(compact.get(key), list):
            compact[key] = compact[key][-2:]
        if len(encoded(compact)) <= max_chars:
            compact["context_compacted"] = True
            return encoded(compact)
    keep = ("schema", "goal_anchor_sha256", "request_id", "task_epoch",
            "request_route", "intent", "semantic_tool", "source_requirement",
            "next_category", "prerequisite_reason_code", "snapshot_source",
            "goal_gap_count", "confirmed_missing_targets", "recent_rejections")
    compact = {key: compact[key] for key in keep if key in compact}
    compact["context_compacted"] = True
    compact["full_context_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return encoded(compact)


def _same_path(a: object, b: object) -> bool:
    x, y = str(a or "").strip(), str(b or "").strip()
    return bool(x and y and ntpath.normcase(ntpath.normpath(x)) ==
                ntpath.normcase(ntpath.normpath(y)))


_DIRECTORY_REQUIRING_READS = {
    "list_directory": "path",
    "find_file": "root",
    "query_project": "project_root",
    "inspect_project_scope": "workspace",
    "inspect_project_working_set": "workspace",
    "run_command": "working_directory",
}


def gate_known_absent_directory(action: Mapping, context: Mapping) -> dict | None:
    """Block a doomed read only when current-epoch CONFIRMED absence exists.

    Never block inspect_directory, observe_fact or create_directory. A blocked
    read is NOT a new filesystem observation or a grant to create the target.
    """
    field = _DIRECTORY_REQUIRING_READS.get(str(action.get("tool") or ""))
    if field is None:
        return None
    selected = action.get(field)
    for row in context.get("confirmed_missing_targets", []):
        if not isinstance(row, Mapping):
            continue
        if (row.get("observation_status") != "CONFIRMED"
                or row.get("observed_value") is not False
                or not row.get("evidence_ref")
                or not _same_path(selected, row.get("path"))):
            continue
        return {
            "reason_code": "CONFIRMED_MISSING_PREREQUISITE_DIRECTORY",
            "repair_mode": "EVIDENCE_GUIDED_REPLAN",
            "prerequisite_evidence_ref": str(row["evidence_ref"])[:160],
            "next_category": str(context.get("next_category") or "REPLAN"),
            "executed": False,
            "scope_authorization_granted": False,
        }
    return None


def classify_rejection(tool: str, code: str, round_id: int) -> dict:
    """Use existing Action Readiness taxonomy, never turn denial into absence."""
    from agent_core.action_readiness import classify_scope_preflight
    reason = _code(code)
    is_scope = reason.startswith("scope_rejected:")
    if is_scope:
        classification = classify_scope_preflight(
            tool, reason.split(":", 1)[-1], allowed=False)
        repair = classification["eligible_transition"]
    elif any(token in reason.lower() for token in (
        "required", "invalid", "schema", "unknown_field", "unsupported_field",
    )):
        repair = "SCHEMA_REPAIR"
    else:
        repair = "EVIDENCE_GUIDED_REPLAN"
    return {
        "tool": _code(tool), "reason_code": reason,
        "repair_mode": repair, "round": round_id,
        "executed": False, "authorizes_action": False,
    }
