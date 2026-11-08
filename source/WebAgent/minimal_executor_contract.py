"""Bounded PokeClaw-style atomic Executor receipts (v10).

Provides typed results to Goal/Prerequisite parents; never executes tools,
changes user scope, grants permission or declares full-goal completion.
"""
from __future__ import annotations
import ntpath
import re
from collections.abc import Mapping

ATOMIC_MODEL_DECISION_LIMIT = 2
TERMINAL_OUTCOMES = frozenset({"VERIFIED", "BLOCKED", "NEEDS_PREREQUISITE", "UNKNOWN"})
_PREREQUISITES = ("PARENT_MISSING", "PREREQUISITE", "DEPENDENCY", "SOURCE_REQUIRED",
                  "SOURCE_LOCATION", "ENVIRONMENT_MISSING")
_DENIALS = ("SCOPE_REJECTED", "PERMISSION_DENIED", "WRITE_CEILING", "NOT_AUTHORIZED")
_COMPOUND_ACTION = re.compile(
    r"(?:並|且|還要|再|接著|然後|同時).{0,32}?"
    r"(?:寫入|修改|刪除|編譯|下載|部署|安裝|複製|建立檔案|執行命令)"
    r"|\b(?:and then|also|followed by)\b.{0,32}?"
    r"\b(?:write|modify|delete|download|deploy|install|copy|run)\b",
    re.I,
)


def single_directory_contract_target(goal: str, action_path: str) -> str:
    """Use a bound one-directory goal; never mistake a compound task for mkdir."""
    from agent_core.target_path_contract import extract_target_contract
    from agent_core.goal_prerequisite_resolution import source_requirement
    contract = extract_target_contract(goal)
    if (not isinstance(contract, Mapping)
            or contract.get("target_kind") != "DIRECTORY_ONLY"
            or contract.get("binding_status") != "BOUND"
            or contract.get("required_top_level_entries")
            or source_requirement(goal) != "NOT_REQUIRED"
            or _COMPOUND_ACTION.search(goal)):
        return ""
    target = str(contract.get("requested_target") or "")
    if not target or not re.match(r"^[A-Za-z]:[\\/]", target):
        return ""
    from agent_core.semantic_capability_resolution import translate_goal_action
    if translate_goal_action(goal).get("canonical_tool") != "create_directory":
        return ""
    normalized = ntpath.normcase(ntpath.normpath(target))
    return normalized if normalized == ntpath.normcase(ntpath.normpath(action_path)) else ""


def blocked_disposition(goal_context: Mapping, history: list[dict],
                        rejected: list[dict]) -> tuple[str, str]:
    """A model's uncertainty is not objective evidence of an environment blocker."""
    if str(goal_context.get("source_requirement") or "") == "REQUIRED":
        category = str(goal_context.get("next_category") or "")
        if category in {
            "SOURCE_LOCATION_OR_SCOPE_REQUIRED", "SOURCE_SCOPE_AUTHORIZATION_REQUIRED",
            "RESOLVE_REGISTERED_SOURCE_ARTIFACT", "VERIFY_SOURCE_CONTENTS",
            "DISCOVER_SOURCE_WITHIN_SCOPE", "CHECK_DEPLOYMENT_ADMISSION",
        }:
            return "NEEDS_PREREQUISITE", category
    for entry in reversed(history[-2:]):
        receipt = entry.get("receipt") or {}
        status = str(receipt.get("status") or "").upper()
        reason = str(receipt.get("reason") or "").upper()
        if status not in {"FAILED", "REJECTED", "UNKNOWN"}:
            continue
        if any(code in reason for code in _PREREQUISITES):
            return "NEEDS_PREREQUISITE", reason[:120]
        if any(code in reason for code in _DENIALS):
            return "BLOCKED", reason[:120]
        return "UNKNOWN", "ACTION_RESULT_" + status
    for entry in reversed(rejected[-2:]):
        reason = str(entry.get("reason_code") or "").upper()
        if any(code in reason for code in _DENIALS):
            return "BLOCKED", reason[:120]
    return "UNKNOWN", "PLANNER_UNCERTAINTY_NOT_VERIFIED"


def action_result_receipt(action: dict, receipt: dict,
                          evidence_ref: str, model_round: int) -> dict:
    """Actual ActionResult receipt, never whole-task proof from a successful tool."""
    status = str(receipt.get("status") or "UNKNOWN").upper()
    reason = str(receipt.get("reason") or "UNKNOWN")[:120]
    executed = receipt.get("executed") is True
    if status == "SUCCEEDED" and executed:
        outcome = "VERIFIED" if (receipt.get("data") or {}).get(
            "verification_status") == "PASS" else "UNKNOWN"
    elif any(code in reason.upper() for code in _PREREQUISITES):
        outcome = "NEEDS_PREREQUISITE"
    elif status == "REJECTED":
        outcome = "BLOCKED"
    else:
        outcome = "UNKNOWN"
    return {
        "schema": "SMARTAGENT_ATOMIC_EXECUTOR_RESULT_V1", "outcome": outcome,
        "reason_code": reason, "tool_status": status,
        "tool": str(action.get("tool") or "")[:64],
        "action_id": str(action.get("action_id") or "")[:96],
        "evidence_ref": str(evidence_ref)[:128], "model_round": int(model_round),
        "executed": executed, "goal_verified": False,
    }


def blocked_result(outcome: str, reason: str, model_round: int) -> dict:
    state = outcome if outcome in TERMINAL_OUTCOMES else "UNKNOWN"
    return {
        "schema": "SMARTAGENT_ATOMIC_EXECUTOR_RESULT_V1", "outcome": state,
        "reason_code": str(reason)[:120], "tool_status": "NOT_EXECUTED",
        "tool": "", "action_id": "", "evidence_ref": "",
        "model_round": int(model_round), "executed": False,
        "goal_verified": False,
    }
