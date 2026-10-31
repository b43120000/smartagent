#!/usr/bin/env python3
"""P2 diagnostic evidence extractor. Never package raw runtime JSONL.

Only allowlisted scalar fields leave the runtime log. No user messages,
response bodies, URLs, stack traces, environment or serialized tools.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from agent_core.paths import log_root

EVENTS = frozenset({
    "request_started", "request_captured", "planner_response_received",
    "browser_response_completed", "browser_response_failed",
    "ack_accepted", "tool_started", "tool_completed",
    "action_evidence_recorded", "action_loop_state_updated",
    "runtime_terminal_accepted", "terminal_evidence_bound",
    "terminal_evidence_identity_audit", "terminal_decision_normalized",
    "request_local_loop_completed", "request_completed", "request_failed",
    "task_result_contract", "task_progress_updated",
    "interruption_render_unavailable", "action_dependency_blocked",
    "gar_safe_pause", "bounded_repair_observed",
    "protocol_rejected", "protocol_loop_paused",
    "controller_started", "controller_ready",
    "webagent_initial_session_settle_completed",
    "browser_send_started", "p2_request_ingress",
    "planner_round_started", "protocol_transport_interrupted",
})
FIELDS = frozenset({
    "request_id", "task_id", "round", "action_id", "tool",
    "status", "result_status", "state", "outcome", "terminal_outcome",
    "action_count", "error_type", "evidence_status", "protocol_version",
    "commit_state", "response_stable", "action_executed", "ack_id",
    "source_interface", "request_phase", "completion_status",
    "session_mode", "source_kind", "request_chars", "request_sha256",
    "hybrid_eligible",
})
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# Convert runtime browser phase text to a fixed enum. Never expose an
# arbitrary model/user string as a source label or request text.
KNOWN_BROWSER_STAGES = {
    "協議初始化": "SESSION_BOOTSTRAP",
    "協議附著確認": "SESSION_ATTACH",
    "smartagent_tool 規劃回合": "TASK_PLANNER",
    "最終自然語言 ACK": "FINAL_RENDER_ACK",
}
def _within_latest_run(row, start):
    if start is None:
        return True
    try:
        value = row.get("utc") or row.get("timestamp")
        if not value:
            return False
        # P2 turn/writer rows are timezone-aware UTC; controller log rows
        # include local offset. Do not compare raw ISO strings.
        from datetime import datetime
        moment = datetime.fromisoformat(str(value))
        return moment >= datetime.fromisoformat(start)
    except (ValueError, TypeError):
        return False


# Fixed classifications only. Runtime errors, diagnostics, model text and
# arbitrary reason strings are NEVER copied verbatim.
KNOWN_REASONS = {
    "missing_smartagent_tool_envelope": "MISSING_TOOL_ENVELOPE",
    "missing_turn_commit": "MISSING_TURN_COMMIT",
    "invalid_json": "INVALID_JSON",
    "json_parse_error": "JSON_PARSE_ERROR",
    "protocol_rejected": "PROTOCOL_REJECTED",
    "bounded_repair_exhausted_without_sufficient_new_evidence": "BOUNDED_REPAIR_EXHAUSTED",
    "semantic_stagnation": "SEMANTIC_STAGNATION",
    "request elapsed soft deadline": "REQUEST_ELAPSED_SOFT_DEADLINE",
    "Repair elapsed budget exhausted": "REPAIR_ELAPSED_BUDGET_EXHAUSTED",
    "composer_chunk_readback_failed": "COMPOSER_READBACK_FAILED",
}
# Exact, Source-defined admission rejection identifiers only, no arbitrary
# user/model prose, paths or free-form error detail.
PROTOCOL_ADMISSION_REASONS = frozenset("""
evidence_file_exists_boolean_required required_transition_action_missing pending_action_requires_observation blocked_action_selected terminal_transition_requires_final_response blocked_semantic_action_repeated progress_wrapper_invalid progress_wrapper_conflict declared_next_action_missing terminal_semantics_complete_but_decision_was_continue command_repair_target_not_pending command_repair_identity_changed invalid_or_duplicate_action_id unresolved_action_repairs action_id_payload_mismatch action_replacement_not_deferred action_repair_count_mismatch missing_turn_commit progress_count_invalid pending_actions_not_preserved query_project_structured_queries_required edit_plan_prerequisite_unsatisfied edit_plan_prerequisite_action_mismatch task_plan_repair_without_runtime_latch task_plan_repair_latch_active task_plan_repair_action_required task_plan_repair_payload_mismatch web_planner_project_read_requires_query_project run_command_verification_contract_required run_command_working_directory_rejected run_command_shell_syntax_mismatch final_response_not_exclusive spec_invalid_protocol_evidence_ref_required objective_unknown_interrupt_not_safe_or_unbound path_independent_command_bound_workspace_already_available missing_context_requires_runtime_read expected_failure_not_bound_to_success_contract final_step_continue_requires_action runtime_evidence_ref_unknown terminal_decision_missing_action_result_evidence continuation_execution_reconciliation_required verifies_action_id_unknown complete_decision_requires_final_response continue_decision_forbids_final_response interrupt_decision_forbids_action active_action_not_complete partial_not_supported_or_recoverable_work verification_evidence_action_required terminal_success_verification_missing success_contradicts_runtime_verification failure_not_supported_by_runtime_evidence failed_fact_unbound_to_declared_success_condition unsupported_tool duplicate_action_id delete_target_requires_resolution invalid_progress_payload action_dependency_unsatisfied progress_repair_stalled_no_new_read_evidence
""".split())

def _reason_categories(value):
    if not isinstance(value, str) or not value:
        return []
    categories = {code for marker, code in KNOWN_REASONS.items()
                  if marker.lower() in value.lower()}
    # Whitelist exact tokens only. A diagnostic containing user prose cannot
    # become an exported reason_code, even if it contains a familiar word.
    if value in PROTOCOL_ADMISSION_REASONS:
        categories.add("ADMISSION_" + value.upper())
    return sorted(categories)


def _safe_field(key: str, value):
    if key in {"round", "action_count", "protocol_version", "request_chars"}:
        return value if type(value) is int and 0 <= value <= 1000000 else None
    if key in {"response_stable", "action_executed", "hybrid_eligible"}:
        return value if type(value) is bool else None
    if key == "request_sha256":
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
    if key == "source_kind":
        return value if value in {"WEB_UI", "LOCAL_TEST", "BOOTSTRAP_PROVISION"} else None
    if key == "session_mode":
        return value if value in {"BOOTSTRAP", "SESSION_ATTACH"} else None
    if isinstance(value, str) and SAFE_TOKEN.fullmatch(value):
        return value
    return None


def _read_jsonl(path: Path, limit: int):
    if not path.is_file() or path.is_symlink():
        return []
    result = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if len(line) > 2 * 1024 * 1024:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                result.append(record)
    return list(result)


def _safe_turns(records: list[dict]):
    output = []
    keys = {
        "utc", "stage", "request_id", "round", "scope_present",
        "scope_id_sha256", "request_user_bound", "request_user_id_sha256",
        "assistant_id_sha256", "assistant_ordinal", "assistant_count_before",
        "user_count_before", "assistant_count_observed", "user_count_observed",
        "response_chars", "response_sha256",
    }
    for row in records:
        if row.get("schema") != "P2_READONLY_TURN_V1":
            continue
        clean = {k: row[k] for k in keys if k in row}
        clean["schema"] = "P2_READONLY_TURN_V1"
        output.append(clean)
    return output


def make_p2_evidence(root: Path) -> dict[str, bytes]:
    """Return named, bounded safe JSON files for the existing ZIP collector."""
    root = Path(root).resolve()
    logs = log_root(root)
    trace_path = logs / "p2-readonly" / "turn_readback.jsonl"
    writer_path = logs / "p2-readonly" / "hybrid_writer.jsonl"
    if not trace_path.is_file() and not writer_path.is_file():
        return {}
    raw_events = _read_jsonl(logs / "webagent_runtime.jsonl", 2500)
    # Always isolate the latest controller process; don't let historical
    # failed requests masquerade as the new P2 outcome.
    latest_start_idx = next(
        (i for i in range(len(raw_events) - 1, -1, -1)
         if raw_events[i].get("event") == "controller_started"),
        None,
    )
    latest_start = None
    if latest_start_idx is not None:
        latest_start = raw_events[latest_start_idx].get("timestamp")
        raw_events = raw_events[latest_start_idx:]
    turns = _safe_turns([
        row for row in _read_jsonl(trace_path, 120)
        if _within_latest_run(row, latest_start)
    ])
    events = []
    for row in raw_events:
        kind = str(row.get("event", ""))
        if kind not in EVENTS:
            continue
        result = {"event": kind}
        if kind in {"browser_send_started", "browser_response_completed",
                    "browser_response_failed"}:
            result["browser_stage"] = KNOWN_BROWSER_STAGES.get(
                row.get("stage"), "OTHER_OR_UNKNOWN",
            )
        if kind == "protocol_rejected":
            diagnostics = row.get("diagnostics")
            if isinstance(diagnostics, list):
                is_parse = any(
                    isinstance(diag, dict) and
                    diag.get("marker") == "[WEBAGENT_V8_PARSE_ERROR]"
                    for diag in diagnostics
                )
                result["rejection_stage"] = (
                    "TRANSPORT_PARSE" if is_parse else "ACK_OR_CONTRACT_ADMISSION"
                )
                result["diagnostic_count"] = min(len(diagnostics), 1000)
        ts = row.get("timestamp")
        if isinstance(ts, str) and len(ts) <= 64:
            result["timestamp"] = ts
        for key in FIELDS:
            if key in row:
                value = _safe_field(key, row[key])
                if value is not None:
                    result[key] = value
        if kind in {"protocol_rejected", "bounded_repair_observed",
                    "protocol_loop_paused", "request_failed"}:
            # Whitelist known runtime reasons, or compare non-reversible hashes
            # of unknown reason fields. Do not serialize exception details.
            reason_candidates = []
            if isinstance(row.get("reason"), str):
                reason_candidates.append(row["reason"])
            if kind == "request_failed" and isinstance(row.get("error"), str):
                reason_candidates.append(row["error"])
            if isinstance(row.get("reason_codes"), list):
                reason_candidates.extend(x for x in row["reason_codes"]
                                         if isinstance(x, str))
            if isinstance(row.get("diagnostics"), list):
                reason_candidates.extend(x.get("reason") for x in row["diagnostics"]
                                         if isinstance(x, dict) and isinstance(x.get("reason"), str))
            categories = sorted(set(code for reason in reason_candidates
                                    for code in _reason_categories(reason)))
            if categories:
                result["reason_categories"] = categories
            elif reason_candidates:
                result["reason_categories"] = ["UNCLASSIFIED"]
            if reason_candidates:
                result["reason_sha256"] = [
                    hashlib.sha256(x.encode("utf-8")).hexdigest()
                    for x in reason_candidates[:8]
                ]
        events.append(result)
    # Controller logs can include several tasks. Only identical Request IDs
    # to recorded turns are treated as scoped; unscoped events are diagnostic.
    ids = {r.get("request_id") for r in turns if r.get("request_id")}
    if ids:
        events = [r for r in events if not r.get("request_id")
                  or r["request_id"] in ids]
        started = [r["timestamp"] for r in events
                   if r["event"] == "request_started"
                   and r.get("request_id") in ids and r.get("timestamp")]
        if started:
            # Controller timestamps are consistently local ISO8601. Limit
            # legacy unscoped browser events to this request's start onward.
            begin = min(started)
            events = [r for r in events if r.get("request_id")
                      or not r.get("timestamp") or r["timestamp"] >= begin]
    accepted = sorted({r.get("request_id") for r in events
                       if r["event"] == "runtime_terminal_accepted"
                       and r.get("request_id")})
    completed = sorted({r.get("request_id") for r in events
                        if r["event"] == "request_completed"
                        and r.get("request_id")})
    failed = sorted({r.get("request_id") for r in events
                     if r["event"] == "request_failed"
                     and r.get("request_id")})
    summary = {
        "schema": "P2_TERMINAL_SUMMARY_V1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": ("FAILED_REQUEST_PRESENT" if failed else
                   "ACCEPTED_AND_COMPLETED" if accepted and set(accepted).issubset(completed)
                   else "UNKNOWN_NOT_FULLY_CORRELATED"),
        "accepted_request_ids": accepted, "completed_request_ids": completed,
        "failed_request_ids": failed, "admission_event_count": len(events),
        "turn_observations": len(turns),
        "latest_controller_started": latest_start,
        "scope": "LATEST_CONTROLLER_RUN" if latest_start is not None else "NO_CONTROLLER_BOUNDARY_AVAILABLE",
        "limits": ["Events are a bounded window; no full history proof",
                   "Missing records must be treated as UNKNOWN",
                   "This does not assert tool effects or admission PASS on its own"],
    }
    # Hybrid writer log is created from controlled scalar-only instrumentation;
    # apply a second explicit allowlist as defense in depth.
    writers = []
    for row in _read_jsonl(writer_path, 50):
        if not _within_latest_run(row, latest_start):
            continue
        if row.get("schema") != "COMPOSER_HYBRID_WRITER_V1":
            continue
        names = ("utc", "mode", "prefix_chars", "tail_units", "prompt_chars",
                 "elapsed_seconds", "prompt_sha256", "status")
        writers.append({k: row[k] for k in names if k in row})
    objects = {
        "p2_turn_observation.json": {"schema": "P2_TURN_OBSERVATIONS_V1", "records": turns},
        "p2_admission_trace.json": {"schema": "P2_ADMISSION_TRACE_V1", "events": events,
                                    "raw_runtime_log_included": False},
        "p2_terminal_summary.json": summary,
        "p2_writer_trace.json": {"schema": "P2_WRITER_TRACE_V1", "records": writers},
    }
    return {name: json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            for name, data in objects.items()}
