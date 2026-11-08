"""Runtime-owned goal evidence and Tri-One terminal admission.

The same evidence and terminal verdict serve Minimal Core, RemoteAgent,
WebCopilot, WebDirect and Tri-One TaskResult. Transports cannot upgrade UNKNOWN.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import ntpath
import re
from typing import Mapping


TERMINAL_SCHEMA = "SMARTAGENT_TERMINAL_ACCEPTANCE_V1"
_SINGLE_MKDIR = re.compile(
    r"^\s*(?:(?:請(?:你|幫我)?|幫我|麻煩(?:你)?|你幫我)\s*)?"
    r"(?:建立|創立|新增|create|mkdir)\s*"
    r"(?:(?:一個|這個|該|指定的|以下的|以下|此)\s*)?"
    r"(?:(?:資料夾|目錄|folder|directory)\s*)?"
    r"(?:(?:在|於)\s*)?[：:\-\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalEvidence:
    status: str
    reason: str
    evidence_refs: tuple[str, ...] = ()
    target: str = ""
    goal_kind: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "PASS" and bool(self.evidence_refs)


@dataclass(frozen=True)
class TerminalVerdict:
    state: str
    outcome: str
    receipt: dict
    reason: str

    @property
    def is_complete(self) -> bool:
        return self.state == "COMPLETED"


def _normal_path(path: object) -> str:
    raw = str(path or "").strip().strip('"\u201c\u201d' + chr(96))
    if not re.match(r"^[A-Za-z]:[\\/]", raw):
        return ""
    return ntpath.normcase(ntpath.normpath(raw))


def _simple_mkdir_target(goal: str, target: str) -> str:
    """Only a single explicit directory-creation goal allows auto-finishing."""
    goal = str(goal or "")
    target = str(target or "")
    normalized = _normal_path(target)
    if not normalized or not goal or goal.casefold().count(target.casefold()) != 1:
        return ""
    begin = goal.casefold().find(target.casefold())
    remainder = goal[:begin] + goal[begin + len(target):]
    if not _SINGLE_MKDIR.fullmatch(remainder):
        return ""
    return normalized


def evaluate_goal_evidence(goal: str, history: list[dict]) -> GoalEvidence:
    """Check real ToolResult receipts; extra successful reads do not erase proof."""
    matches = []
    for index, entry in enumerate(history):
        if not isinstance(entry, Mapping):
            continue
        action = entry.get("action") or {}
        receipt = entry.get("receipt") or {}
        if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
            continue
        if action.get("tool") != "create_directory":
            continue
        path = str(action.get("path") or "")
        normalized = _simple_mkdir_target(goal, path)
        if not normalized:
            continue
        data = receipt.get("data") or {}
        if not isinstance(data, Mapping):
            continue
        if (receipt.get("tool") != "create_directory"
                or receipt.get("status") != "SUCCEEDED"
                or receipt.get("reason") != "DIRECTORY_CREATED"
                or receipt.get("executed") is not True
                or data.get("directory_created") is not True
                or data.get("observed_exists") is not True
                or data.get("observed_is_directory") is not True
                or data.get("observation_status") != "CONFIRMED"):
            continue
        if (_normal_path(receipt.get("requested_path")) != normalized
                or _normal_path(receipt.get("resolved_path")) != normalized):
            continue
        ref = str(action.get("action_id") or entry.get("action_id") or "")
        if ref:
            matches.append((index, path, ref, normalized))
    if not matches:
        return GoalEvidence("UNKNOWN", "NO_MATCHING_VERIFIED_GOAL_EVIDENCE")

    index, path, ref, normalized = matches[-1]
    for entry in history[index + 1:]:
        action = entry.get("action") or {}
        receipt = entry.get("receipt") or {}
        if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
            continue
        if _normal_path(action.get("path")) != normalized:
            continue
        if action.get("tool") not in {"inspect_directory", "list_directory"}:
            continue
        reason = str(receipt.get("reason") or "").upper()
        if receipt.get("status") != "SUCCEEDED" and (
            "NOT_FOUND" in reason or "MISSING" in reason or "NOT_DIRECTORY" in reason
        ):
            return GoalEvidence("UNKNOWN", "CONTRADICTORY_LATER_OBSERVATION")

    return GoalEvidence("PASS", "DIRECTORY_CREATED_AND_CONFIRMED",
                        (ref,), path, "SINGLE_CREATE_DIRECTORY")


def accept_runtime_terminal(loop, proof: GoalEvidence, *, decision: str = "RUNTIME_GOAL_VERIFIED") -> dict:
    """Issue a V1 acceptance only against current request-scoped action ledger."""
    if not proof.verified:
        raise ValueError("terminal_goal_evidence_unverified")
    ledger = getattr(loop, "action_result_ledger", None)
    if not isinstance(ledger, Mapping):
        raise ValueError("terminal_action_ledger_missing")
    for ref in proof.evidence_refs:
        item = ledger.get(ref)
        if not isinstance(item, Mapping):
            raise ValueError("terminal_evidence_not_in_action_ledger")
        if (item.get("request_id") != loop.run_id
                or item.get("task_id") != loop.task_id
                or item.get("task_epoch") != loop.task_epoch
                or item.get("executed") is not True
                or item.get("result_status") != "SUCCEEDED"
                or item.get("evidence_status") != "ACTIVE"):
            raise ValueError("terminal_evidence_identity_or_status_mismatch")
    record = {
        "schema": TERMINAL_SCHEMA,
        "admission_status": "ACCEPTED",
        "request_id": loop.run_id,
        "task_id": loop.task_id,
        "task_epoch": loop.task_epoch,
        "round": loop.turn_id,
        "decision": decision,
        "task_outcome": "SUCCESS",
        "runtime_state": "COMPLETED",
        "matched_condition_id": proof.goal_kind,
        "evidence_refs": list(proof.evidence_refs),
        "evidence_verdict": "PASS",
        "verification_status": "PASS",
        "goal_evidence_reason": proof.reason,
    }
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    record["receipt_id"] = "TERM-" + hashlib.sha256(encoded).hexdigest()[:20].upper()
    loop.terminal_acceptance = dict(record)
    loop.terminal_outcome = "SUCCESS"
    loop.protocol_state = "COMPLETED"
    return record


def accept_runtime_answer_only(
    loop, *, goal: str, answer: str, had_attachments: bool = False,
) -> tuple[dict | None, str]:
    """Admit only a no-tool, request-scoped text answer.

    A text answer has no ActionResult evidence. Admission proves only that
    Runtime accepted the eligible model reply, never external factual truth.
    """
    from .tri_one_answer_only import classify_original_request, _UNSUPPORTED_CLAIM
    from .task_requirement import assess_task_requirement

    if not isinstance(answer, str) or not answer.strip() or len(answer) > 3000:
        return None, "ANSWER_ONLY_CONTENT_INVALID"
    if (getattr(loop, "continuation_source", None)
            or had_attachments
            or getattr(loop, "_tri_one_has_attached_context", False)
            or getattr(loop, "image_delivery_plan", None)):
        return None, "ANSWER_ONLY_REQUIRES_RESOURCE_CONTEXT"
    if any((
        getattr(loop, "action_ledger", None),
        getattr(loop, "action_result_ledger", None),
        getattr(loop, "condition_result_ledger", None),
        getattr(loop, "pending_result_recovery", None),
        getattr(loop, "deferred_action_repairs", None),
        getattr(loop, "pending_progress_repair_actions", None),
        getattr(loop, "pending_action_repair", None),
    )):
        return None, "ANSWER_ONLY_RUNTIME_ACTION_OR_RECOVERY_PENDING"
    requirement = assess_task_requirement(
        goal, attachments=False, authorized_paths=False,
        image_or_artifact_request=False, continuation=False,
    )
    eligible, reason = classify_original_request(goal)
    if requirement.get("route") != "ANSWER_ONLY" or not eligible:
        return None, "ANSWER_ONLY_REQUEST_NOT_ELIGIBLE:" + str(reason)[:100]
    if _UNSUPPORTED_CLAIM.search(answer):
        return None, "ANSWER_ONLY_UNVERIFIED_EXECUTION_CLAIM"
    if str(getattr(loop, "task_request", goal) or "") != str(goal):
        return None, "ANSWER_ONLY_GOAL_IDENTITY_MISMATCH"
    record = {
        "schema": TERMINAL_SCHEMA,
        "admission_status": "ACCEPTED",
        "admission_reason": "ANSWER_ONLY_RESPONSE_ADMITTED",
        "completion_kind": "ANSWER_ONLY",
        "decision": "MINIMAL_TEXT_ONLY_FINISH_V1",
        "request_id": loop.run_id,
        "task_id": loop.task_id,
        "task_epoch": loop.task_epoch,
        "round": loop.turn_id,
        "task_outcome": "SUCCESS",
        "runtime_state": "COMPLETED",
        "matched_condition_id": "NO_TOOL_TEXT_RESPONSE",
        "evidence_refs": [],
        "evidence_verdict": "UNVERIFIED",
        "verification_status": "NOT_REQUIRED",
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    record["receipt_id"] = "TERM-" + hashlib.sha256(encoded).hexdigest()[:20].upper()
    loop.terminal_acceptance = dict(record)
    loop.terminal_outcome = "SUCCESS"
    loop.protocol_state = "COMPLETED"
    return record, "ANSWER_ONLY_RESPONSE_ADMITTED"


def terminal_verdict_from_loop(loop, *, minimal: bool = True) -> TerminalVerdict:
    """Canonical status projection, not a chance to reinterpret goal evidence."""
    receipt = getattr(loop, "terminal_acceptance", {}) or {}
    receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
    outcome = str(getattr(loop, "terminal_outcome", "UNKNOWN") or "UNKNOWN").upper()
    if receipt.get("schema") == TERMINAL_SCHEMA and receipt.get("admission_status") == "ACCEPTED":
        identities = (
            ("task_id", "task_id"), ("request_id", "run_id"),
            ("task_epoch", "task_epoch"),
        )
        if not all(receipt.get(key) == getattr(loop, attr, None) for key, attr in identities):
            return TerminalVerdict("PAUSED", "UNKNOWN", {}, "FOREIGN_TERMINAL_RECEIPT")
        state = str(receipt.get("runtime_state") or "").upper()
        if receipt.get("task_outcome") != outcome:
            return TerminalVerdict("PAUSED", "UNKNOWN", {}, "TERMINAL_OUTCOME_MISMATCH")
        kind = str(receipt.get("completion_kind") or "")
        refs = receipt.get("evidence_refs")
        evidence_verdict = str(receipt.get("evidence_verdict") or "")
        if outcome == "SUCCESS":
            answer_only = (
                kind == "ANSWER_ONLY"
                and receipt.get("decision") == "MINIMAL_TEXT_ONLY_FINISH_V1"
                and receipt.get("admission_reason") == "ANSWER_ONLY_RESPONSE_ADMITTED"
                and refs == [] and evidence_verdict == "UNVERIFIED"
                and str(receipt.get("answer_sha256") or "")
            )
            evidence_backed = (
                kind != "ANSWER_ONLY" and isinstance(refs, list)
                and bool(refs) and evidence_verdict == "PASS"
            )
            if not (answer_only or evidence_backed):
                return TerminalVerdict("PAUSED", "UNKNOWN", {},
                                       "SUCCESS_WITHOUT_ACCEPTED_GOAL_PROOF")
        if state == "COMPLETED" and outcome in {"SUCCESS", "PARTIAL", "FAILED", "UNKNOWN"}:
            return TerminalVerdict(state, outcome, receipt, "ACCEPTED_TERMINAL")
        if state == "INTERRUPTED":
            return TerminalVerdict(state, outcome, receipt, "ACCEPTED_INTERRUPTION")
        return TerminalVerdict("PAUSED", "UNKNOWN", {}, "INVALID_TERMINAL_STATE")
    if minimal:
        return TerminalVerdict("PAUSED", "UNKNOWN", {}, "MINIMAL_GOAL_NOT_VERIFIED")
    return TerminalVerdict("COMPLETED", outcome, {}, "LEGACY_UNVERIFIED")


def format_runtime_goal_summary(proof: GoalEvidence) -> str:
    if proof.goal_kind == "SINGLE_CREATE_DIRECTORY" and proof.verified:
        return "已建立並確認資料夾：\n" + proof.target
    raise ValueError("no_verified_runtime_goal_summary")


__all__ = [
    "GoalEvidence", "TerminalVerdict", "evaluate_goal_evidence",
    "accept_runtime_terminal", "accept_runtime_answer_only", "terminal_verdict_from_loop",
    "format_runtime_goal_summary",
]
