"""Durable, task/epoch-bound Executor handoff for Goal and Prerequisite loops.

Only Runtime writes terminal receipts. Parent consumption and stage completion
are separate decisions; neither action observation nor a success flag can
silently advance a parent stage. No raw prompt, commands or tool output stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "SMARTAGENT_EXECUTOR_BLACKBOARD_V1"
TERMINAL = frozenset({"VERIFIED", "BLOCKED", "NEEDS_PREREQUISITE"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def _id(value: object) -> str:
    text = str(value or "")
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError("executor_blackboard_invalid_task_identity")
    return text


def _code(value: object) -> str:
    # All producer reasons are Runtime-owned. Preserve their stable composite
    # tokens regardless of case: a lower-case "scope_rejected:" must NOT
    # collapse to UNCLASSIFIED_BLOCKER at Blackboard egress.
    text = str(value or "").strip().upper()[:160]
    return text if _CODE.fullmatch(text) else "UNMAPPED_TERMINAL_REASON_FORMAT"


def _root(root: str | Path) -> Path:
    base = Path(root)
    if base.name.casefold() == "source" and (base.parent / "collect.bat").is_file():
        base = base.parent
    return base / "localdata" / "runtime" / "executor_blackboard"


def _path(root: str | Path, task_id: str) -> Path:
    return _root(root) / (_id(task_id) + ".json")


def _load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    handle, tmp = tempfile.mkstemp(prefix=".executor-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parent_binding(loop) -> dict:
    contract = getattr(loop, "atomic_task_contract", None)
    if (not isinstance(contract, Mapping)
            or contract.get("schema") != "SMARTAGENT_ATOMIC_TASK_V1"
            or contract.get("task_id") != getattr(loop, "task_id", None)):
        return {"parent_task_id": "", "parent_task_epoch": "",
                "stage_id": "", "atomic_task_id": str(getattr(loop, "task_id", "")),
                "parent_binding_status": "ROOT_TASK_PARENT_NOT_BOUND"}
    parent = str(contract.get("parent_task_id") or "")[:160]
    epoch = str(contract.get("parent_task_epoch") or "")[:160]
    stage = str(contract.get("stage_id") or "")[:160]
    if parent and epoch and stage:
        return {
            "parent_task_id": parent, "parent_task_epoch": epoch,
            "stage_id": stage,
            "atomic_task_id": str(contract.get("atomic_task_id") or loop.task_id)[:160],
            "parent_binding_status": "BOUND",
        }
    return {
        "parent_task_id": "", "parent_task_epoch": "", "stage_id": "",
        "atomic_task_id": str(contract.get("atomic_task_id") or loop.task_id)[:160],
        "parent_binding_status": "ROOT_TASK_PARENT_NOT_BOUND",
    }


def publish_executor_terminal(loop, result: Mapping, *,
                              root: str | Path, goal: str,
                              evidence_refs: list[str] | tuple[str, ...] = (),
                              resolution_attempts: int = 0,
                              resolution_state: str = "NOT_REQUIRED") -> dict:
    """Reject UNKNOWN and reject VERIFIED without binding to real runtime proof."""
    status = str(result.get("outcome") or "")
    trusted_acceptance = getattr(loop, "terminal_acceptance", {})
    if status not in TERMINAL:
        raise ValueError("executor_blackboard_nonterminal_status")
    if status == "VERIFIED" and (
            result.get("goal_verified") is not True
            or not getattr(loop, "terminal_acceptance", {}).get("evidence_verdict") == "PASS"):
        raise ValueError("executor_blackboard_unverified_success")
    task_id = _id(getattr(loop, "task_id", ""))
    request_id = str(getattr(loop, "run_id", ""))
    epoch = str(getattr(loop, "task_epoch", ""))
    if not request_id or not epoch:
        raise ValueError("executor_blackboard_missing_identity")
    evidence = list(dict.fromkeys(
        str(ref)[:160] for ref in evidence_refs if isinstance(ref, str) and ref
    ))[:24]
    if status == "VERIFIED" and not evidence:
        raise ValueError("executor_blackboard_verified_without_evidence_refs")
    # No model text or tool prose can manufacture a confirmed fact.
    fact = {}
    if status == "VERIFIED" and trusted_acceptance.get(
            "matched_condition_id") == "SINGLE_FACT_QUERY":
        expected = trusted_acceptance.get("fact_observation")
        if (not isinstance(expected, dict)
                or result.get("fact_observation") != expected
                or expected.get("fact_observation_status") != "CONFIRMED"
                or type(expected.get("fact_observed_value")) is not bool
                or expected.get("fact_evidence_ref") not in evidence):
            raise ValueError("executor_blackboard_single_fact_provenance_invalid")
        fact = dict(expected)
    action_ids = list(dict.fromkeys(
        str(ref)[:160] for ref in [result.get("action_id", "")] if ref
    ))
    admission_refs = list(dict.fromkeys(
        str(ref)[:160] for ref in (result.get("admission_rejection_refs") or [])
        if isinstance(ref, str) and ref.startswith("MIN-ADM-")
    ))[:24]
    denial = result.get("last_admission_rejection")
    denial = denial if isinstance(denial, Mapping) else {}
    # Safe only: no original proposal paths, command, or raw exception text.
    denial_keys = (
        "tool", "reason_code", "root_cause_code",
        "scope_failure_stage", "scope_category", "scope_next_transition",
        "scope_cause_digest", "scope_cause_classification",
        "admission_evidence_ref", "round", "executed",
    )
    last_denial = {
        key: denial[key] for key in denial_keys
        if key in denial
    }
    raw_preconditions = denial.get("directory_preconditions") or {}
    if isinstance(raw_preconditions, Mapping):
        last_denial["directory_preconditions"] = {
            key: str(raw_preconditions.get(key) or "")[:72]
            for key in (
                "write_authorization_verdict",
                "target_binding_verdict",
                "parent_directory_verdict",
            )
        }
    causal_basis = (
        "ACTUAL_TOOL_EVIDENCE_WITH_PRIOR_DENIAL" if (
            status == "VERIFIED" and admission_refs
        ) else "ACTUAL_TOOL_EVIDENCE" if status == "VERIFIED"
        else "RUNTIME_ADMISSION_DENIAL" if admission_refs
        else "ACTUAL_TOOL_EVIDENCE" if evidence
        else "EXECUTOR_DECISION_CONTRACT"
    )
    verification_action_refs = list(dict.fromkeys(
        str(ref)[:160] for ref in (result.get("verification_action_refs") or [])
        if isinstance(ref, str) and ref
    ))[:24]
    tool_evidence_refs = list(dict.fromkeys(
        str(ref)[:160] for ref in (result.get("tool_evidence_refs") or [])
        if isinstance(ref, str) and ref.startswith("MIN-EV-")
    ))[:24]
    if status == "VERIFIED" and result.get("orchestration_mode") == "EXECUTOR_ONLY" and (
            verification_action_refs != list(
                trusted_acceptance.get("evidence_refs") or []
            )
            or not tool_evidence_refs
            or not set(verification_action_refs).issubset(set(evidence))
            or not set(tool_evidence_refs).issubset(set(evidence))):
        raise ValueError("executor_blackboard_verified_evidence_classes_unbound")
    existing = _load(_path(root, task_id))
    if existing and (existing["request_id"], existing["task_epoch"]) != (request_id, epoch):
        # A task ID may not silently overwrite a previous request/epoch receipt.
        raise ValueError("executor_blackboard_identity_collision")
    binding = _parent_binding(loop)
    data = {
        "schema": SCHEMA, "task_id": task_id, "request_id": request_id,
        "task_epoch": epoch,
        "goal_sha256": hashlib.sha256(str(goal).encode("utf-8")).hexdigest(),
        "terminal_receipt_id": str(getattr(loop, "terminal_acceptance", {}).get(
            "receipt_id", "") or "")[:96] if status == "VERIFIED" else "",
        **binding,
        "executor_state": "TERMINAL", "terminal_status": status,
        "orchestration_mode": (
            "EXECUTOR_ONLY" if result.get("orchestration_mode") == "EXECUTOR_ONLY"
            else "STANDARD"
        ),
        "reason_code": _code(result.get("reason_code")),
        "root_cause_code": _code(
            result.get("root_cause_code") or result.get("reason_code")
        ),
        "reason_sha256": hashlib.sha256(
            str(result.get("reason_code") or "").encode("utf-8")
        ).hexdigest(),
        "causal_basis": causal_basis,
        "goal_verified": status == "VERIFIED",
        "fact_observation": fact,
        "original_tool_status": str(result.get("original_tool_status") or
                                    result.get("tool_status") or "")[:24],
        "last_observed_tool_status": str(result.get("last_observed_tool_status") or
                                         result.get("tool_status") or "")[:24],
        "side_effect_outcome_confirmed": bool(
            result.get("side_effect_outcome_confirmed", status == "VERIFIED")),
        "side_effect_may_have_occurred": bool(
            result.get("side_effect_may_have_occurred", False)),
        "action_ids": action_ids,
        "last_observed_action_id": str(result.get("last_observed_action_id") or "")[:160],
        "last_tool_result_reason": _code(
            result.get("last_tool_result_reason") or "NO_TOOL_RESULT_OBSERVED"
        ),
        "evidence_refs": evidence,
        "verification_action_refs": verification_action_refs,
        "tool_evidence_refs": tool_evidence_refs,
        "admission_rejection_refs": admission_refs,
        "last_admission_rejection": last_denial,
        "resolution_attempts": min(16, max(0, int(resolution_attempts))),
        "resolution_state": str(resolution_state)[:40],
        "model_round": max(0, int(result.get("model_round") or 0)),
        "parent_consumption": {
            "status": "PENDING", "stage_verification": "NOT_EVALUATED",
            "consumer_evidence_refs": [],
        },
        "grants_authorization": False,
        "advances_parent_stage": False,
    }
    from .tri_layer_handoff import build_handoff
    data["tri_layer_handoff"] = build_handoff(data)
    if existing:
        # Idempotent replay of a terminal receipt is safe; contradictory
        # terminal transitions are rejected rather than overwritten.
        stable = ("terminal_status", "reason_code", "task_epoch", "request_id")
        if any(existing.get(k) != data.get(k) for k in stable):
            raise ValueError("executor_blackboard_terminal_conflict")
        return existing
    _atomic_write(_path(root, task_id), data)
    return data


def read_executor_terminal(root: str | Path, *, task_id: str,
                           request_id: str | None = None,
                           task_epoch: str | None = None) -> dict | None:
    record = _load(_path(root, task_id))
    if not record or record.get("terminal_status") not in TERMINAL:
        return None
    if request_id is not None and record.get("request_id") != request_id:
        return None
    if task_epoch is not None and record.get("task_epoch") != task_epoch:
        return None
    return record


def executor_child_results(root: str | Path, *, parent_task_id: str,
                           parent_task_epoch: str = "", limit: int = 16) -> list[dict]:
    parent = _id(parent_task_id)
    directory = _root(root)
    if not directory.is_dir():
        return []
    results = []
    for path in sorted(directory.glob("*.json"))[-1024:]:
        value = _load(path)
        if not value or value.get("parent_task_id") != parent:
            continue
        if parent_task_epoch and value.get("parent_task_epoch") != parent_task_epoch:
            continue
        if value.get("terminal_status") not in TERMINAL:
            continue
        results.append(value)
    return results[-max(1, min(int(limit), 32)):]


def consume_executor_terminal(root: str | Path, *, child_task_id: str,
                              parent_task_id: str, parent_task_epoch: str,
                              stage_id: str, stage_verification: str,
                              consumer_evidence_refs: list[str]) -> dict:
    """Parent acknowledgement only; NEVER mark a parent Goal/Stage completed."""
    path = _path(root, child_task_id)
    value = _load(path)
    if not value:
        raise ValueError("executor_blackboard_child_not_found")
    if (value.get("parent_task_id") != parent_task_id
            or value.get("parent_task_epoch") != parent_task_epoch
            or value.get("stage_id") != stage_id):
        raise ValueError("executor_blackboard_parent_binding_mismatch")
    if stage_verification not in {"PASS", "FAIL", "NEEDS_RECHECK"}:
        raise ValueError("executor_blackboard_invalid_stage_verification")
    refs = list(dict.fromkeys(str(r)[:160] for r in consumer_evidence_refs if r))[:24]
    if not refs:
        raise ValueError("executor_blackboard_consumer_evidence_required")
    old = value.get("parent_consumption", {})
    if old.get("status") == "CONSUMED":
        if old.get("stage_verification") != stage_verification:
            raise ValueError("executor_blackboard_consumer_conflict")
        return value
    value["parent_consumption"] = {
        "status": "CONSUMED", "stage_verification": stage_verification,
        "consumer_evidence_refs": refs,
    }
    # This legacy acknowledgement is not the Goal Stage Verifier.
    # The dedicated prerequisite and goal-stage handoff identities remain
    # separate; no layer's status is copied onto another.
    handoff = value.get("tri_layer_handoff")
    if isinstance(handoff, dict):
        handoff["handoff_delivery_state"] = "PARENT_ACKNOWLEDGED"
    # Even PASS acknowledges one child, not the entire parent stage.
    value["advances_parent_stage"] = False
    _atomic_write(path, value)
    return value


def project_executor_for_parent(value: Mapping | None) -> dict:
    """Bounded safe projection, equally usable by Goal and Prerequisite."""
    if not value or value.get("terminal_status") not in TERMINAL:
        return {}
    fields = (
        "task_id", "request_id", "goal_sha256", "terminal_receipt_id",
        "parent_task_id", "parent_task_epoch", "stage_id",
        "atomic_task_id", "task_epoch", "terminal_status", "reason_code",
        "root_cause_code", "reason_sha256", "causal_basis",
        "parent_binding_status", "goal_verified", "fact_observation",
        "orchestration_mode", "original_tool_status", "last_observed_tool_status",
        "side_effect_outcome_confirmed", "side_effect_may_have_occurred",
        "action_ids", "last_observed_action_id", "last_tool_result_reason",
        "evidence_refs", "verification_action_refs", "tool_evidence_refs",
        "admission_rejection_refs",
        "last_admission_rejection", "resolution_attempts",
        "resolution_state", "parent_consumption", "grants_authorization",
        "advances_parent_stage",
    )
    result = {k: value[k] for k in fields if k in value}
    from .tri_layer_handoff import project_handoff
    result["tri_layer_handoff"] = project_handoff(value)
    return result


def acknowledge_prerequisite_handoff(root: str | Path, *,
                                    executor_task_id: str, parent_task_id: str,
                                    parent_task_epoch: str, readiness_verdict: str,
                                    evidence_refs: list[str]) -> dict:
    """Prerequisite owns ONLY its readiness verdict; never marks Goal complete."""
    if readiness_verdict not in {"READY", "NOT_READY", "NEEDS_REPLAN"}:
        raise ValueError("prerequisite_readiness_verdict_invalid")
    path = _path(root, executor_task_id)
    record = _load(path)
    handoff = (record or {}).get("tri_layer_handoff") or {}
    prerequisite = handoff.get("prerequisite_handoff") or {}
    if not record or (
            prerequisite.get("prerequisite_owner_task_id") != parent_task_id
            or prerequisite.get("prerequisite_owner_task_epoch") != parent_task_epoch):
        raise ValueError("prerequisite_handoff_identity_mismatch")
    refs = list(dict.fromkeys(str(ref)[:160] for ref in evidence_refs if ref))[:24]
    if not refs:
        raise ValueError("prerequisite_handoff_evidence_required")
    existing = prerequisite.get("prerequisite_readiness_verdict")
    if existing not in {"NOT_EVALUATED", readiness_verdict}:
        raise ValueError("prerequisite_handoff_conflict")
    prerequisite["prerequisite_readiness_verdict"] = readiness_verdict
    prerequisite["prerequisite_readiness_evidence_refs"] = refs
    prerequisite["prerequisite_handoff_state"] = (
        "READY_FOR_GOAL_REVIEW" if readiness_verdict == "READY"
        else "REPLAN_REQUIRED" if readiness_verdict == "NEEDS_REPLAN"
        else "READINESS_RECHECK_REQUIRED"
    )
    handoff["handoff_delivery_state"] = "PREREQUISITE_CONSUMED"
    # The Goal Stage owner remains separate and MUST re-evaluate.
    _atomic_write(path, record)
    return record


def acknowledge_goal_stage_handoff(root: str | Path, *,
                                   executor_task_id: str, parent_task_id: str,
                                   parent_task_epoch: str, stage_id: str,
                                   stage_verdict: str,
                                   evidence_refs: list[str]) -> dict:
    """Goal Stage may record a verdict, but not infer Overall Goal SUCCESS."""
    if stage_verdict not in {"PASS", "FAIL", "NEEDS_RECHECK"}:
        raise ValueError("goal_stage_verification_verdict_invalid")
    path = _path(root, executor_task_id)
    record = _load(path)
    handoff = (record or {}).get("tri_layer_handoff") or {}
    goal_stage = handoff.get("goal_stage_handoff") or {}
    if not record or (
            goal_stage.get("goal_owner_task_id") != parent_task_id
            or goal_stage.get("goal_owner_task_epoch") != parent_task_epoch
            or goal_stage.get("goal_stage_id") != stage_id):
        raise ValueError("goal_stage_handoff_identity_mismatch")
    refs = list(dict.fromkeys(str(ref)[:160] for ref in evidence_refs if ref))[:24]
    if not refs:
        raise ValueError("goal_stage_handoff_evidence_required")
    old = goal_stage.get("goal_stage_verification_verdict")
    if old not in {"NOT_EVALUATED", stage_verdict}:
        raise ValueError("goal_stage_handoff_conflict")
    goal_stage["goal_stage_verification_verdict"] = stage_verdict
    goal_stage["goal_stage_evidence_refs"] = refs
    goal_stage["goal_stage_handoff_state"] = (
        "VERIFICATION_RECORDED" if stage_verdict == "PASS"
        else "WAITING_FOR_REVIEW"
    )
    # Does not set Stage COMPLETED or Goal SUCCESS. Those are independent
    # Goal Driver terminal rules, requiring all stage evidence.
    handoff["handoff_delivery_state"] = "GOAL_STAGE_CONSUMED"
    handoff["stage_transition_committed"] = False
    _atomic_write(path, record)
    return record
