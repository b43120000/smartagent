"""Executor-only: user-bound conditional mkdir with independent evidence.

Goal Driver and Prerequisite may be disabled, but Runtime Goal Terminal
verification and all original Tool Admission / Security checks stay enabled.
"""
from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_EXECUTOR_ONLY_DIRECTORY_SEQUENCE_V1"
_PATH = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s\x60"<>|?*,，。；;\r\n)\]]+)', re.I)


def executor_only_enabled(loop) -> bool:
    value = getattr(loop, "executor_only_mode", None)
    if value is not None:
        return value is True
    return os.environ.get("SMARTAGENT_EXECUTOR_ONLY_MODE", "0").strip() == "1"


def directory_sequence_contract(request: str) -> dict:
    """Strict six-step integration contract; never a generic goal shortcut."""
    text = str(request or "")
    paths = [m.group(1) for m in _PATH.finditer(text)]
    if len(paths) != 1 or text.count(paths[0]) != 1:
        return {}
    raw = paths[0]
    if not re.fullmatch(r"[A-Za-z]:[\\/][^:*?<>|]+", raw):
        return {}
    checks = [
        r"三層\s*Blackboard\s*整合驗證",
        r"如果不存在|若不存在",
        r"建立資料夾|建立",
        r"已存在",
        r"確認目標.*資料夾",
        r"ToolResult",
        r"Shared\s*Blackboard",
        r"Executor.*Prerequisite.*Goal\s*Driver",
        r"回報.*狀態",
    ]
    if not all(re.search(expr, text, re.I | re.S) for expr in checks):
        return {}
    nums = re.findall(r"(?m)^\s*([1-9])\s*[.．、]\s*", text)
    if nums != list("123456"):
        return {}
    return {
        "schema": SCHEMA,
        "target": ntpath.normcase(ntpath.normpath(raw)),
        "display_target": raw,
        "goal_kind": "EXECUTOR_ONLY_DIRECTORY_SEQUENCE",
        "authorization_granted": False,
    }


def _same_path(value: object, target: str) -> bool:
    raw = str(value or "")
    return bool(re.match(r"^[A-Za-z]:[\\/]", raw)) and (
        ntpath.normcase(ntpath.normpath(raw)) == target
    )


def _observation(entry: Mapping, target: str) -> tuple[str, str]:
    from agent_core.tri_one_runtime_terminal import (
        _confirmed_exists_fact, _verified_existing_directory,
    )
    action, receipt = entry.get("action") or {}, entry.get("receipt") or {}
    if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
        return "UNKNOWN", ""
    ref = str(action.get("action_id") or "")
    if not ref:
        return "UNKNOWN", ""
    if action.get("tool") == "observe_fact":
        fact = _confirmed_exists_fact(entry, target)
        if fact is False:
            return "ABSENT", ref
        if fact is True:
            return "PRESENT_PATH", ref
    if action.get("tool") == "inspect_directory":
        paths, data = action.get("paths"), receipt.get("data") or {}
        if (isinstance(paths, list) and len(paths) == 1
                and _same_path(paths[0], target)
                and receipt.get("tool") == "inspect_directory"
                and receipt.get("status") == "SUCCEEDED"
                and receipt.get("reason") == "DIRECTORY_ABSENCE_OBSERVED"
                and receipt.get("executed") is True
                and isinstance(data, Mapping)
                and data.get("observation_status") == "CONFIRMED"
                and data.get("observed_exists") is False
                and _same_path(data.get("observed_path"), target)):
            return "ABSENT", ref
    if action.get("tool") in {"list_directory", "inspect_directory"}:
        if _verified_existing_directory(entry, target):
            return "PRESENT_DIRECTORY", ref
    return "UNKNOWN", ""


def sequence_progress(contract: Mapping, history: list[dict]) -> dict:
    """No observation can be inferred from Planner prose or denied actions."""
    target = str(contract["target"])
    phase, refs, created = "INITIAL_CHECK", [], False
    for row in history:
        action = row.get("action") or {}
        kind, ref = _observation(row, target)
        if phase == "INITIAL_CHECK":
            if kind == "ABSENT":
                refs.append(ref)
                phase = "CREATE_REQUIRED"
            elif kind == "PRESENT_PATH":
                refs.append(ref)
                phase = "TYPE_VERIFY_EXISTING"
            elif kind == "PRESENT_DIRECTORY":
                refs.append(ref)
                phase = "COMPLETE_REUSED"
            else:
                phase = "INDETERMINATE"
                break
        elif phase == "CREATE_REQUIRED":
            from agent_core.tri_one_runtime_terminal import _verified_created_directory
            if action.get("tool") == "create_directory" and _verified_created_directory(row, target):
                refs.append(str(action["action_id"]))
                created = True
                phase = "POSTCREATE_TYPE_VERIFY"
            else:
                phase = "INDETERMINATE"
                break
        elif phase in {"TYPE_VERIFY_EXISTING", "POSTCREATE_TYPE_VERIFY"}:
            if kind == "PRESENT_DIRECTORY":
                refs.append(ref)
                phase = "COMPLETE_CREATED" if created else "COMPLETE_REUSED"
            elif kind == "PRESENT_PATH":
                refs.append(ref)  # Still needs trusted directory-type result.
            else:
                phase = "INDETERMINATE"
                break
        else:
            phase = "INDETERMINATE"
            break
    reasons = {
        "INITIAL_CHECK": "EXECUTOR_INITIAL_OBSERVATION_REQUIRED",
        "CREATE_REQUIRED": "EXECUTOR_CREATE_REQUIRED_AFTER_CONFIRMED_ABSENCE",
        "TYPE_VERIFY_EXISTING": "EXECUTOR_EXISTING_DIRECTORY_TYPE_REQUIRED",
        "POSTCREATE_TYPE_VERIFY": "EXECUTOR_POSTCREATE_DIRECTORY_TYPE_REQUIRED",
        "COMPLETE_CREATED": "EXECUTOR_DIRECTORY_CREATED_AND_INDEPENDENTLY_VERIFIED",
        "COMPLETE_REUSED": "EXECUTOR_DIRECTORY_ALREADY_PRESENT_AND_VERIFIED",
        "INDETERMINATE": "EXECUTOR_SEQUENCE_EVIDENCE_INCOMPLETE_OR_CONTRADICTORY",
    }
    return {"phase": phase, "reason": reasons[phase],
            "proof_refs": list(dict.fromkeys(refs)), "created": created}


def next_action_gate(contract: Mapping, history: list[dict], action: Mapping) -> str:
    """Check sequence only, then run usual Action Admission and write policy."""
    phase = sequence_progress(contract, history)["phase"]
    tool = action.get("tool")
    allowed = {
        "INITIAL_CHECK": {"observe_fact", "inspect_directory", "list_directory"},
        "CREATE_REQUIRED": {"create_directory"},
        "TYPE_VERIFY_EXISTING": {"list_directory", "inspect_directory", "observe_fact"},
        "POSTCREATE_TYPE_VERIFY": {"list_directory", "inspect_directory", "observe_fact"},
    }.get(phase, set())
    if tool not in allowed:
        return "EXECUTOR_ONLY_ACTION_OUT_OF_ORDER:" + phase
    target = str(contract["target"])
    if tool == "inspect_directory":
        paths = action.get("paths")
        if not isinstance(paths, list) or len(paths) != 1 or not _same_path(paths[0], target):
            return "EXECUTOR_ONLY_PATH_NOT_EXACT_TARGET"
        if action.get("recursive") not in {None, False}:
            return "EXECUTOR_ONLY_RECURSIVE_INSPECTION_DISALLOWED"
    elif not _same_path(action.get("path"), target):
        return "EXECUTOR_ONLY_PATH_NOT_EXACT_TARGET"
    if tool == "observe_fact" and (action.get("predicate") or action.get("condition_id")):
        return "EXECUTOR_ONLY_OBSERVE_FACT_PREDICATE_DISALLOWED"
    return ""


def evaluate_executor_sequence(contract: Mapping, history: list[dict]):
    from agent_core.tri_one_runtime_terminal import GoalEvidence
    state = sequence_progress(contract, history)
    if state["phase"] not in {"COMPLETE_CREATED", "COMPLETE_REUSED"}:
        return GoalEvidence("UNKNOWN", state["reason"])
    return GoalEvidence(
        "PASS", state["reason"], tuple(state["proof_refs"]),
        str(contract["display_target"]), str(contract["goal_kind"]),
    )


def format_executor_blackboard_report(proof, blackboard: Mapping) -> str:
    """No report until terminal receipt is really persisted and read back."""
    if (not blackboard or blackboard.get("terminal_status") != "VERIFIED"
            or blackboard.get("goal_verified") is not True
            or not blackboard.get("terminal_receipt_id")
            or not isinstance(blackboard.get("tri_layer_handoff"), Mapping)):
        raise ValueError("executor_only_blackboard_verified_receipt_missing")
    handoff = blackboard["tri_layer_handoff"]
    if (not handoff.get("handoff_id")
            or handoff.get("executor_handoff", {}).get("executor_terminal_status") != "VERIFIED"):
        raise ValueError("executor_only_blackboard_handoff_invalid")
    if not blackboard.get("evidence_refs"):
        raise ValueError("executor_only_blackboard_evidence_missing")
    verdict = ("本次已建立，並完成獨立型別驗證"
               if proof.reason == "EXECUTOR_DIRECTORY_CREATED_AND_INDEPENDENTLY_VERIFIED"
               else "原已存在，未重複建立，並完成目錄型別驗證")
    prereq = handoff["prerequisite_handoff"]
    goal = handoff["goal_stage_handoff"]
    return (
        "Executor-only 整合驗證完成（上層 Agent 決策暫停）。\n"
        f"目標：{proof.target}\n"
        f"結果：{verdict}\n"
        f"Executor Terminal：{blackboard['terminal_status']}\n"
        f"Runtime Terminal Receipt：{blackboard['terminal_receipt_id']}\n"
        f"Tool Evidence References：{', '.join(blackboard['evidence_refs'])}\n"
        f"Handoff ID：{handoff['handoff_id']}\n"
        "Blackboard：終態寫入後已重新讀取\n"
        f"Prerequisite：{prereq['prerequisite_handoff_state']} / {prereq['prerequisite_readiness_verdict']}\n"
        f"Goal Stage：{goal['goal_stage_handoff_state']} / {goal['goal_stage_verification_verdict']}\n"
        f"Parent Binding：{handoff.get('parent_binding_status', '')}\n"
        f"Parent Dispatch：{handoff.get('parent_dispatch_state', '')}\n"
        f"Handoff Consumption：{handoff.get('handoff_delivery_state', '')}\n"
        "尚未啟動 Prerequisite／Goal Driver，亦未推定任何父層完成。"
    )
