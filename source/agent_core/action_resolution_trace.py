"""Action resolution: proposal -> bounded rewrite/admission -> real evidence.

Runtime owns admission receipts. The model's submitted action is immutable
audit input; no preexecution rejection can claim execution or new authority.
A safe Git remote read may use the already-bound, separately admitted CWD;
this does not grant read access to arbitrary local paths, network permission,
archive acquisition or deployment writes.
"""
from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Mapping
from copy import deepcopy

from .runtime_fact_ledger import _public_action

SCHEMA = "SMARTAGENT_ACTION_RESOLUTION_TRACE_V1"
PREREQ_SCHEMA = "SMARTAGENT_DEPLOYMENT_PATH_PREREQUISITES_V1"
MAX_RECENT = 16

_GIT_REMOTE_READ = re.compile(
    r"git(?:\.exe)?\s+ls-remote\s+"
    r"https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})(?:\.git)?\s+HEAD",
    re.IGNORECASE,
)
_CWD_BLOCKERS = (
    "command_working_directory_not_directory:",
    "command_working_directory_outside_request_scope:",
    "path_independent_command_cwd_outside_bound_workspace:",
)


def exact_remote_git_read_repository(command: object) -> str:
    """A single literal command: no interpolation, extra flags or shell chain."""
    if not isinstance(command, str):
        return ""
    match = _GIT_REMOTE_READ.fullmatch(command.strip())
    if not match:
        return ""
    repo = match.group(1)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return repo.casefold()


def registered_remote_git_read(action: Mapping, artifact_view: Mapping | None) -> bool:
    if (action.get("tool") != "run_command"
            or action.get("operation") != "GIT_INSPECT"):
        return False
    repo = exact_remote_git_read_repository(action.get("command"))
    if not repo:
        return False
    refs = list((artifact_view or {}).get("artifact_references") or [])[:8]
    return any(
        isinstance(item, Mapping)
        and item.get("kind") == "REMOTE_ARTIFACT_REFERENCE"
        and str(item.get("repository", "")).casefold() == repo
        for item in refs
    )


def _same_path(a: object, b: object) -> bool:
    x, y = str(a or "").strip(), str(b or "").strip()
    if not x or not y:
        return False
    return ntpath.normcase(ntpath.normpath(x)) == ntpath.normcase(ntpath.normpath(y))


def confirmed_target_absence(goal_gaps: Mapping | None, target: str) -> str:
    """Return exact current-epoch observation ref; no filesystem inference."""
    if not target or not isinstance(goal_gaps, Mapping):
        return ""
    for row in list(goal_gaps.get("active_gaps") or [])[:32]:
        if (isinstance(row, Mapping)
                and _same_path(row.get("path"), target)
                and row.get("observation_status") == "CONFIRMED"
                and row.get("observed_value") is False
                and row.get("status") == "UNRESOLVED"
                and isinstance(row.get("observed_fact_ref"), str)
                and row.get("observed_fact_ref")):
            return str(row["observed_fact_ref"])[:160]
    return ""


def deployment_path_prerequisites(
    target_contract: Mapping | None, goal_gaps: Mapping | None,
) -> dict:
    """Differentiate read-only command CWD and new deployment destination."""
    contract = target_contract if isinstance(target_contract, Mapping) else {}
    path = str(contract.get("requested_target") or "")
    if contract.get("binding_status") != "BOUND":
        path = ""
    evidence = confirmed_target_absence(goal_gaps, path)
    return {
        "schema": PREREQ_SCHEMA,
        "target_path": path[:1024],
        "target_observation_status": (
            "CONFIRMED_ABSENT" if evidence else "NOT_CONFIRMED_ABSENT"
        ),
        "target_absence_evidence_ref": evidence,
        "missing_target_is_not_valid_command_cwd": bool(evidence),
        "git_remote_read_command_cwd_requirement": (
            "EXISTING_BOUND_CWD_WITH_NORMAL_ADMISSION"
        ),
        "git_clone_destination_may_be_absent": True,
        "git_clone_requires_existing_authorized_writable_parent": True,
        "remote_reference_requires_separate_access_and_integrity": True,
        "deployment_requires_separate_write_admission": True,
        "absence_does_not_authorize_creating_target": True,
        "source_acquisition_before_deployment_mutation": True,
    }


def propose_equivalent_bound_cwd_rewrite(
    action: Mapping, *, original_cwd_error: str,
    bound_workspace: str, target_contract: Mapping | None,
    goal_gaps: Mapping | None, artifact_view: Mapping | None,
) -> dict | None:
    """Return a CWD-only *candidate*, never an admitted or executed action.

    No rewriting of command, operation, target, repository, or arguments.
    Normal Tool/Scope/Network Admission MUST separately accept the candidate.
    """
    if not isinstance(action, Mapping) or not any(
        str(original_cwd_error).startswith(prefix) for prefix in _CWD_BLOCKERS
    ):
        return None
    if not registered_remote_git_read(action, artifact_view):
        return None
    contract = target_contract if isinstance(target_contract, Mapping) else {}
    target = str(contract.get("requested_target") or "")
    evidence = confirmed_target_absence(goal_gaps, target)
    if not evidence or not bound_workspace:
        return None
    original_cwd = str(action.get("working_directory") or "")
    # Either the known absent deployment destination, or its existing parent
    # rejected *for the original request's command CWD*. Do not use a random
    # unapproved host path as a rewrite trigger.
    same_target = _same_path(original_cwd, target)
    parent = ntpath.dirname(ntpath.normpath(target))
    parent_scope_rejection = (
        str(original_cwd_error).startswith(
            "command_working_directory_outside_request_scope:"
        ) and _same_path(original_cwd, parent)
    )
    if not (same_target or parent_scope_rejection):
        return None
    if _same_path(original_cwd, bound_workspace):
        return None
    candidate = deepcopy(dict(action))
    candidate["working_directory"] = str(bound_workspace)
    return {
        "candidate_action": candidate,
        "rewrite_reason_code": (
            "KNOWN_ABSENT_DEPLOYMENT_TARGET_NOT_COMMAND_CWD"
            if same_target else "DEPLOYMENT_PARENT_OUTSIDE_COMMAND_REQUEST_SCOPE"
        ),
        "rewrite_changed_fields": ["working_directory"],
        "preserved_semantics": "EXACT_REMOTE_GIT_READ_SAME_REPOSITORY_AND_HEAD",
        "target_absence_evidence_ref": evidence,
        "authorization_granted": False,
        "execution_started": False,
    }


REWRITE_RULE_SCHEMA = "SMARTAGENT_ACTION_REWRITE_RULES_V1"


def _rewrite_details(original: Mapping, canonical: Mapping, info: Mapping | None) -> dict:
    """Explain every actual payload difference without inventing a semantic rule.

    Known transformations have stable rule IDs. Unknown normalizations are
    explicitly unclassified, and the admitted canonical form is the only
    recommended example. Never surface arbitrary command/script contents.
    """
    before = dict(original) if isinstance(original, Mapping) else {}
    after = dict(canonical) if isinstance(canonical, Mapping) else {}
    notes = info if isinstance(info, Mapping) else {}
    fields = sorted(
        key for key in set(before).union(after)
        if key != "action_id" and (
            (key in before) != (key in after)
            or before.get(key) != after.get(key)
        )
    )
    if not fields:
        return {
            "changed_fields": [], "rewrite_reason_code": "",
            "rewrite_rule_ids": [], "rewrite_rules": [],
            "rewrite_rule_schema": REWRITE_RULE_SCHEMA,
            "planner_canonical_form": None,
        }
    rules = []
    explicit_fields = set(notes.get("rewrite_changed_fields") or [])
    paired_path = (
        "path" in fields and "paths" in fields
        and isinstance(before.get("path"), str)
        and after.get("paths") == [before["path"]]
    )
    if paired_path:
        rules.append({
            "rule_id": "SINGULAR_PATH_TO_PATHS_LIST",
            "input_fields": ["path"], "output_fields": ["paths"],
            "rule": "Use paths as a one-element list containing the exact original path.",
            "from_type": "str", "to_type": "list",
        })
    for field in fields:
        if paired_path and field in {"path", "paths"}:
            continue
        old, new = before.get(field), after.get(field)
        reason = ""
        description = ""
        if field in explicit_fields and notes.get("rewrite_reason_code"):
            reason = str(notes["rewrite_reason_code"])[:120]
            description = (
                "Runtime's explicitly evidenced, independently admitted "
                "field rewrite; preserve all other command and target semantics."
            )
        elif isinstance(new, list) and old is not None and new == [old]:
            reason = "SCALAR_TO_SINGLETON_LIST"
            description = "Supply this field as a single-element JSON array."
        elif isinstance(old, str) and isinstance(new, str) and old.strip() == new:
            reason = "TRIM_SURROUNDING_WHITESPACE"
            description = "Remove only surrounding whitespace from the field."
        elif (
            field == "operation" and isinstance(old, str)
            and isinstance(new, str) and old.casefold() == new.casefold()
        ):
            reason = "OPERATION_ENUM_CASE"
            description = "Use the exact canonical operation enum spelling."
        else:
            reason = "CANONICAL_FORM_UNCLASSIFIED"
            description = (
                "A payload change is observed, but Runtime has not attested "
                "the transformation rule. Use the admitted canonical field "
                "value where visible; do not assume an equivalent permission."
            )
        rules.append({
            "rule_id": reason,
            "input_fields": [field], "output_fields": [field],
            "from_type": type(old).__name__ if field in before else "absent",
            "to_type": type(new).__name__ if field in after else "absent",
            "rule": description,
        })
    ids = list(dict.fromkeys(item["rule_id"] for item in rules))
    reason = str(notes.get("rewrite_reason_code") or "")[:120]
    if not reason:
        reason = (
            "CANONICAL_FORM_UNCLASSIFIED"
            if "CANONICAL_FORM_UNCLASSIFIED" in ids
            else "CANONICAL_FORM_NORMALIZATION"
        )
    return {
        "changed_fields": fields[:24],
        "rewrite_reason_code": reason,
        "rewrite_rule_ids": ids[:24],
        "rewrite_rules": rules[:24],
        "rewrite_rule_schema": REWRITE_RULE_SCHEMA,
        "planner_canonical_form": _public_action(after),
    }


def rejection_traces(
    proposed_actions: list, diagnostics: list,
    *, original_actions: Mapping | None,
    rewrite_notes: Mapping | None = None,
    request_id: str, task_epoch: str, round_id: int,
) -> list[dict]:
    """Record *all* parsed operational proposals including rejected batches.

    A batch rejection is attributed to the turn, not falsely to one operation.
    No execution or success evidence is created.
    """
    reasons = [
        str(item.get("reason") or "UNKNOWN")[:120]
        for item in diagnostics[:12] if isinstance(item, Mapping)
    ]
    issues = [
        {"reason_code": str(item.get("reason") or "UNKNOWN")[:120],
         "detail": str(item.get("detail") or "")[:512]}
        for item in diagnostics[:6] if isinstance(item, Mapping)
    ]
    records = []
    for position, candidate in enumerate(proposed_actions[:24]):
        if not isinstance(candidate, Mapping):
            continue
        tool = str(candidate.get("tool") or "")
        if tool in {"report_progress", "turn_commit", "final_response"}:
            continue
        aid = str(candidate.get("action_id") or "")[:160]
        original = (
            original_actions.get(aid, candidate)
            if isinstance(original_actions, Mapping) else candidate
        )
        rewrite = (rewrite_notes or {}).get(aid) if isinstance(rewrite_notes, Mapping) else None
        rewrite = rewrite if isinstance(rewrite, Mapping) else {}
        rewrite_details = _rewrite_details(original, candidate, rewrite)
        rewritten = bool(rewrite_details["changed_fields"])
        records.append({
            "schema": SCHEMA,
            "request_id": request_id,
            "task_epoch": task_epoch,
            "round": round_id,
            "action_id": aid or f"UNBOUND-{round_id}-{position}",
            "planner_requested": _public_action(original),
            "runtime_resolved": {
                "disposition": (
                    "REWRITE_CANDIDATE_NOT_ADMITTED" if rewritten
                    else "REJECTED_PREEXECUTION"
                ),
                "submitted_normalized": _public_action(candidate),
                "effective_action": None,
                "rewrite_candidate_action": _public_action(candidate) if rewritten else None,
                "rewrite_reason_code": rewrite_details["rewrite_reason_code"],
                "rewrite_rule_schema": rewrite_details["rewrite_rule_schema"],
                "rewrite_rule_ids": rewrite_details["rewrite_rule_ids"],
                "rewrite_rules": rewrite_details["rewrite_rules"],
                "planner_canonical_form": rewrite_details["planner_canonical_form"],
                "reason_codes": reasons,
                "reason_details": issues,
                "changed_fields": rewrite_details["changed_fields"],
                "rewrite_applied": False,
                "rewrite_attempted": rewritten,
                "authorization_granted": False,
                "admission_status": "NOT_ADMITTED",
            },
            "execution": {
                "status": "NOT_EXECUTED",
                "dispatched_action": None,
                "executed": False,
                "evidence_type": "PREEXECUTION_ADMISSION_DIAGNOSTIC",
                "execution_evidence_ref": "",
                "admission_diagnostic_ref": f"ADMISSION-{round_id}-{position}",
            },
        })
    return records


def executed_trace(
    provenance: Mapping, *, rewrite_info: Mapping | None,
    request_id: str, task_epoch: str,
) -> dict | None:
    """Project already-verified dispatched Action Provenance, not model prose."""
    if (not isinstance(provenance, Mapping)
            or provenance.get("request_id") != request_id
            or provenance.get("task_epoch") != task_epoch
            or provenance.get("dispatched_action") is None):
        return None
    original = provenance.get("original_action")
    canonical = provenance.get("canonical_action")
    dispatched = provenance.get("dispatched_action")
    actual = provenance.get("actual_result") or {}
    info = rewrite_info if isinstance(rewrite_info, Mapping) else {}
    rewrite_details = _rewrite_details(original, canonical, info)
    modified = bool(rewrite_details["changed_fields"])
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "task_epoch": task_epoch,
        "round": provenance.get("round"),
        "action_id": str(provenance.get("action_id") or "")[:160],
        "planner_requested": _public_action(original),
        "runtime_resolved": {
            "disposition": "REWRITTEN_AND_DISPATCHED" if modified else "ADMITTED_UNCHANGED",
            "effective_action": _public_action(canonical),
            "changed_fields": rewrite_details["changed_fields"],
            "rewrite_reason_code": rewrite_details["rewrite_reason_code"],
            "rewrite_rule_schema": rewrite_details["rewrite_rule_schema"],
            "rewrite_rule_ids": rewrite_details["rewrite_rule_ids"],
            "rewrite_rules": rewrite_details["rewrite_rules"],
            "planner_canonical_form": rewrite_details["planner_canonical_form"],
            "rewrite_applied": bool(modified),
            "authorization_granted": False,
            "admission_status": "ADMITTED",
        },
        "execution": {
            "status": str(actual.get("status") or "UNKNOWN")[:60],
            "reason_code": str(actual.get("reason") or "")[:120],
            "dispatched_action": _public_action(dispatched),
            "executed": actual.get("executed"),
            "evidence_type": "ACTUAL_TOOL_RESULT",
            "execution_evidence_ref": str(provenance.get("action_id") or "")[:160],
            "observed_data": deepcopy(actual.get("observed_data") or {}),
        },
    }


def bounded_action_trace_projection(
    rejected: list | None, actions: Mapping | None,
    *, rewrite_ledger: Mapping | None, request_id: str,
    task_epoch: str,
) -> list[dict]:
    from .runtime_fact_ledger import planner_projection
    result = [
        deepcopy(t) for t in list(rejected or [])[-MAX_RECENT:]
        if isinstance(t, Mapping)
        and t.get("request_id") == request_id and t.get("task_epoch") == task_epoch
    ]
    for key, row in list((actions or {}).items())[-MAX_RECENT:]:
        if not isinstance(row, Mapping):
            continue
        provenance = row.get("provenance")
        if planner_projection(provenance, request_id=request_id, task_epoch=task_epoch) is None:
            continue
        trace = executed_trace(
            provenance,
            rewrite_info=(rewrite_ledger or {}).get(key),
            request_id=request_id, task_epoch=task_epoch,
        )
        if trace:
            result.append(trace)
    result.sort(key=lambda row: (int(row.get("round") or 0), str(row.get("action_id") or "")))
    return result[-MAX_RECENT:]


__all__ = [
    "SCHEMA", "PREREQ_SCHEMA", "exact_remote_git_read_repository",
    "registered_remote_git_read", "confirmed_target_absence",
    "deployment_path_prerequisites", "propose_equivalent_bound_cwd_rewrite",
    "rejection_traces", "executed_trace", "bounded_action_trace_projection",
]
