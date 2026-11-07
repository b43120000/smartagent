"""Request-bound prerequisite -> Progress transition constraint.

A discovered environment candidate and a suggested Probe are NOT tool
evidence, approval, or a satisfied prerequisite. This module projects a
verified exact registered Git read candidate with Runtime Scope preview;
terminal admission is blocked only when the candidate can be tried safely
under ordinary full admission and no relevant current-epoch evidence exists.

Planner chooses actions. Runtime keeps Scope, command security and approval.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

SCHEMA = "SMARTAGENT_PREREQUISITE_TRANSITION_GATE_V1"
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REMOTE_REQUEST = re.compile(
    r"(?i)(github|repository|repo\b|私人儲存庫|遠端|網路授權|"
    r"git\s+ls-remote|working.directory|工作目錄|存取權限|來源取得)"
)


def _registered_candidate(artifact_view: Mapping | None) -> tuple[str, str]:
    refs = [row for row in list((artifact_view or {}).get("artifact_references") or [])[:8]
            if isinstance(row, Mapping)
            and row.get("kind") == "REMOTE_ARTIFACT_REFERENCE"
            and _REPO.fullmatch(str(row.get("repository") or ""))]
    # An artifact list is not a Planner-selected arbitrary source. A single
    # exact repository permits only its HEAD read; never a clone or a download.
    if len(refs) != 1:
        return "", ""
    return str(refs[0]["repository"]), str(refs[0].get("candidate_id") or "")


def _current_probe_attempts(artifact_view: Mapping | None, *,
                            action_ledger: Mapping | None,
                            result_ledger: Mapping | None,
                            request_id: str, task_epoch: str) -> list[dict]:
    """Only actual current-epoch Tool Result Evidence counts as attempted."""
    from .action_resolution_trace import registered_remote_git_read
    actions = action_ledger if isinstance(action_ledger, Mapping) else {}
    results = result_ledger if isinstance(result_ledger, Mapping) else {}
    attempts: list[dict] = []
    for action_id, evidence in list(results.items())[-64:]:
        if (not isinstance(evidence, Mapping)
                or evidence.get("request_id") != request_id
                or evidence.get("task_epoch") != task_epoch
                or evidence.get("evidence_status", "ACTIVE") != "ACTIVE"):
            continue
        action = (actions.get(action_id) or {}).get("action") or {}
        if not isinstance(action, Mapping) or not registered_remote_git_read(action, artifact_view):
            continue
        attempts.append({
            "action_id": str(action_id)[:160],
            "result_status": str(evidence.get("result_status") or "UNKNOWN")[:40],
            "execution_status": str(evidence.get("execution_status") or "UNKNOWN")[:40],
        })
    return attempts[-8:]


def build_prerequisite_transition(*, artifact_view: Mapping | None,
                                  prerequisite_view: Mapping | None,
                                  remote_view: Mapping | None,
                                  request_id: str, task_epoch: str,
                                  bound_workspace: str = "",
                                  tool_scope_preview=None,
                                  action_ledger: Mapping | None = None,
                                  result_ledger: Mapping | None = None,
                                  guidance_issued: bool = False) -> dict:
    """Return the same causal gate for Planner Context and Progress Admission.

    tool_scope_preview is the Runtime's existing read-only scoped preflight
    function, not a model-provided assertion. A PASS is only CWD scope and is
    never permission to contact the network or read the repository.
    """
    prereq = prerequisite_view if isinstance(prerequisite_view, Mapping) else {}
    access = remote_view if isinstance(remote_view, Mapping) else {}
    remote_state = str(access.get("authorization_state") or "UNKNOWN")
    if remote_state not in {"UNKNOWN", "AUTHORIZED", "DENIED", "NEEDS_USER_APPROVAL"}:
        remote_state = "UNKNOWN"
    repository, candidate_id = _registered_candidate(artifact_view)
    required = (
        prereq.get("source_requirement") == "REQUIRED"
        and prereq.get("next_category") == "RESOLVE_REGISTERED_SOURCE_ARTIFACT"
        and bool(repository)
        and bool(request_id and task_epoch)
    )
    attempts = (_current_probe_attempts(
        artifact_view, action_ledger=action_ledger,
        result_ledger=result_ledger, request_id=request_id, task_epoch=task_epoch
    ) if required else [])
    command = "git ls-remote https://github.com/" + repository + ".git HEAD" if repository else ""
    # A candidate from a registered artifact is a *probe suggestion*. CWD
    # Scope can be checked without starting or authorizing that probe.
    scope_status, scope_reason, resolved_cwd = "NOT_CHECKED", "", ""
    if required and remote_state == "UNKNOWN" and not attempts:
        if not bound_workspace or not callable(tool_scope_preview):
            scope_status = "UNAVAILABLE"
            scope_reason = "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
        else:
            try:
                preview = tool_scope_preview({
                    "tool": "run_command", "operation": "GIT_INSPECT",
                    "command": command, "working_directory": bound_workspace,
                })
                if not isinstance(preview, Mapping):
                    scope_status = "UNAVAILABLE"
                    scope_reason = "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
                elif preview.get("allowed") is True:
                    resolved = list(preview.get("resolved_paths") or [])
                    if resolved and str(resolved[0]) == str(bound_workspace):
                        scope_status, resolved_cwd = "PASS", str(resolved[0])
                    else:
                        scope_status = "UNAVAILABLE"
                        scope_reason = "RUNTIME_SCOPE_PATH_UNCONFIRMED"
                else:
                    scope_status = "DENIED"
                    from .planner_context_snapshot import _scope_code
                    scope_reason = _scope_code(preview.get("error"))
                    if not scope_reason:
                        scope_reason = "RUNTIME_SCOPE_PREFLIGHT_REJECTED"
            except (OSError, ValueError, RuntimeError, TypeError):
                scope_status = "UNAVAILABLE"
                scope_reason = "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
    if not required:
        stage = "NOT_APPLICABLE"
        disposition = "DEFER_TO_EXISTING_GOAL_ADMISSION"
    elif remote_state == "AUTHORIZED":
        stage = "ACCESS_VERIFIED"
        disposition = "CONTINUE_SOURCE_ACQUISITION_WITH_NORMAL_ADMISSION"
    elif remote_state == "DENIED":
        stage = "POLICY_DENIAL_VERIFIED"
        disposition = "ALLOW_EVIDENCE_BOUND_USER_PERMISSION_REQUEST"
    elif remote_state == "NEEDS_USER_APPROVAL":
        stage = "APPROVAL_REQUIREMENT_VERIFIED"
        disposition = "USE_EXISTING_ONE_TIME_APPROVAL"
    elif attempts:
        stage = "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED"
        disposition = "PLANNER_ASSESS_ACTUAL_PROBE_RESULT"
    elif access.get("git_executable_status") != "AVAILABLE":
        stage = "GIT_CAPABILITY_UNVERIFIED"
        disposition = "VERIFY_GIT_CAPABILITY_BEFORE_REMOTE_READ"
    elif scope_status == "PASS":
        stage = "READY_FOR_TOOL_ADMISSION"
        disposition = "CONTINUE_PROPOSE_EXACT_GIT_READ_ACTION"
    elif scope_status == "DENIED":
        stage = "WORKSPACE_SCOPE_REJECTED"
        disposition = "REQUEST_EXACT_MISSING_SCOPE_OR_REPLAN"
    else:
        stage = "WORKSPACE_SCOPE_UNVERIFIED"
        disposition = "RESOLVE_EXISTING_WORKSPACE_SCOPE"
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "task_epoch": task_epoch,
        "required": bool(required),
        "repository": repository if required else "",
        "candidate_id": candidate_id if required else "",
        "access_state": remote_state,
        "stage": stage,
        "progress_transition": disposition,
        "source_integrity_verified": False,
        "probe_action": ({
            "tool": "run_command", "operation": "GIT_INSPECT",
            "command": command, "working_directory": resolved_cwd,
        } if stage == "READY_FOR_TOOL_ADMISSION" else {}),
        "scope_preflight_status": scope_status,
        "scope_preflight_reason_code": scope_reason,
        "scope_preflight_is_network_authorization": False,
        "actual_probe_attempts": attempts,
        "probe_evidence_present": bool(attempts),
        "guidance_previously_issued": bool(guidance_issued),
        "guidance_is_evidence": False,
        "interrupt_must_have_proof_of_blocker": bool(required and stage == "READY_FOR_TOOL_ADMISSION"),
        "planner_decision_authority": True,
        "full_tool_admission_required": True,
        "authorization_granted": False,
        "action_dispatched": False,
        "evidence_created": False,
    }


def premature_interrupt_diagnostic(
    context_request: Mapping | None,
    readiness: Mapping | None,
) -> dict | None:
    """Stop unsupported user-input interruption when a scoped probe is ready.

    This is a narrow policy for the registered remote source used by Case D.
    Genuine preview-denied scope, actual denied/approval receipts and prior
    attempted probes remain under existing INTERRUPT and safety gates.
    """
    req = context_request if isinstance(context_request, Mapping) else {}
    record = readiness if isinstance(readiness, Mapping) else {}
    if (req.get("kind") != "USER_INPUT" or
            not record.get("required") or
            not _REMOTE_REQUEST.search(str(req.get("detail") or "")) or
            record.get("stage") != "READY_FOR_TOOL_ADMISSION"):
        return None
    return {
        "marker": "[PREREQUISITE_TRANSITION_EVIDENCE_REQUIRED]",
        "reason": "remote_prerequisite_probe_not_executed",
        "repair_scope": "PREREQUISITE_EVIDENCE",
        "prerequisite_transition": dict(record),
        "detail": "A current-epoch registered GitHub HEAD read is scope-ready but has no actual Tool Result evidence. Prior guidance is not Evidence.",
        "suggestion": (
            "Use report_progress decision=CONTINUE with next_action=run_command; "
            "propose exactly prerequisite_transition.probe_action under full "
            "Runtime Tool/Scope/Network/Approval Admission. A scope preflight "
            "PASS is not permission or execution evidence. If normal Admission "
            "denies the Action, use that current-epoch Runtime blocker or "
            "approval receipt for an exact USER_INPUT request. Never run Git "
            "with the absent deployment target as working_directory; never "
            "invent a repository result or modify deployment files."
        ),
    }


__all__ = ["SCHEMA", "build_prerequisite_transition", "premature_interrupt_diagnostic"]
