"""CCRC V1: bounded cross-contract diagnostics for one unadmitted decision.

This module never edits Planner actions, executes tools, grants authorization,
creates evidence, resets BoundedRepairBudget, or selects a new strategy.
Every replacement must pass the ordinary full Runtime admission chain.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re

SCHEMA = "SMARTAGENT_CROSS_CONTRACT_REPAIR_V1"
FIELD_REPAIR = "FIELD_REPAIR"
ACTION_RECONSTRUCTION = "ACTION_RECONSTRUCTION"
REPLAN_REQUIRED = "REPLAN_REQUIRED"
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
_RELEVANT = {
    "invalid_progress_payload", "invalid_progress_envelope",
    "run_command_operation_mismatch", "run_command_verification_contract_required",
    "inline_script_too_complex", "declared_next_action_missing",
    "declared_next_action_mismatch", "command_repair_identity_changed",
    "command_action_reconstruction_required", "pending_actions_not_preserved",
    "MISSING_OR_INVALID_FIELD", "unexpected_field",
    "query_project_structured_queries_required",
}


def _code(value: object, fallback: str = "INVALID") -> str:
    candidate = str(value or "").strip()
    return candidate if _IDENTIFIER.fullmatch(candidate) else fallback


def inspect_cross_contract(
    calls: Sequence[Mapping] | None,
    diagnostics: Sequence[Mapping] | None,
    *,
    declared_tools: Sequence[str] = (),
) -> dict:
    """Batch safe, independently visible defects without changing admission.

    The original Progress may be invalid, so lexical next_action checking here
    is strictly a hint, not a replacement for validate_model_progress().
    """
    issues: set[tuple[str, str, str]] = set()
    structural_reconstruction = False
    command_involved = False

    def add(origin: str, field: str, code: str) -> None:
        issues.add((_code(origin), _code(field), _code(code)))

    for diagnostic in list(diagnostics or [])[:32]:
        if not isinstance(diagnostic, Mapping):
            continue
        reason = _code(diagnostic.get("reason"), "protocol_rejected")
        add("ADMISSION", "contract", reason)
        for row in list(diagnostic.get("field_issues") or [])[:32]:
            if isinstance(row, Mapping):
                field = _code(row.get("field") or row.get("path"), "field")
                add("PROGRESS", field, _code(row.get("code"), reason))
            elif isinstance(row, str):
                add("PROGRESS", _code(row, "field"), reason)
        ready_values = [diagnostic.get("command_repair")]
        ready_values.extend(list(diagnostic.get("command_repair_diagnostics") or [])[:16])
        for ready in ready_values:
            if not isinstance(ready, Mapping):
                continue
            command_involved = True
            structural_reconstruction |= ready.get("mode") == ACTION_RECONSTRUCTION
            for row in list(ready.get("field_issues") or [])[:32]:
                if isinstance(row, Mapping):
                    add("COMMAND", row.get("field", "field"), row.get("code", "invalid"))
        if reason == "query_project_structured_queries_required":
            add("QUERY", "queries", "explicit_query_operation_required")
        if reason == "run_command_operation_mismatch":
            add("COMMAND", "operation", "operation_command_mismatch")
        if reason == "run_command_verification_contract_required":
            add("COMMAND", "verify", "verification_contract_required")
        if reason in {"inline_script_too_complex", "command_action_reconstruction_required"}:
            structural_reconstruction = True

    operational = []
    progress = None
    for call in list(calls or [])[:64]:
        if not isinstance(call, Mapping):
            continue
        name = _code(call.get("tool"), "UNKNOWN_TOOL")
        if name == "report_progress":
            if progress is None:
                progress = call
        elif name not in {"turn_commit", "final_response"}:
            operational.append(call)
            command_involved |= name == "run_command"

    declared = sorted({_code(x) for x in declared_tools if _code(x) != "INVALID"})
    actual = sorted({_code(x.get("tool")) for x in operational})
    if progress is not None and str(progress.get("decision", "")).upper() == "CONTINUE":
        if declared and not set(declared).intersection(actual):
            add("CONSISTENCY", "next_action", "declared_next_action_mismatch"
                if actual else "declared_next_action_missing")

    # Always preserve the primary admission reason even if no other field is
    # classifiable. Never include raw command, next_action, or evidence text.
    rows = [
        {"origin": origin, "field": field, "code": code}
        for origin, field, code in sorted(issues)[:48]
    ]
    # A run_command in a rejected turn is not by itself a contract defect.
    # In particular, Scope/Authorization/Preflight denials belong to their
    # original safety gates and must never consume CCRC's format window.
    relevant = any(
        row["origin"] in {"PROGRESS", "CONSISTENCY", "COMMAND", "QUERY"}
        or row["code"] in _RELEVANT for row in rows
    )
    return {
        "schema": SCHEMA,
        "relevant": bool(relevant),
        "issue_count": len(issues),
        "field_issues": rows,
        "declared_next_tools": declared[:12],
        "actual_operational_tools": actual[:12],
        "command_involved": bool(command_involved),
        "structural_reconstruction": bool(structural_reconstruction),
        "authorization_granted": False,
        "executed": False,
        "budget_reset": False,
    }


class CrossContractRepairCoordinator:
    """One request-bound transaction; Action-ID changes cannot reset limits.

    A newly admitted decision ends the current transaction. A rejected
    replacement or Progress patch remains inside the same transaction.
    """
    def __init__(self, *, request_id: str, task_epoch: str, goal: str,
                 max_field_rounds: int = 2, max_reconstruction_rounds: int = 1):
        if max_field_rounds < 1 or max_reconstruction_rounds < 1:
            raise ValueError("ccrc_repair_limits_invalid")
        self.request_id = str(request_id)
        self.task_epoch = str(task_epoch)
        self.goal_digest = hashlib.sha256(str(goal).encode("utf-8")).hexdigest()
        self.max_field_rounds = int(max_field_rounds)
        self.max_reconstruction_rounds = int(max_reconstruction_rounds)
        self.transaction_seq = 0
        self.field_rounds = 0
        self.reconstruction_rounds = 0
        self.reject_rounds = 0
        self.nonconverging_rounds = 0
        self.previous_issues: set[tuple[str, str, str]] = set()
        self.convergence_generation = 0
        self.active = False
        self.replan_required = False
        self.mode = ""
        self.action_ids: set[str] = set()

    def observe(self, calls: Sequence[Mapping] | None,
                diagnostics: Sequence[Mapping] | None,
                *, declared_tools: Sequence[str] = ()) -> dict | None:
        inspected = inspect_cross_contract(
            calls, diagnostics, declared_tools=declared_tools,
        )
        if not inspected["relevant"] and not self.active:
            return None
        if not self.active:
            self.active = True
            self.transaction_seq += 1
            self.field_rounds = 0
            self.reconstruction_rounds = 0
            self.reject_rounds = 0
            self.nonconverging_rounds = 0
            self.previous_issues = set()
            self.convergence_generation = 0
            self.replan_required = False
        issues = {
            (row["origin"], row["field"], row["code"])
            for row in inspected["field_issues"]
        }
        # Ignore the generic ADMISSION row when measuring semantic field
        # convergence; new defect categories cannot masquerade as progress.
        field_issues = {row for row in issues if row[0] != "ADMISSION"}
        previous_fields = {row for row in self.previous_issues if row[0] != "ADMISSION"}
        resolved = previous_fields - field_issues
        introduced = field_issues - previous_fields
        converging = bool(self.reject_rounds and resolved and not introduced
                          and field_issues < previous_fields)
        if converging:
            self.convergence_generation += 1
        if self.reject_rounds:
            self.nonconverging_rounds = 0 if converging else self.nonconverging_rounds + 1
        self.previous_issues = issues
        self.reject_rounds += 1
        for call in list(calls or []):
            if isinstance(call, Mapping) and call.get("tool") not in {
                    "report_progress", "turn_commit", "final_response"}:
                aid = call.get("action_id")
                if isinstance(aid, str) and aid.strip() and len(self.action_ids) < 32:
                    self.action_ids.add(aid)

        if self.replan_required:
            mode = REPLAN_REQUIRED
        elif inspected["structural_reconstruction"]:
            if self.reconstruction_rounds:
                mode = REPLAN_REQUIRED
            else:
                mode = ACTION_RECONSTRUCTION
        elif (self.field_rounds < self.max_field_rounds
              and self.nonconverging_rounds < 3):
            # An ACTION_RECONSTRUCTION can expose previously unvalidated
            # required fields. Allow field-only repair without immediately
            # escalating to another reconstruction/replan.
            mode = FIELD_REPAIR
        elif (not self.reconstruction_rounds
              and inspected["actual_operational_tools"]):
            mode = ACTION_RECONSTRUCTION
        else:
            mode = REPLAN_REQUIRED

        if mode == FIELD_REPAIR:
            self.field_rounds += 1
        elif mode == ACTION_RECONSTRUCTION:
            self.reconstruction_rounds += 1
        else:
            self.replan_required = True
        self.mode = mode

        return {
            **inspected,
            "mode": mode,
            "transaction_seq": self.transaction_seq,
            "request_id": self.request_id,
            "task_epoch": self.task_epoch,
            "goal_anchor_sha256": self.goal_digest,
            "rejected_rounds": self.reject_rounds,
            "field_rounds": self.field_rounds,
            "field_rounds_limit": self.max_field_rounds,
            "reconstruction_rounds": self.reconstruction_rounds,
            "reconstruction_rounds_limit": self.max_reconstruction_rounds,
            "nonconverging_rounds": self.nonconverging_rounds,
            "resolved_issue_count": len(resolved) if self.reject_rounds > 1 else 0,
            "introduced_issue_count": len(introduced) if self.reject_rounds > 1 else 0,
            "converging": converging,
            "convergence_generation": self.convergence_generation,
            "tracked_action_count": len(self.action_ids),
            "replan_required": self.replan_required,
            "runtime_strategy_selected": False,
            "full_admission_required": True,
        }

    def admitted(self) -> dict | None:
        """Close after full admission only, never after a malformed reply."""
        if not self.active:
            return None
        snapshot = {
            "schema": SCHEMA,
            "transaction_seq": self.transaction_seq,
            "request_id": self.request_id,
            "admission_passed": True,
            "rejected_rounds": self.reject_rounds,
            "authorization_granted": False,
            "budget_reset": False,
        }
        self.active = False
        self.replan_required = False
        self.mode = ""
        self.action_ids.clear()
        self.previous_issues.clear()
        return snapshot


def repair_instruction(decision: Mapping) -> str:
    """Bounded Planner guidance, not permission to run a proposed action."""
    mode = decision.get("mode")
    progress_mode = str(decision.get("progress_mode") or "FULL").upper()
    full = progress_mode != "PATCH"
    progress_guidance = (
        "No Progress plan has passed Admission. Use a COMPLETE FULL "
        "report_progress with progress_update=FULL, total_steps, "
        "current_step, steps, base_evaluation, completion_contract, "
        "decision=CONTINUE, outcome=PENDING, current_focus, next_action, "
        "matched_condition or a valid matched_condition_id, evidence_refs, "
        "and decision_reason. No existing plan fields can be inherited. "
        "Select the matched condition from completion_contract.in_progress. "
        if full else
        "An already accepted Progress plan exists. Use PATCH to inherit "
        "unchanged plan fields; supply fresh decision and evidence fields. "
    )
    summary = {
        key: decision[key] for key in (
            "schema", "mode", "transaction_seq", "goal_anchor_sha256",
            "field_issues", "declared_next_tools", "actual_operational_tools",
            "rejected_rounds", "field_rounds", "field_rounds_limit",
            "reconstruction_rounds", "reconstruction_rounds_limit",
            "converging", "progress_mode", "accepted_plan",
            "held_unadmitted_action_ids",
        ) if key in decision
    }
    prefix = "[CROSS_CONTRACT_REPAIR]\n" + json.dumps(
        summary, ensure_ascii=False, separators=(",", ":")
    ) + "\n"
    if mode == FIELD_REPAIR:
        query_only = bool(decision.get("field_issues")) and all(
            row.get("origin") in {"QUERY", "ADMISSION"}
            for row in decision["field_issues"])
        instruction = (
            "Repair ONLY query_project.queries[] with explicit canonical "
            "operation objects. " + progress_guidance +
            "No operation/path guessing, no new Scope, no fake Action Evidence. "
            "Full admission remains mandatory."
            if query_only else
            "Repair Progress, Query and next_action as ONE pending decision. "
            + progress_guidance +
            "Correct only the listed canonical issues; preserve Action semantics, "
            "evidence and original Goal. Never replay executed actions or "
            "broaden scope."
        )
    elif mode == ACTION_RECONSTRUCTION:
        instruction = (
            "STOP field-only patches. Planner must reconstruct an authorized "
            "canonical Action/Progress pair; use NEW action_id and "
            "replaces_action_id when replacing an unexecuted deferred action. "
            "Keep the original Goal and permissions; short script invocation "
            "is preferable to a complex inline command. " + progress_guidance +
            "Full admission required."
        )
    else:
        instruction = (
            "CCRC field and reconstruction windows are exhausted. STOP schema "
            "patch cycles; hand back to Goal-Anchored strategy replan using "
            "existing Runtime evidence and original authorized scope. Planner "
            "chooses the next strategy, not CCRC. " + progress_guidance +
            "No new goal, no inferred permission, no fabricated ToolResult. "
            "The same cumulative BoundedRepair/GAR budgets remain active."
        )
    held = list(decision.get("held_unadmitted_action_ids") or [])
    if mode == FIELD_REPAIR and held:
        instruction += (
            " Runtime has already held an UNEXECUTED, schema-valid Action "
            "batch with exact action_ids=" + json.dumps(held) + ". "
            "For a Progress-only repair, declare every retained Tool in "
            "next_action and submit only report_progress + turn_commit; "
            "Runtime will deterministically restore the *identical* held "
            "Actions for complete normal admission, not treat them as executed "
            "evidence. Do not invent or rename Action IDs. "
        )
    return prefix + instruction + "\n[/CROSS_CONTRACT_REPAIR]"
