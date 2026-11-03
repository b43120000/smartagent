"""Narrow B-P1 read-only observation completion; never a PR-B/C/D verifier.

This is a terminal *scope* check against trusted Runtime ledgers.  It does not
assert package installation, independent VERIFY, remediation or resumed work.
"""
from __future__ import annotations

import ntpath
import re


EXPECTED_WHEEL = "smartagent_pr_bcd_live-1.0-py3-none-any.whl"
OBSERVATION_REASONS = (
    "NATIVE_DIRECTORY_LIST_CURRENT",
    "NATIVE_DIRECTORY_INSPECT_CURRENT",
    "PROVEN_INSPECT_EXIT_ZERO",
    "NATIVE_DIRECTORY_LIST_CURRENT",
)


def is_bp1_request(request: str) -> bool:
    """Fail closed: only an explicitly read-only B-P1 Evidence Live request."""
    text = str(request or "")
    lower = text.casefold()
    return bool(
        re.search(r"(?<![a-z0-9])b-p1(?![a-z0-9])", lower)
        and "evidence" in lower
        and "runtime observation" in lower
        and "freshness" in lower
        and "只驗證" in text
        and "禁止" in text
        and "寫入" in text
        and "pr-b/c/d" in lower
    )


def _winpath(value: object) -> str:
    text = str(value or "").strip()
    if not re.match(r"^[A-Za-z]:\\", text):
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _result(admitted: bool, reason: str, action_ids=()) -> dict:
    return {
        "admitted": bool(admitted),
        "reason": str(reason),
        "action_ids": list(action_ids),
        "completion_kind": "B_P1_OBSERVATION_ONLY",
        "full_pr_bcd": "NOT_VERIFIED",
    }


def evaluate_bp1_terminal(loop, progress: dict) -> dict:
    """Check four exact, current, request-bound actions and the PR-A ledger.

    The original request selects this lane; model completion text cannot grant
    authority. The exact wheelhouse/INSPECT shape prevents using arbitrary
    read-only actions to satisfy the four-action contract.
    """
    if not is_bp1_request(getattr(loop, "task_request", "")):
        return _result(False, "B_P1_REQUEST_NOT_SCOPED")
    if str(progress.get("decision", "")).upper() != "COMPLETE" or str(
        progress.get("outcome", "")
    ).upper() != "SUCCESS":
        return _result(False, "B_P1_TERMINAL_DECISION_INVALID")
    if not bool(getattr(loop, "_pr_v3_project_root_explicit", False)):
        return _result(False, "B_P1_EXPLICIT_PROJECT_ROOT_REQUIRED")
    root = _winpath(getattr(loop, "_pr_v3_project_root", ""))
    if not root:
        return _result(False, "B_P1_PROJECT_ROOT_UNAVAILABLE")
    wheelhouse = ntpath.join(root, "wheelhouse")
    deps = ntpath.join(root, "deps")
    wheel = ntpath.join(wheelhouse, EXPECTED_WHEEL)
    action_records = list(getattr(loop, "action_ledger", {}).items())
    evidence = getattr(loop, "action_result_ledger", {})
    if len(action_records) != 4 or len(evidence) != 4:
        return _result(False, "B_P1_EXACT_FOUR_ACTIONS_REQUIRED")
    if any(
        getattr(loop, attr, None)
        for attr in (
            "pending_progress_repair_actions", "pending_action_repair",
            "deferred_action_repairs", "pending_result_recovery",
            "pending_verification_requirement", "pending_command_field_repairs",
            "active_stage_manifest",
        )
    ):
        return _result(False, "B_P1_UNRESOLVED_RUNTIME_STATE")
    if getattr(loop, "condition_result_ledger", {}):
        return _result(False, "B_P1_UNEXPECTED_CONDITION_VERIFICATION")
    refs = set(str(ref) for ref in progress.get("evidence_refs") or [])
    ids = [str(action_id) for action_id, _ in action_records]
    if len(set(ids)) != 4 or refs != set(ids):
        return _result(False, "B_P1_FOUR_ACTION_EVIDENCE_REFS_REQUIRED", ids)
    for index, (action_id, record) in enumerate(action_records):
        action = record.get("action") or {}
        tool = str(action.get("tool", ""))
        path = _winpath(action.get("path"))
        if index == 0:
            shape_ok = tool == "list_directory" and path == wheelhouse
        elif index == 1:
            shape_ok = (
                tool == "inspect_directory"
                and action.get("recursive") is False
                and [_winpath(p) for p in action.get("paths", [])]
                == [root, wheelhouse, deps]
            )
        elif index == 2:
            shape_ok = (
                tool == "run_command"
                and str(action.get("operation", "")).upper() == "INSPECT"
                and _winpath(action.get("working_directory")) == root
                and bool(re.fullmatch(
                    r"(?i)Get-Item\s+-LiteralPath\s+'([^']+)'",
                    str(action.get("command", "")).strip(),
                ))
                and _winpath(re.fullmatch(
                    r"(?i)Get-Item\s+-LiteralPath\s+'([^']+)'",
                    str(action.get("command", "")).strip(),
                ).group(1)) == wheel
                and action.get("verify", []) == []
            )
        else:
            shape_ok = tool == "list_directory" and path == deps
        if not shape_ok:
            return _result(False, "B_P1_ACTION_SHAPE_MISMATCH", ids)
        row = evidence.get(action_id) or {}
        if (
            str(row.get("request_id", "")) != str(getattr(loop, "run_id", ""))
            or str(row.get("task_id", "")) != str(getattr(loop, "task_id", ""))
            or str(row.get("task_epoch", "")) != str(getattr(loop, "task_epoch", ""))
        ):
            return _result(False, "B_P1_EVIDENCE_IDENTITY_MISMATCH", ids)
        if (
            row.get("executed") is not True
            or row.get("observation_proof_admitted") is not True
            or str(row.get("observation_proof_reason", "")) != OBSERVATION_REASONS[index]
            or str(row.get("execution_status", "")).upper() != "SUCCEEDED"
            or str(row.get("result_status", "")).upper() != "SUCCEEDED"
            or str(row.get("evidence_status", "")).upper() != "ACTIVE"
            or str(row.get("verification_status", "")).upper() not in {"UNKNOWN", "UNVERIFIED"}
            or str(row.get("effective_verification_status", "")).upper() not in {"UNKNOWN", "UNVERIFIED"}
            or str(row.get("expectation_status", "")).upper() in {"FAIL", "SPEC_INVALID"}
        ):
            return _result(False, "B_P1_OBSERVATION_NOT_CURRENT_OR_PROVEN", ids)
    graph_provider = getattr(loop, "_pr_a_ledger", None)
    if graph_provider is None:
        return _result(False, "B_P1_PR_A_LEDGER_UNAVAILABLE", ids)
    try:
        graph = graph_provider.read(loop.task_id) or {}
    except (OSError, KeyError, TypeError, ValueError):
        return _result(False, "B_P1_PR_A_LEDGER_UNREADABLE", ids)
    nodes = graph.get("nodes") or {}
    if (
        str(graph.get("request_id", "")) != str(loop.run_id)
        or str(graph.get("task_id", "")) != str(loop.task_id)
        or any(
            (nodes.get("action:" + action_id.lower()) or {}).get("state") != "SATISFIED"
            for action_id in ids
        )
    ):
        return _result(False, "B_P1_PR_A_ACTION_NOT_SATISFIED", ids)
    return _result(True, "B_P1_FOUR_CURRENT_OBSERVATIONS_PROVEN", ids)
