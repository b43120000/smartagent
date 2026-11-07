"""Rule DIR-001: confirmed Goal directory absence -> create; presence -> work.

Decision routing is read-only. Directory creation is a separate native Tool
Action behind Runtime request scope and write admission. Goal intent is a
prerequisite, not permission. The rule does not inspect the filesystem.
"""
from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_DIRECTORY_DECISION_RULE_V1"
RULE_ID = "DIR-001"
_ROUTE = "GOAL_DIRECTORY_CREATE"
_INTENTS = {"REMEDIATION_CANDIDATE", "CONDITIONAL_REMEDIATION"}


def _path_key(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 2048:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _identity(runtime) -> tuple[str, str, str]:
    return (
        str(getattr(runtime, "run_id", "") or ""),
        str(getattr(runtime, "task_id", "") or ""),
        str(getattr(runtime, "task_epoch", "") or ""),
    )


def _base(runtime, path: str, state: str, *, evidence_ref: str = "",
          status: str = "", reason: str = "", candidate: dict | None = None) -> dict:
    request_id, task_id, epoch = _identity(runtime)
    return {
        "schema": SCHEMA,
        "rule_id": RULE_ID,
        "request_id": request_id,
        "task_id": task_id,
        "task_epoch": epoch,
        "target": path,
        "observed_evidence_ref": evidence_ref,
        "decision": state,
        "candidate_status": status,
        "reason_code": reason,
        "next_action": (
            "CREATE_DIRECTORY" if state == "CREATE_DIRECTORY_REQUIRED" else
            "WORK_IN_EXISTING_DIRECTORY" if state == "ENTER_EXISTING_DIRECTORY" else
            "INSPECT_DIRECTORY" if state == "INSPECT_DIRECTORY_REQUIRED" else
            "NONE"
        ),
        "candidate": candidate or {},
        "observation_is_not_write_authorization": True,
        "normal_tool_and_write_admission_required": True,
        "executed": False,
        "authorization_granted": False,
        "goal_completion_verified": False,
    }


def evaluate_directory_decision(runtime, gap_view: Mapping | None) -> dict:
    """Deterministically select the *next step*, never create a directory.

    A single bound original Goal target is mandatory. Prefer the newest
    trusted current-epoch observation; no inference from task prose,
    rejected Actions or directory-not-found error messages.
    """
    gap = gap_view if isinstance(gap_view, Mapping) else {}
    contract = getattr(runtime, "_target_path_contract", None) or {}
    if not isinstance(contract, Mapping):
        contract = {}
    path = str(contract.get("requested_target") or "")
    if contract.get("binding_status") != "BOUND" or not _path_key(path):
        return _base(runtime, "", "NOT_APPLICABLE",
                     reason="EXACT_GOAL_TARGET_UNBOUND")
    policy = gap.get("goal_policy") or {}
    if policy.get("intent") == "READ_ONLY_ONLY":
        return _base(runtime, path, "READ_ONLY", reason="MUTATION_FORBIDDEN")
    if policy.get("intent") not in _INTENTS:
        return _base(runtime, path, "NOT_APPLICABLE",
                     reason="NO_EXPLICIT_CREATION_OR_REMEDIATION_INTENT")
    rows = [
        x for x in list(gap.get("gaps") or [])[:32]
        if isinstance(x, Mapping) and _path_key(x.get("path")) == _path_key(path)
        and x.get("observation_status") == "CONFIRMED"
        and isinstance(x.get("observed_value"), bool)
        and x.get("observed_fact_ref")
    ]
    if not rows:
        return _base(runtime, path, "INSPECT_DIRECTORY_REQUIRED",
                     reason="NO_CONFIRMED_DIRECTORY_OBSERVATION")
    observed = rows[-1]
    ref = str(observed["observed_fact_ref"])[:160]
    if observed["observed_value"] is True:
        return _base(runtime, path, "ENTER_EXISTING_DIRECTORY", evidence_ref=ref,
                     reason="TARGET_DIRECTORY_CONFIRMED_PRESENT")
    if observed.get("status") != "UNRESOLVED":
        return _base(runtime, path, "NOT_APPLICABLE", evidence_ref=ref,
                     reason="ABSENCE_NOT_BOUND_TO_GOAL")
    request_id, _, epoch = _identity(runtime)
    if not request_id or not epoch:
        return _base(runtime, path, "NOT_APPLICABLE", evidence_ref=ref,
                     reason="REQUEST_IDENTITY_INCOMPLETE")

    # Native typed create-directory has no CWD and does not require an
    # unrelated parent-directory grant. Its actual target is still checked
    # against the request and writable-root ACL, *before* execution.
    preview_fn = getattr(getattr(runtime, "tools", None), "preview_tool_scope", None)
    scope = "UNAVAILABLE"
    code = "RUNTIME_WRITE_PREFLIGHT_UNAVAILABLE"
    if callable(preview_fn):
        try:
            preview = preview_fn({"tool": "create_directory", "path": path})
        except (OSError, ValueError, TypeError, RuntimeError):
            preview = None
        if isinstance(preview, Mapping):
            resolved = list(preview.get("resolved_paths") or [])
            if preview.get("allowed") is True and len(resolved) == 1 and (
                    _path_key(resolved[0]) == _path_key(path)
                    or (
                        list(preview.get("requested_paths") or []) == [path]
                        and preview.get("scope_source") == "exact_directory_write_target"
                    )):
                scope, code = "PASS", "RUNTIME_EXACT_WRITE_SCOPE_PASS"
            elif preview.get("allowed") is False:
                scope = "DENIED"
                diag = preview.get("scope_diagnostic") or {}
                reason = (diag.get("reason_code") if isinstance(diag, Mapping) else "")
                code = str(reason or preview.get("reason_code") or
                           "RUNTIME_WRITE_SCOPE_DENIED")[:120]
            else:
                scope, code = "UNAVAILABLE", "RUNTIME_WRITE_SCOPE_UNCONFIRMED"
    available = "create_directory" in set(
        __import__(
            "agent_core.tool_capabilities",
            fromlist=["get_allowed_tools"],
        ).get_allowed_tools(
            str(getattr(runtime, "tool_interface", "") or "web_direct")
        )
    )
    status = (
        "CAPABILITY_UNAVAILABLE" if not available else
        "UNTRIED" if scope == "PASS" else
        "NEEDS_USER_AUTHORIZATION" if scope == "DENIED" else
        "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"
    )
    seed = "|".join((request_id, epoch, RULE_ID, _path_key(path), ref))
    strategy_id = "STRAT-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20].upper()
    candidate = {
        "strategy_id": strategy_id,
        "revision": 1,
        "route_id": _ROUTE,
        "goal_relevance": "CREATE_CONFIRMED_MISSING_TARGET_DIRECTORY",
        "tool": "create_directory",
        "operation": "",
        "working_directory": "",
        "target": path,
        "target_path_root": path,
        "target_absence_evidence_ref": ref,
        "purpose": (
            "Create exactly the confirmed-missing Goal directory, without "
            "new files, download, overwrite or parent-scope expansion."
        ),
        "required_action_fields": ["path"],
        "planner_action_contract": {"tool": "create_directory", "path": path},
        "working_directory_scope_preflight": scope,
        "working_directory_scope_reason_code": code,
        "normal_tool_admission_required": True,
        "target_write_admission_required": True,
        "source_integrity_verified": False,
        "status": status,
        "blocks_interrupt": status in {
            "UNTRIED", "NEEDS_ACTION_REPAIR",
            "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED",
        },
        "requires_user_input": status == "NEEDS_USER_AUTHORIZATION",
        "candidate_is_executable_action": False,
        "authorization_granted": False,
        "execution_authorized": False,
        "executed": False,
        "attempt_count": 0,
        "attempted_action_id": "",
        "attempted_reason_code": "",
        "rule": RULE_ID,
        "evidence_digest": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
    }
    return _base(runtime, path, "CREATE_DIRECTORY_REQUIRED", evidence_ref=ref,
                 status=status, reason=code, candidate=candidate)


__all__ = ["SCHEMA", "RULE_ID", "evaluate_directory_decision"]
