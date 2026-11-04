"""Runtime-attested pre-execution strategy recovery (no side effects).

Classifies a very narrow class of *unexecuted* Action Admission failures:
an absent command working directory within the request's authorized target
scope. Never infers authorization from the user request, exposes a new
write root, creates directories, downloads tools, or runs commands.

A Planner can switch to a canonical file-writing action, whose ordinary
Tool Admission checks and post-action evidence are still mandatory.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .path_security import PathSecurityError, is_within
from .security_context import SecurityContext

SCHEMA = "SMARTAGENT_PREEXECUTION_REPLAN_V1"
MAX_ACTIONS = 16


def _enclosed(path: Path, roots: Sequence[str]) -> bool:
    for value in roots:
        try:
            root = Path(value).expanduser().resolve()
            if is_within(path, root):
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _authorized_existing_parent(target: Path, context: SecurityContext) -> Path | None:
    """Return an existing directory admitted by actual Runtime write policy."""
    for candidate in target.parents:
        try:
            if not candidate.is_dir():
                continue
            context.require_write(candidate, require_absolute=True)
            return candidate
        except (PathSecurityError, OSError, RuntimeError, ValueError):
            continue
    return None


def analyze_preexecution_rejection(
    *, diagnostics: Sequence[Mapping[str, object]],
    submitted_actions: Sequence[Mapping[str, object]],
    security_context: SecurityContext,
    original_request_paths: Sequence[str],
    request_id: str,
    task_epoch: str,
) -> dict | None:
    """Report a verified missing-CWD prerequisite, never a model guess.

    Only an actual Runtime CWD rejection combined with a fresh filesystem
    preflight and existing policy allows this guidance. An existing target
    with missing permission, symlink, or an unrelated path is not recoverable
    by this route. The rejected command is NOT executed.
    """
    reasons = [dict(row) for row in diagnostics
               if isinstance(row, Mapping)
               and row.get("reason") == "run_command_working_directory_rejected"]
    if not reasons or not 0 < len(submitted_actions) <= MAX_ACTIONS:
        return None
    commands = [dict(item) for item in submitted_actions
                if isinstance(item, Mapping)
                and item.get("tool") == "run_command"]
    if len(commands) != 1 or len(reasons) != 1:
        return None
    action = commands[0]
    raw = action.get("working_directory")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        return None
    # Other CWD denials (out-of-scope, ACL, symlink, policy) are not
    # permission to propose another path.
    detail = str(reasons[0].get("detail", "") or "")
    if not detail.startswith("command_working_directory_not_directory:"):
        return None
    prefix = "command_working_directory_not_directory:"
    # The error must be about the same literal directory as this Action,
    # not merely another similarly shaped diagnostic.
    if Path(detail[len(prefix):]).expanduser().resolve() != Path(raw).expanduser().resolve():
        return None
    original_path = Path(raw).expanduser()
    if original_path.is_symlink():
        return None
    target = original_path.resolve()
    if not _enclosed(target, original_request_paths):
        return None
    # Explicitly verify the same absence reported by the CWD admission.
    if target.exists() or target.is_symlink():
        return None
    try:
        approved_target = security_context.require_write(
            target, require_absolute=True, forbid_glob=True,
        )
    except (PathSecurityError, OSError, RuntimeError, ValueError):
        return None
    if approved_target != target:
        return None
    parent = _authorized_existing_parent(target, security_context)
    if parent is None:
        return None
    # Never treat a verified parent as permission to execute in a
    # parent that is outside the request's command CWD boundary. The
    # permitted strategy is a new Tool Action targeting the original scope.
    receipt = {
        "schema": SCHEMA,
        "request_id": request_id,
        "task_epoch": task_epoch,
        "rejected_action_id": str(action.get("action_id", "")),
        "rejected_action_tool": "run_command",
        "admission_reason": "command_working_directory_not_directory",
        "target": str(target),
        "target_exists": False,
        "existing_writable_parent": str(parent),
        "scope_verified": True,
        "executed": False,
        "authorization_granted": False,
        "result_verification": "PREFLIGHT_ONLY",
        "routes": [
            {
                "id": "NATIVE_FILE_CREATION",
                "tool": "write_file",
                "rule": "If the original goal requires a file, use a NEW write_file action for an exact user-requested file inside the approved target; ordinary Tool Scope and readback verification remain mandatory. write_file creates missing parents.",
            },
            {
                "id": "EXISTING_CWD_CREATION",
                "tool": "run_command",
                "rule": "Alternatively create the requested directory from a separately authorized, existing CWD; a NEW atomic action must pass command CWD, write scope, approval, verification and success criteria. The existing parent here is not itself a command CWD grant.",
            },
        ],
        "verification_required": True,
    }
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    receipt["evidence_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return receipt


VERIFIED_FAILURE_SCHEMA = "SMARTAGENT_VERIFIED_PREREQUISITE_REPLAN_V1"
_SAFE_OBSERVATIONS = frozenset({
    "read_file", "list_directory", "find_file", "inspect_directory",
    "query_project", "inspect_project_scope", "observe_fact",
})


def analyze_verified_prerequisite_failure(
    *, action: Mapping[str, object], result: Mapping[str, object],
    evidence_ref: str, request_id: str, task_epoch: str,
    effects_pending: bool = False,
) -> dict | None:
    """Only a completed, verified failed *read/probe* may request replanning.

    An aborted build, failed installer, or unknown/partial mutation might
    already have effects. Such actions MUST be reconciled before replanning.
    This receipt asserts only the failed probe, never that a particular
    dependency is missing or an installer is authorized.
    """
    tool = str(action.get("tool", "") or "")
    operation = str(action.get("operation", "") or "").upper()
    if not (tool in _SAFE_OBSERVATIONS or
            (tool == "run_command" and operation in {"INSPECT", "GIT_INSPECT"})):
        return None
    if effects_pending or result.get("executed") is not True:
        return None
    if (str(result.get("result_status", "")).upper() not in {"FAILED", "REJECTED"}
            or str(result.get("execution_status", "")).upper()
            not in {"FAILED", "REJECTED"}
            or str(result.get("effective_verification_status", "")).upper()
            != "FAIL" or str(result.get("expectation_status", "")).upper() == "PASS"):
        return None
    if not isinstance(evidence_ref, str) or not evidence_ref:
        return None
    receipt = {
        "schema": VERIFIED_FAILURE_SCHEMA,
        "request_id": request_id,
        "task_epoch": task_epoch,
        "rejected_action_id": str(action.get("action_id", "")),
        "admission_reason": "VERIFIED_FAILED_PREREQUISITE_OBSERVATION",
        "evidence_ref": evidence_ref,
        "result_status": "FAILED",
        "result_verification": "FAIL",
        "scope_verified": True,
        "executed": True,
        "completion_verified": False,
        "authorization_granted": False,
        "routes": [{
            "id": "INSPECT_ALTERNATIVE",
            "tool": "run_command",
            "rule": "Choose another already authorized, bounded probe to locate a usable capability or alternative toolchain.",
        }, {
            "id": "REMEDIATE_IF_AUTHORIZED",
            "tool": "run_command",
            "rule": "If the goal truly needs a missing dependency, remediate only after the Runtime has verified absence, trusted artifact/source, exact target, explicit installation/network/command authorization, and a bounded independent post-install verification plan. Workspace write access alone is never permission to download or execute installers.",
        }],
        "verification_required": True,
    }
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    receipt["evidence_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return receipt


def render_verified_prerequisite_replan(receipt: Mapping[str, object]) -> str:
    """Only the failed *probe* is verified, not the missing dependency itself."""
    public = {key: value for key, value in dict(receipt).items()
              if key in {"schema", "rejected_action_id", "admission_reason",
                         "evidence_ref", "result_status", "result_verification",
                         "scope_verified", "executed", "completion_verified",
                         "authorization_granted", "routes",
                         "verification_required", "evidence_digest"}}
    return (
        "[RUNTIME_VERIFIED_PREREQUISITE_REPLAN_REQUIRED]\n"
        + json.dumps(public, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
        + "\nThis completed read/probe failed with actual Runtime-verified "
          "FAIL evidence. That does NOT by itself prove a toolchain is missing, "
          "nor authorize downloading/installing it. Replan from the original "
          "Goal with a NEW, properly scoped observation or separately "
          "authorized remediation. Reconcile uncertain effects before any "
          "mutation. Preserve existing evidence, step progress, and budgets.\n"
          "[/RUNTIME_VERIFIED_PREREQUISITE_REPLAN_REQUIRED]"
    )


def repeats_rejected_strategy(action: Mapping[str, object], blocked: Mapping[str, object]) -> bool:
    """Ignore a new Action ID if the rejected semantic command is identical."""
    return bool(
        blocked
        and action.get("tool") == "run_command"
        and str(action.get("command", "")) == str(blocked.get("command", ""))
        and str(action.get("operation", "")) == str(blocked.get("operation", ""))
        and str(action.get("working_directory", "")) == str(blocked.get("working_directory", ""))
    )


def render_preexecution_replan(receipt: Mapping[str, object]) -> str:
    """Bounded untrusted-to-Planner data; never used as direct tool arguments."""
    public = {key: value for key, value in dict(receipt).items()
              if key in {"schema", "rejected_action_id", "admission_reason",
                         "target", "target_exists", "existing_writable_parent",
                         "scope_verified", "executed", "authorization_granted",
                         "result_verification", "routes",
                         "verification_required", "evidence_digest"}}
    return (
        "[RUNTIME_PREEXECUTION_REPLAN_REQUIRED]\n"
        + json.dumps(public, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
        + "\nThe rejected action was NOT executed. Change strategy, not only "
          "verify fields or action_id. Keep the original Goal, authorized "
          "paths and budget. Do not replay the rejected command. The Runtime "
          "will independently validate any NEW file write or command; "
          "this preflight is not a successful mutation or verification. "
          "Use the runtime Progress Strategy Revision below.\n"
          "[/RUNTIME_PREEXECUTION_REPLAN_REQUIRED]"
    )
