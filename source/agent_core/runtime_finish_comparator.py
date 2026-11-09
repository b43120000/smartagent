"""Runtime-owned Finish comparison for the PokeClaw executor.

The model may report expected ToolResult fields; only Runtime receipts determine
facts.  A match is not a goal proof. Unsupported goal contracts fail closed.
Tool execution status, observed boolean values and goal acceptance are separate.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from collections.abc import Mapping
from pathlib import Path

from .executor_run_blackboard import read_executor_run
from .tool_result_contract import decode_result
from .tri_one_runtime_terminal import (
    _single_fact_query_contract, evaluate_goal_evidence,
)

_FIELD = re.compile(r"^[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|[0-9]+))*$")
_MAX_CLAIMS = 64
_MAX_FIELDS = 24


def _path(value: object) -> str:
    value = str(value or "").replace("：", ":").strip()
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        return ""
    return ntpath.normcase(ntpath.normpath(value))


def _shown(value: object) -> object:
    """Never put unbounded command output or credentials in mismatch traces."""
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str) and len(value) <= 80:
        return value
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {"redacted_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def _lookup(receipt: Mapping, field: str) -> tuple[bool, object]:
    if not isinstance(field, str) or not _FIELD.fullmatch(field):
        return False, None
    cursor = receipt
    for part in field.split("."):
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list) and part.isdecimal() and int(part) < len(cursor):
            cursor = cursor[int(part)]
        else:
            return False, None
    return True, cursor


def _fail(reason: str, refs: list[str] | None = None, *,
          mismatches: list[dict] | None = None, tool_results: list[dict] | None = None) -> dict:
    return {
        "verdict": "FAIL", "reason_code": reason,
        "goal_kind": "", "goal_evidence_reason": reason,
        "evidence_refs": list(refs or []),
        "mismatches": list(mismatches or [])[:20],
        "tool_results": list(tool_results or []),
    }


def _readback(loop, root: str | Path, *, allow_open: bool = False) -> tuple[list[dict], list[str], dict | None, str]:
    """Replay all current-request actions, not model-selected evidence alone."""
    board = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if not isinstance(board, Mapping) or (
        not allow_open and not isinstance(board.get("terminal"), Mapping)
    ):
        return [], [], None, "BLACKBOARD_READBACK_MISSING"
    ledger = getattr(loop, "action_result_ledger", None)
    actions = getattr(loop, "_canonical_action_proposals", None)
    if not isinstance(ledger, Mapping) or not isinstance(actions, Mapping):
        return [], [], None, "ACTION_LEDGER_MISSING"
    events = [
        row for row in board.get("events", [])
        if row.get("kind") == "TOOL_RESULT"
    ]
    denied = [
        row for row in board.get("events", [])
        if row.get("kind") == "ADMISSION_REJECTED"
    ]
    if any(
        row.get("runtimeEvidence", {}).get("status") != "REJECTED"
        or row.get("runtimeEvidence", {}).get("executed") is not False
        or row.get("runtimeEvidence", {}).get("evidence_ref")
        or row.get("toolResult") is not None
        for row in denied
    ):
        return [], [], None, "ADMISSION_REJECTION_RECORD_INVALID"
    if not events:
        # A legitimate pure-text Finish has no ActionResult. Verify that no
        # admitted/pending action or rejected ToolCall is being hidden.
        if (actions or ledger or denied or any(
            row.get("kind") in {"ACTION_ADMITTED", "MODEL_TOOL_CALL"}
            for row in board.get("events", [])
        )):
            return [], [], None, "NO_TOOL_FINISH_WITH_PENDING_ACTION"
        if (not allow_open and
                board["terminal"].get("tool_evidence_refs") != []):
            return [], [], None, "BLACKBOARD_EVIDENCE_SET_MISMATCH"
        return [], [], {"board": board, "results": []}, ""
    if len(events) > _MAX_CLAIMS:
        return [], [], None, "EVIDENCE_MISSING_OR_EXCESSIVE"
    results, refs, history = [], [], []
    for row in events:
        event = row.get("runtimeEvidence") or {}
        ref, action_id = event.get("evidence_ref"), row.get("action_id")
        if not ref or not action_id or ref in refs:
            return [], [], None, "EVIDENCE_IDENTITY_MISSING_OR_DUPLICATED"
        entry, action = ledger.get(action_id), actions.get(action_id)
        if not isinstance(entry, Mapping) or not isinstance(action, Mapping):
            return [], [], None, "ACTION_EVIDENCE_NOT_BOUND"
        if any((entry.get(k) != v) for k, v in (
            ("request_id", loop.run_id), ("task_id", loop.task_id),
            ("task_epoch", loop.task_epoch), ("result_id", ref),
            ("action_id", action_id),
        )):
            return [], [], None, "EVIDENCE_IDENTITY_MISMATCH"
        raw = entry.get("result")
        if not isinstance(raw, str) or hashlib.sha256(raw.encode("utf-8")).hexdigest() != entry.get("result_sha256"):
            return [], [], None, "EVIDENCE_DIGEST_MISMATCH"
        receipt = decode_result(raw)
        if not isinstance(receipt, Mapping):
            return [], [], None, "TOOL_RECEIPT_UNPARSEABLE"
        if (receipt.get("status") != entry.get("result_status")
                or receipt.get("status") != event.get("status")
                or receipt.get("executed") != entry.get("executed")
                or receipt.get("executed") != event.get("executed")
                or entry.get("result_sha256") != event.get("result_sha256")
                or action.get("action_id") != action_id
                or action.get("tool") != row.get("toolName")
                or receipt.get("tool") != action.get("tool")
                or (row.get("toolResult") or {}).get("isSuccess") is not
                   (receipt.get("status") == "SUCCEEDED")):
            return [], [], None, "TOOL_RECEIPT_BLACKBOARD_MISMATCH"
        if receipt.get("action_id") and receipt["action_id"] != action_id:
            return [], [], None, "TOOL_RECEIPT_ACTION_MISMATCH"
        if entry.get("evidence_status") != "ACTIVE":
            return [], [], None, "EVIDENCE_NOT_ACTIVE"
        refs.append(ref)
        results.append({
            "evidence_ref": ref, "action_id": action_id,
            "tool": action["tool"], "execution_status": receipt["status"],
            "executed": receipt["executed"],
        })
        history.append({"action": action, "receipt": receipt, "evidence_ref": ref})
    if set(actions) != {row["action_id"] for row in results} or set(ledger) != set(actions):
        return [], [], None, "ACTION_COVERAGE_INCOMPLETE"
    if not allow_open and list(board["terminal"].get("tool_evidence_refs", [])) != refs:
        return [], [], None, "BLACKBOARD_EVIDENCE_SET_MISMATCH"
    return history, refs, {"board": board, "results": results}, ""


def _single_inspect_query(goal: str, history: list[dict]) -> tuple[str, str] | None:
    """A typed inspect can answer a simple existence question without observe_fact."""
    if not history or history[-1]["action"].get("tool") != "inspect_directory":
        return None
    if any(row["action"].get("tool") not in {"inspect_directory", "observe_fact"} for row in history):
        return None
    item = history[-1]
    action, receipt = item["action"], item["receipt"]
    paths = action.get("paths")
    if not isinstance(paths, list) or len(paths) != 1:
        return None
    target, kind = _single_fact_query_contract(
        goal.replace("：", ":"), str(paths[0]),
    )
    data = receipt.get("data")
    if (not target or kind not in {"DIRECTORY", "PATH"}
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("executed") is not True
            or receipt.get("reason") != "DIRECTORY_INSPECTED"
            or not isinstance(data, Mapping)
            or data.get("observation_status") != "CONFIRMED"
            or type(data.get("observed_exists")) is not bool):
        return None
    if data["observed_exists"] and data.get("observed_is_directory") is not True:
        return None
    if any(
        _path((row["action"].get("paths") or [None])[0]
              if row["action"].get("tool") == "inspect_directory"
              else row["action"].get("path")) != target
        for row in history
    ):
        return None
    return "SINGLE_FACT_QUERY", (
        "SINGLE_FACT_DIRECTORY_EXISTS" if data["observed_exists"]
        else "SINGLE_FACT_DIRECTORY_ABSENT"
    )


def _goal_proof(goal: str, history: list[dict]) -> tuple[str, str] | None:
    """Finite, original-request-bound contracts. No model-only goal completion."""
    existing = evaluate_goal_evidence(goal, history)
    if existing.verified:
        return existing.goal_kind, existing.reason
    checked = _single_inspect_query(goal, history)
    if checked:
        return checked
    # Declarative desired state: not a fact question and not a conditional mkdir.
    if len(history) == 1 and history[0]["action"].get("tool") == "inspect_directory":
        action = history[0]["action"]
        paths = action.get("paths") or []
        if len(paths) == 1 and _path(paths[0]):
            text = goal.replace("：", ":")
            path = str(paths[0])
            if text.casefold().count(path.casefold()) == 1:
                residue = text.casefold().replace(path.casefold(), " ")
                if not re.search(r"如果|若|倘若|建立|創建|新增|是否|有沒有", residue):
                    intent = re.fullmatch(
                        r"\s*(?:請|請你|幫我|請幫我)?\s*(?:確認|確保|驗證|保證)\s*"
                        r"(?:這個|該|目標)?\s*(?:資料夾|目錄|路徑)\s*"
                        r"(不存在|存在)\s*[。.!！]?\s*", residue,
                    )
                    receipt = history[0]["receipt"]
                    data = receipt.get("data") or {}
                    if (intent and receipt.get("status") == "SUCCEEDED"
                            and receipt.get("reason") == "DIRECTORY_INSPECTED"
                            and receipt.get("executed") is True
                            and data.get("observation_status") == "CONFIRMED"
                            and type(data.get("observed_exists")) is bool):
                        observed = data["observed_exists"]
                        desired = intent.group(1) == "存在"
                        if observed == desired and (not observed or data.get("observed_is_directory") is True):
                            return "DIRECTORY_STATE_ASSERTION", (
                                "DESIRED_DIRECTORY_STATE_CONFIRMED"
                            )
    # Nonzero exit is a tool failure, but may satisfy an explicitly negative
    # command goal. Merely executing a command never proves arbitrary goals.
    if len(history) == 1 and history[0]["action"].get("tool") == "run_command":
        action, receipt = history[0]["action"], history[0]["receipt"]
        command = str(action.get("command") or "")
        data = receipt.get("data") or {}
        if (command and goal.count(command) == 1
                and re.fullmatch(r"\s*執行\s*.+\s*(?:並|且)\s*(?:確認|驗證|回報|顯示).+", goal, re.S)
                and receipt.get("executed") is True
                and isinstance(data, Mapping)
                and data.get("process_state") == "EXITED"
                and type(data.get("exit_code")) is int
                and data.get("timed_out") is False):
            prefix = goal.replace(command, " ")
            wants_failure = bool(re.search(r"非零|預期失敗|應失敗|失敗原因", prefix))
            wants_success = bool(re.search(r"成功|零退出碼|exit_code\s*=\s*0", prefix))
            if wants_failure and wants_success:
                return None
            if wants_failure and receipt.get("status") == "FAILED" and data["exit_code"] != 0:
                return "COMMAND_EXPECTED_FAILURE", "EXPECTED_NONZERO_EXIT_CONFIRMED"
            if wants_success and receipt.get("status") == "SUCCEEDED" and data["exit_code"] == 0:
                return "COMMAND_EXPECTED_SUCCESS", "EXPECTED_ZERO_EXIT_CONFIRMED"
            if (not wants_failure and not wants_success
                    and re.search(r"回報|顯示", prefix)):
                return "COMMAND_RESULT_QUERY", "COMMAND_TERMINATED_AND_REPORTED"
    return None


def _recognized_but_unsatisfied(goal: str, history: list[dict]) -> bool:
    """Do not let model-declared checks override a recognized unmet goal."""
    from .tri_one_runtime_terminal import _conditional_directory_target, _simple_mkdir_target
    if len(history) != 1:
        return False
    action = history[0]["action"]
    path = action.get("path") or (
        (action.get("paths") or [None])[0]
        if isinstance(action.get("paths"), list) else None
    )
    if not _path(path):
        return False
    normalized = goal.replace("：", ":")
    if _single_fact_query_contract(normalized, str(path))[0]:
        return True
    if _simple_mkdir_target(normalized, str(path)):
        return True
    if _conditional_directory_target(normalized, str(path)):
        return True
    residue = normalized.casefold().replace(str(path).casefold(), " ")
    return bool(re.fullmatch(
        r"\s*(?:請|請你|幫我|請幫我)?\s*(?:確認|確保|驗證|保證)\s*"
        r"(?:這個|該|目標)?\s*(?:資料夾|目錄|路徑)\s*"
        r"(不存在|存在)\s*[。.!！]?\s*", residue
    ))


def _evaluate_model_goal_checks(history: list[dict], refs: list[str],
                                claimed: dict, checks: object) -> tuple[bool, str, list[dict]]:
    """Model supplies task-specific postconditions, Runtime checks actual typed data.

    This is evidence verification of model-generated criteria; it is not an
    independent semantic proof that every clause of arbitrary prose was covered.
    """
    if not isinstance(checks, list) or not (1 <= len(checks) <= _MAX_CLAIMS):
        return False, "MODEL_GOAL_CHECKS_REQUIRED", []
    index = {row["evidence_ref"]: row["receipt"] for row in history}
    mismatches = []
    observed_criteria = set()
    used_refs = set()
    for n, check in enumerate(checks):
        if not isinstance(check, dict) or set(check) != {
            "criterion", "evidence_ref", "field", "operator", "expected",
        }:
            return False, "MODEL_GOAL_CHECK_SCHEMA_INVALID", []
        criterion, ref = check["criterion"], check["evidence_ref"]
        field, op, expected = check["field"], check["operator"], check["expected"]
        if (not isinstance(criterion, str) or not (1 <= len(criterion) <= 240)
                or criterion in observed_criteria or ref not in index
                or not isinstance(field, str) or not field.startswith("data.")
                or not _FIELD.fullmatch(field) or op not in {"eq", "ne", "ge", "le"}
                or type(expected) not in (str, int, float, bool, type(None))
                or field not in claimed[ref]):
            return False, "MODEL_GOAL_CHECK_SCHEMA_INVALID", []
        observed_criteria.add(criterion)
        used_refs.add(ref)
        exists, value = _lookup(index[ref], field)
        if not exists or type(value) is not type(expected):
            valid = False
        elif op == "eq":
            valid = value == expected
        elif op == "ne":
            valid = value != expected
        elif type(value) in (int, float):
            valid = value >= expected if op == "ge" else value <= expected
        else:
            return False, "MODEL_GOAL_CHECK_OPERATOR_TYPE_INVALID", []
        if not valid:
            mismatches.append({
                "evidence_ref": ref, "field": field, "criterion": criterion,
                "operator": op, "expected": _shown(expected),
                "actual": _shown(value) if exists else "[MISSING]",
            })
    if mismatches:
        return False, "GOAL_CHECK_RUNTIME_MISMATCH", mismatches
    return True, "MODEL_DECLARED_GOAL_CHECKS_CONFIRMED", []


def finish_runtime_field_catalog(loop, *, max_records: int = 64) -> list[dict]:
    """Build dynamic field names from actual receipts; no per-tool Finish template.

    Values are not copied into this catalogue. ToolResult already contains the
    bounded values the model observed. For older actions, it may ask for readback.
    """
    ledger = getattr(loop, "action_result_ledger", {}) or {}
    if not isinstance(ledger, Mapping):
        return []
    catalog = []
    for row in sorted(
        (v for v in ledger.values() if isinstance(v, Mapping)),
        key=lambda v: int(v.get("produced_round") or 0),
    )[:max_records]:
        raw = row.get("result")
        receipt = decode_result(raw) if isinstance(raw, str) else None
        if not isinstance(receipt, Mapping):
            continue
        fields = []
        def walk(value, prefix, depth):
            if len(fields) >= 48 or depth > 4:
                return
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_]\w*", key)
                            or re.search(r"password|secret|token|api.?key", key, re.I)):
                        continue
                    walk(item, f"{prefix}.{key}" if prefix else key, depth + 1)
            elif isinstance(value, list):
                for i, item in enumerate(value[:6]):
                    walk(item, f"{prefix}.{i}", depth + 1)
            elif type(value) in (str, int, float, bool, type(None)):
                fields.append(prefix)
        walk(receipt, "", 0)
        catalog.append({
            "evidence_ref": row.get("evidence_ref"),
            "tool": row.get("tool"),
            "status": receipt.get("status"),
            "available_scalar_fields": fields,
        })
    return catalog


def compare_runtime_finish(loop, root: str | Path, goal: str,
                           expected_runtime: object, goal_checks: object = None,
                           *, allow_open: bool = False,
                           answer: str = "") -> dict:
    """Binary verdict. Dynamic model fields, Runtime facts and goal validation."""
    history, refs, context, error = _readback(loop, root, allow_open=allow_open)
    if error:
        return _fail(error)
    if context["board"].get("request_sha256") != hashlib.sha256(
        str(goal).encode("utf-8")
    ).hexdigest():
        return _fail("REQUEST_DIGEST_MISMATCH", refs, tool_results=context["results"])
    results = context["results"]
    from .finish_evidence_requirement import valid_finish_evidence_requirement
    requirement = context["board"].get("evidence_requirement")
    if not valid_finish_evidence_requirement(requirement, goal):
        return _fail("UPSTREAM_EVIDENCE_REQUIREMENT_INVALID", refs, tool_results=results)
    if not history:
        if requirement["tool_evidence_required"] or not requirement["answer_only_eligible"]:
            return _fail("TOOL_EVIDENCE_REQUIRED", refs)
        if expected_runtime not in (None, []) or goal_checks not in (None, []):
            return _fail("TEXT_ONLY_FINISH_HAS_UNBACKED_TOOL_CLAIMS", refs)
        if not isinstance(answer, str):
            return _fail("ANSWER_ONLY_CONTENT_INVALID", refs)
        if not allow_open and (
            context["board"]["terminal"].get("summary_sha256")
            != hashlib.sha256(answer.encode("utf-8")).hexdigest()
        ):
            return _fail("ANSWER_ONLY_DIGEST_MISMATCH", refs)
        from .tri_one_runtime_terminal import validate_runtime_answer_only
        eligible, reason = validate_runtime_answer_only(
            loop, goal=goal, answer=answer,
            had_attachments=bool(getattr(loop, "_tri_one_has_attached_context", False)),
            upstream_requirement=requirement,
        )
        if not eligible:
            return _fail("ANSWER_ONLY_ADMISSION_REJECTED:" + str(reason)[:100], refs)
        return {
            "verdict": "PASS", "reason_code": "ANSWER_ONLY_RESPONSE_ADMITTED",
            "goal_kind": "ANSWER_ONLY",
            "goal_evidence_reason": "TEXT_RESPONSE_ACCEPTED_NO_TOOL_NEEDED",
            "coverage_basis": "RUNTIME_TEXT_ADMISSION",
            "completion_kind": "ANSWER_ONLY",
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "evidence_refs": [], "mismatches": [], "tool_results": [],
        }
    if (not isinstance(expected_runtime, list) or
            len(expected_runtime) != len(history) or
            len(expected_runtime) > _MAX_CLAIMS):
        return _fail("EXPECTED_RUNTIME_COVERAGE_MISMATCH", refs, tool_results=results)
    claimed = {}
    for claim in expected_runtime:
        if not isinstance(claim, dict) or set(claim) != {"evidence_ref", "fields"}:
            return _fail("EXPECTED_RUNTIME_SCHEMA_INVALID", refs, tool_results=results)
        ref, fields = claim["evidence_ref"], claim["fields"]
        if (not isinstance(ref, str) or ref in claimed
                or not isinstance(fields, dict) or not (1 <= len(fields) <= _MAX_FIELDS)
                or "status" not in fields):
            return _fail("EXPECTED_RUNTIME_FIELDS_INVALID", refs, tool_results=results)
        if any(not isinstance(key, str) or not _FIELD.fullmatch(key)
               or type(value) not in (str, int, float, bool, type(None))
               for key, value in fields.items()):
            return _fail("EXPECTED_RUNTIME_FIELD_TYPE_INVALID", refs, tool_results=results)
        claimed[ref] = fields
    if set(claimed) != set(refs):
        return _fail("EXPECTED_RUNTIME_EVIDENCE_SET_MISMATCH", refs, tool_results=results)
    mismatches = []
    for item in history:
        ref, actual = item["evidence_ref"], item["receipt"]
        for field, expected in claimed[ref].items():
            present, observed = _lookup(actual, field)
            if not present or type(expected) is not type(observed) or expected != observed:
                mismatches.append({
                    "evidence_ref": ref, "field": field,
                    "expected": _shown(expected),
                    "actual": _shown(observed) if present else "[MISSING]",
                })
    if mismatches:
        return _fail("MODEL_RUNTIME_MISMATCH", refs,
                     mismatches=mismatches, tool_results=results)
    # Historical REJECTED/FAILED operations remain in the claimed evidence
    # set (never omitted or rewritten), but cannot veto later independent
    # successful evidence for the original goal. Uncertain execution is still
    # fail-closed and cannot be silently treated as a previous rejection.
    if any(item["receipt"]["status"] in {"UNKNOWN", "RUNNING", "PARTIAL"}
           for item in history):
        return _fail("RUNTIME_EXECUTION_NOT_CONFIRMED", refs, tool_results=results)
    valid_goal_history = [
        item for item in history
        if item["receipt"]["status"] == "SUCCEEDED"
        and item["receipt"]["executed"] is True
    ]
    if not valid_goal_history:
        return _fail("NO_SUCCEEDED_GOAL_EVIDENCE", refs, tool_results=results)
    if any(item["receipt"]["status"] not in {"SUCCEEDED", "FAILED", "REJECTED"}
           for item in history):
        return _fail("RUNTIME_RESULT_STATUS_UNSUPPORTED", refs, tool_results=results)
    proof = _goal_proof(goal, valid_goal_history)
    if proof is None and _recognized_but_unsatisfied(goal, valid_goal_history):
        return _fail("KNOWN_GOAL_POSTCONDITION_NOT_SATISFIED", refs, tool_results=results)
    # Even an independent goal proof requires the model to reference a
    # task-relevant fact, not merely compare the generic Tool status.
    if not any(
        field.startswith("data.") for fields in claimed.values() for field in fields
    ):
        return _fail("MODEL_FINISH_GOAL_FACT_NOT_DECLARED", refs, tool_results=results)
    if goal_checks is not None:
        # A rejected/failed tool may be faithfully reported, but may not
        # satisfy the model-declared *positive* completion goal.
        good_refs = {item["evidence_ref"] for item in valid_goal_history}
        if not any(isinstance(check, dict)
                   and check.get("evidence_ref") in good_refs
                   for check in goal_checks if isinstance(goal_checks, list)):
            return _fail("GOAL_CHECK_LACKS_SUCCEEDED_EVIDENCE", refs,
                         tool_results=results)
        ok, reason, deviations = _evaluate_model_goal_checks(
            history, refs, claimed, goal_checks,
        )
        if not ok:
            return _fail(reason, refs, mismatches=deviations, tool_results=results)
    elif proof is None:
        return _fail("MODEL_GOAL_CHECKS_REQUIRED", refs, tool_results=results)
    if proof is not None:
        kind, reason = proof
        basis = "RUNTIME_INDEPENDENT_GOAL_PROOF"
    else:
        # The model proposed the Goal criteria; Runtime verified the evidence
        # values but cannot independently certify arbitrary natural language.
        kind, reason = "MODEL_GENERATED_GOAL_CHECKS", "MODEL_DECLARED_GOAL_CHECKS_CONFIRMED"
        basis = "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH"
    return {
        "verdict": "PASS", "reason_code": "MODEL_RUNTIME_AND_GOAL_MATCH",
        "goal_kind": kind, "goal_evidence_reason": reason,
        "coverage_basis": basis,
        "evidence_refs": refs, "mismatches": [],
        "tool_results": results,
    }


__all__ = ["compare_runtime_finish", "finish_runtime_field_catalog"]
