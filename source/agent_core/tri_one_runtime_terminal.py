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
    if _SINGLE_MKDIR.fullmatch(remainder):
        return normalized
    # Reuse the exact request-bound atomic contract for equivalent word orders.
    # This does NOT allow extra operations or infer mutation from incidental paths.
    from WebAgent.minimal_executor_contract import single_directory_contract_target
    return single_directory_contract_target(goal, target)


# A finite, fully-anchored directory Goal contract. Matching a phrase such as
# "if missing" alone is NEVER sufficient to skip unrelated Goal conditions.
_CONDITIONAL_CHECK_CREATE_VERIFY = re.compile(
    r"^\s*(?:請(?:你|幫我)?\s*)?(?:檢查|確認|查看)\s*"
    r"(?:這個|該|指定的)?(?:資料夾|目錄)?\s*"
    r"(?:是否存在|有沒有存在|存不存在)\s*"
    r"[，,；;]\s*(?:如果|若|倘若)\s*"
    r"(?:該|這個)?(?:資料夾|目錄)?\s*不存在\s*"
    r"(?:就|則|請)?\s*(?:建立|創建|新增|建立它|建立該資料夾|建立資料夾)"
    r"\s*[，,；;]\s*(?:最後|然後|再|並|且|接著)\s*"
    r"(?:再次|重新)?(?:檢查|確認|驗證)\s*"
    r"(?:該|這個)?(?:資料夾|目錄)?\s*"
    r"(?:是否|確實|真的)?\s*(?:存在|已存在)"
    r"\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


def _conditional_directory_target(goal: str, target: str) -> str:
    """Return a bound Windows target ONLY for the complete known Goal shape.

    No suffix commands, file writes, extra targets or unbound variables are
    allowed. All action proofs later need the exact same requested path.
    """
    raw = str(goal or "")
    path = str(target or "")
    canonical = _normal_path(path)
    if not canonical or raw.casefold().count(path.casefold()) != 1:
        return ""
    start = raw.casefold().find(path.casefold())
    residue = raw[:start] + raw[start + len(path):]
    return canonical if _CONDITIONAL_CHECK_CREATE_VERIFY.fullmatch(residue) else ""


def _confirmed_exists_fact(entry: Mapping, target: str) -> bool | None:
    """Independently stat-observed file.exists, never a planner assertion.

    A confirmed file.exists=True only proves existence, not that the object
    is a directory. Directory type requires a separate create/inspect receipt.
    """
    action, receipt = entry.get("action") or {}, entry.get("receipt") or {}
    if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
        return None
    action_id = action.get("action_id")
    if (action.get("tool") != "observe_fact" or not action_id
            or _normal_path(action.get("path")) != target
            or receipt.get("tool") != "observe_fact"
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("reason") not in {"FACT_OBSERVED", "FACT_PREDICATE_PASSED"}
            or receipt.get("executed") is not True
            or receipt.get("action_id") != action_id
            or _normal_path(receipt.get("requested_path")) != target):
        return None
    data = receipt.get("data") or {}
    obs = data.get("observations") if isinstance(data, Mapping) else None
    if (not isinstance(obs, list) or len(obs) != 1
            or not isinstance(obs[0], Mapping)
            or data.get("observation_status") != "CONFIRMED"):
        return None
    fact = obs[0]
    if (fact.get("schema") != "SMARTAGENT_EVIDENCE_V1"
            or fact.get("field") != "file.exists"
            or fact.get("source") != "filesystem.stat"
            or fact.get("freshness") != "CURRENT"
            or fact.get("observation_status") != "CONFIRMED"
            or fact.get("raw_evidence_ref") != action_id
            or _normal_path(fact.get("scope")) != target
            or type(fact.get("value")) is not bool):
        return None
    return fact["value"]


def _verified_created_directory(entry: Mapping, target: str) -> bool:
    action, receipt = entry.get("action") or {}, entry.get("receipt") or {}
    if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
        return False
    data = receipt.get("data") or {}
    return bool(
        action.get("tool") == "create_directory"
        and action.get("action_id")
        and _normal_path(action.get("path")) == target
        and receipt.get("tool") == "create_directory"
        and receipt.get("status") == "SUCCEEDED"
        and receipt.get("reason") == "DIRECTORY_CREATED"
        and receipt.get("executed") is True
        and _normal_path(receipt.get("requested_path")) == target
        and bool(receipt.get("resolved_path"))
        and isinstance(data, Mapping)
        and data.get("directory_created") is True
        and data.get("observed_exists") is True
        and data.get("observed_is_directory") is True
        and data.get("observation_status") == "CONFIRMED"
    )


def _verified_existing_directory(entry: Mapping, target: str) -> bool:
    action, receipt = entry.get("action") or {}, entry.get("receipt") or {}
    if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
        return False
    # A successfully executed list_directory also proves the target is a
    # directory (unlike file.exists=True, which could be a regular file).
    if action.get("tool") == "list_directory":
        data = receipt.get("data") or {}
        return bool(
            action.get("action_id")
            and _normal_path(action.get("path")) == target
            and receipt.get("tool") == "list_directory"
            and receipt.get("status") == "SUCCEEDED"
            and receipt.get("reason") == "DIRECTORY_LISTED"
            and receipt.get("executed") is True
            and _normal_path(receipt.get("requested_path")) == target
            and isinstance(data, Mapping)
            and data.get("terminal_eligible") is True
            and isinstance(data.get("entries"), list)
        )
    # Only a typed inspect_directory listing of one exact path can confirm
    # the directory's type. A generic file.exists=True cannot.
    paths = action.get("paths") or []
    if (action.get("tool") != "inspect_directory"
            or not action.get("action_id")
            or not isinstance(paths, list) or len(paths) != 1
            or _normal_path(paths[0]) != target
            or receipt.get("tool") != "inspect_directory"
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("reason") != "DIRECTORY_INSPECTED"
            or receipt.get("executed") is not True):
        return False
    data = receipt.get("data") or {}
    if not isinstance(data, Mapping):
        return False
    return bool(
        data.get("observed_exists") is True
        and data.get("observed_is_directory") is True
        and data.get("observation_status") == "CONFIRMED"
    )


def _conditional_directory_proof(goal: str, history: list[dict]) -> GoalEvidence:
    # Discover target only from a Runtime tool's action path, and bind it
    # against the entire original request before using observations.
    targets = []
    display_targets = {}
    for row in history:
        action = row.get("action") or {}
        if not isinstance(action, Mapping):
            continue
        path = action.get("path") or (
            action.get("paths", [""])[0]
            if isinstance(action.get("paths"), list) and len(action["paths"]) == 1
            else ""
        )
        canonical = _conditional_directory_target(goal, str(path or ""))
        if canonical and canonical not in targets:
            targets.append(canonical)
            raw_path = str(path or "")
            index = goal.casefold().find(raw_path.casefold())
            display_targets[canonical] = (
                goal[index:index + len(raw_path)] if index >= 0 else raw_path
            )
    if len(targets) != 1:
        return GoalEvidence("UNKNOWN", "CONDITIONAL_GOAL_TARGET_NOT_UNIQUE")
    target = targets[0]
    display_target = display_targets.get(target) or target
    observations = []
    creations = []
    directory_inspections = []
    for index, row in enumerate(history):
        action = row.get("action") or {}
        if not isinstance(action, Mapping) or not action.get("action_id"):
            continue
        fact = _confirmed_exists_fact(row, target)
        if fact is not None:
            observations.append((index, bool(fact), str(action["action_id"])))
        if _verified_created_directory(row, target):
            creations.append((index, str(action["action_id"])))
        if _verified_existing_directory(row, target):
            directory_inspections.append((index, str(action["action_id"])))
    if not observations or observations[-1][1] is not True:
        return GoalEvidence("UNKNOWN", "POSTCONDITION_DIRECTORY_EXISTENCE_NOT_CONFIRMED")
    last_index, _, post_ref = observations[-1]
    # A later operation may have changed the target. The final admitted
    # action must therefore be the trusted postcondition observation.
    if last_index != len(history) - 1:
        return GoalEvidence("UNKNOWN", "UNVERIFIED_ACTION_AFTER_FINAL_OBSERVATION")
    # Newly created: confirmed missing -> created -> confirmed present.
    for create_index, create_ref in reversed(creations):
        if create_index >= last_index:
            continue
        before = [row for row in observations
                  if row[0] < create_index and row[1] is False]
        if before:
            _, _, before_ref = before[-1]
            # A subsequently observed absence revokes the completion proof.
            if any(not value for ix, value, _ in observations if ix > create_index):
                return GoalEvidence("UNKNOWN", "CONTRADICTORY_LATER_OBSERVATION")
            return GoalEvidence(
                "PASS", "CONDITIONAL_DIRECTORY_CREATED_AND_REOBSERVED",
                (before_ref, create_ref, post_ref), display_target,
                "CONDITIONAL_DIRECTORY_EXISTS",
            )
    # Already existed: never infer directory kind from file.exists alone.
    if not creations:
        for inspect_index, inspect_ref in reversed(directory_inspections):
            if inspect_index <= last_index and not any(
                not value for ix, value, _ in observations if ix > inspect_index
            ):
                return GoalEvidence(
                    "PASS", "CONDITIONAL_DIRECTORY_ALREADY_PRESENT",
                    (inspect_ref, post_ref), display_target,
                    "CONDITIONAL_DIRECTORY_EXISTS",
                )
    return GoalEvidence("UNKNOWN", "CONDITIONAL_GOAL_EVIDENCE_CHAIN_INCOMPLETE")


def evaluate_goal_evidence(goal: str, history: list[dict]) -> GoalEvidence:
    """Check real ToolResult receipts; extra successful reads do not erase proof."""
    conditional = _conditional_directory_proof(goal, history)
    if conditional.verified:
        return conditional
    # A conditional goal must not silently fall through to single-create
    # admission, even if one tool happened to succeed.
    if any(
        _conditional_directory_target(goal, str(
            (row.get("action") or {}).get("path") or ""
        ))
        for row in history if isinstance(row, Mapping)
    ):
        return conditional
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
    if proof.goal_kind == "CONDITIONAL_DIRECTORY_EXISTS":
        # Do not accept only copied in-memory history: replay the same
        # current-request ToolResult from Runtime's durable action/result
        # ledgers, including intermediate actions that might invalidate the
        # final existence observation. Never read model-authored prose.
        from .tool_result_contract import decode_result
        source_actions = getattr(loop, "action_ledger", None)
        if not isinstance(source_actions, Mapping):
            raise ValueError("conditional_terminal_action_ledger_missing")
        original_goal = str(getattr(loop, "task_request", "") or "")
        if not original_goal:
            raise ValueError("conditional_terminal_goal_missing")
        reconstructed = []
        for action_id, outcome in ledger.items():
            if not isinstance(outcome, Mapping):
                continue
            if outcome.get("request_id") != loop.run_id or outcome.get("task_epoch") != loop.task_epoch:
                continue
            record_action = source_actions.get(action_id)
            action = (record_action.get("action") or {}
                      if isinstance(record_action, Mapping) else {})
            raw = outcome.get("result")
            if (not isinstance(action, Mapping)
                    or action.get("action_id") != action_id
                    or not isinstance(raw, str)):
                raise ValueError("conditional_terminal_source_action_missing")
            digest = outcome.get("result_sha256")
            if digest and hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
                raise ValueError("conditional_terminal_result_digest_mismatch")
            typed = decode_result(raw)
            if not isinstance(typed, Mapping):
                raise ValueError("conditional_terminal_result_unparseable")
            if outcome.get("result_status") != typed.get("status"):
                raise ValueError("conditional_terminal_result_status_mismatch")
            reconstructed.append({"action": action, "receipt": typed})
        replay = _conditional_directory_proof(original_goal, reconstructed)
        if (not replay.verified or replay.evidence_refs != proof.evidence_refs
                or replay.goal_kind != proof.goal_kind
                or _normal_path(replay.target) != _normal_path(proof.target)):
            raise ValueError("conditional_terminal_proof_replay_mismatch")

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
    from .task_requirement import assess_task_requirement, no_tool_terminal_candidate

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
    if not no_tool_terminal_candidate(requirement) or not eligible:
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
    if proof.goal_kind == "RESOLVED_SINGLE_DIRECTORY" and proof.verified:
        # A later directory observation proves the postcondition, NOT which
        # process created it. Never assert that an uncertain mutation ran.
        return "已透過獨立唯讀觀測確認資料夾存在：\\n" + proof.target
    if proof.goal_kind == "RESOLVED_SINGLE_FILE_WRITE" and proof.verified:
        return "已重新讀取並確認目標檔案內容符合要求：\\n" + proof.target
    if proof.goal_kind == "RESOLVED_SINGLE_OBSERVATION" and proof.verified:
        return "已重新取得並驗證指定目標的觀測結果：\\n" + proof.target
    if proof.goal_kind == "SINGLE_CREATE_DIRECTORY" and proof.verified:
        return "已建立並確認資料夾：\n" + proof.target
    if proof.goal_kind == "CONDITIONAL_DIRECTORY_EXISTS" and proof.verified:
        prefix = ("已建立並重新確認資料夾存在：\n"
                  if proof.reason == "CONDITIONAL_DIRECTORY_CREATED_AND_REOBSERVED"
                  else "已確認既有資料夾存在：\n")
        return prefix + proof.target
    if proof.goal_kind == "DEPLOYMENT_CONTENT_VERIFIED" and proof.verified:
        return ("部署根目錄與已釘選來源內容通過獨立 VERIFY：\n"
                + proof.target)
    raise ValueError("no_verified_runtime_goal_summary")


__all__ = [
    "GoalEvidence", "TerminalVerdict", "evaluate_goal_evidence",
    "accept_runtime_terminal", "accept_runtime_answer_only", "terminal_verdict_from_loop",
    "format_runtime_goal_summary",
]
