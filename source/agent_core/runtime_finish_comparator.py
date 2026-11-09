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


def _readback(loop, root: str | Path) -> tuple[list[dict], list[str], dict | None, str]:
    """Replay all current-request actions, not model-selected evidence alone."""
    board = read_executor_run(
        root, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    if not isinstance(board, Mapping) or not isinstance(board.get("terminal"), Mapping):
        return [], [], None, "BLACKBOARD_READBACK_MISSING"
    ledger = getattr(loop, "action_result_ledger", None)
    actions = getattr(loop, "_canonical_action_proposals", None)
    if not isinstance(ledger, Mapping) or not isinstance(actions, Mapping):
        return [], [], None, "ACTION_LEDGER_MISSING"
    events = [row for row in board.get("events", []) if row.get("kind") == "TOOL_RESULT"]
    if not events or len(events) > _MAX_CLAIMS:
        return [], [], None, "EVIDENCE_MISSING_OR_EXCESSIVE"
    if any(row.get("runtimeEvidence", {}).get("status") == "REJECTED" for row in events):
        return [], [], None, "ADMISSION_REJECTED_NOT_GOAL_PROOF"
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
    if list(board["terminal"].get("tool_evidence_refs", [])) != refs:
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


def compare_runtime_finish(loop, root: str | Path, goal: str,
                           expected_runtime: object) -> dict:
    """Return only PASS/FAIL, with diagnostic reasons and independent tool facts."""
    history, refs, context, error = _readback(loop, root)
    if error:
        return _fail(error)
    if context["board"].get("request_sha256") != hashlib.sha256(
        str(goal).encode("utf-8")
    ).hexdigest():
        return _fail("REQUEST_DIGEST_MISMATCH", refs, tool_results=context["results"])
    results = context["results"]
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
               or isinstance(value, (list, dict)) for key, value in fields.items()):
            return _fail("EXPECTED_RUNTIME_FIELD_TYPE_INVALID", refs, tool_results=results)
        claimed[ref] = fields
    if set(claimed) != set(refs):
        return _fail("EXPECTED_RUNTIME_EVIDENCE_SET_MISMATCH", refs, tool_results=results)
    # The model must claim the decisive observed fields, not only a convenient
    # success flag. Tool-specific evidence remains raw and authoritative.
    required = {
        "inspect_directory": {
            "status", "data.observed_exists", "data.observed_is_directory",
            "data.observation_status",
        },
        "create_directory": {
            "status", "data.observed_exists", "data.observed_is_directory",
            "data.observation_status",
        },
        "observe_fact": {"status", "data.observation_status"},
        "run_command": {"status", "data.exit_code", "data.process_state"},
    }
    for item in history:
        fields = claimed[item["evidence_ref"]]
        missing = required.get(item["action"]["tool"], {"status"}) - set(fields)
        if missing:
            return _fail("EXPECTED_RUNTIME_FIELDS_INCOMPLETE", refs,
                         mismatches=[{
                             "evidence_ref": item["evidence_ref"],
                             "field": key, "expected": "[REQUIRED]",
                             "actual": "[NOT_DECLARED]",
                         } for key in sorted(missing)], tool_results=results)
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
    if any(item["receipt"]["status"] in {"UNKNOWN", "RUNNING", "PARTIAL"}
           or item["receipt"]["executed"] is not True for item in history):
        return _fail("RUNTIME_EXECUTION_NOT_CONFIRMED", refs, tool_results=results)
    goal_proof = _goal_proof(goal, history)
    if goal_proof is None:
        return _fail("GOAL_COVERAGE_NOT_PROVEN", refs, tool_results=results)
    kind, reason = goal_proof
    return {
        "verdict": "PASS", "reason_code": "MODEL_RUNTIME_AND_GOAL_MATCH",
        "goal_kind": kind, "goal_evidence_reason": reason,
        "evidence_refs": refs, "mismatches": [],
        "tool_results": results,
    }


__all__ = ["compare_runtime_finish"]
