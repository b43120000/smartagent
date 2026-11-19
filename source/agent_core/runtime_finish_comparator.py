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
_MAX_CLAIMS = 64  # Legacy V1 claims and task-goal check count
_MAX_CANONICAL_ACTIONS = 1000  # V2 Runtime-owned full ledger
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


def _safe_finish_ref(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"MIN-EV-[a-f0-9]{24}", value):
        return value
    return "[INVALID_OR_MISSING_REF]"


def _safe_finish_field(value: object) -> str:
    return value if isinstance(value, str) and len(value) <= 96 and _FIELD.fullmatch(value) else ""


def finish_input_projection(expected_runtime: object, goal_checks: object) -> dict:
    """Record Finish JSON structure with user text and values omitted."""
    claims = []
    for row in expected_runtime[:_MAX_CLAIMS] if isinstance(expected_runtime, list) else ():
        if not isinstance(row, dict):
            continue
        fields = row.get("fields")
        claims.append({
            "evidence_ref": _safe_finish_ref(row.get("evidence_ref")),
            "fields": [
                {"field": _safe_finish_field(field), "value_type": type(value).__name__}
                for field, value in list(fields.items())[:_MAX_FIELDS]
            ] if isinstance(fields, dict) else [],
        })
    checks = [
        {"evidence_ref": _safe_finish_ref(row.get("evidence_ref")),
         "field": _safe_finish_field(row.get("field")),
         "operator": row["operator"] if row.get("operator") in ("eq", "ne", "ge", "le") else "",
         "expected_type": type(row.get("expected")).__name__ if "expected" in row else "MISSING"}
        for row in (goal_checks[:_MAX_CLAIMS] if isinstance(goal_checks, list) else ())
        if isinstance(row, dict)
    ]
    return {"expected_runtime": claims, "goal_checks": checks, "values_included": False}


def _goal_binding_diagnostic(history, good_refs, goal_checks):
    by_ref = {item["evidence_ref"]: item["receipt"] for item in history}
    issues = []
    for index, check in enumerate(goal_checks[:_MAX_CLAIMS] if isinstance(goal_checks, list) else ()):
        if not isinstance(check, dict):
            continue
        ref = check.get("evidence_ref")
        if isinstance(ref, str) and ref in good_refs:
            continue
        field = _safe_finish_field(check.get("field"))
        candidates = []
        for eligible in sorted(good_refs):
            present, value = _lookup(by_ref[eligible], field)
            if present and field.startswith("data.") and type(value) in (str, int, float, bool, type(None)):
                candidates.append({"evidence_ref": eligible, "actual_type": type(value).__name__})
        issues.append({
            "check_index": index, "evidence_ref": _safe_finish_ref(ref),
            "field": field, "expected_type": type(check.get("expected")).__name__,
            "reason_code": (
                "EVIDENCE_EXECUTION_NOT_ELIGIBLE" if isinstance(ref, str) and ref in by_ref
                else "EVIDENCE_REF_UNKNOWN_OR_INVALID"
            ),
            "field_candidates": candidates[:4],
        })
    return {
        "repair_class": "MODEL_GOAL_EVIDENCE_BINDING_REPAIR", "repair_only": True,
        "good_refs": sorted(good_refs), "invalid_checks": issues,
        "authorization_granted": False,
    }


def _fail(reason: str, refs: list[str] | None = None, *,
          mismatches: list[dict] | None = None, tool_results: list[dict] | None = None,
          finish_diagnostic: dict | None = None,
          goal_state_table: list[dict] | None = None,
          goal_contract_sha256: str = "",
          model_goal_checks_advisory: dict | None = None) -> dict:
    origin = (
        "PLANNER_FACT_DECLARATION_ERROR"
        if reason in {"MODEL_RUNTIME_MISMATCH", "MODEL_FINISH_GOAL_FACT_NOT_DECLARED"}
        else "PLANNER_PREDICATE_OR_GOAL_UNRESOLVED"
        if reason == "GOAL_CHECK_RUNTIME_MISMATCH"
        else "GOAL_STATE_NOT_SATISFIED"
        if reason in {"FROZEN_GOAL_STATE_NOT_SATISFIED",
                      "KNOWN_GOAL_POSTCONDITION_NOT_SATISFIED"}
        else "EVIDENCE_OR_FINISH_CONTRACT_INCOMPLETE"
    )
    result = {
        "verdict": "FAIL", "reason_code": reason,
        "failure_origin": origin,
        "goal_kind": "", "goal_evidence_reason": reason,
        "evidence_refs": list(refs or []),
        "mismatches": list(mismatches or [])[:20],
        "tool_results": list(tool_results or []),
    }
    if finish_diagnostic is not None:
        result["finish_diagnostic"] = dict(finish_diagnostic)
    if goal_state_table is not None:
        result["goal_state_table"] = list(goal_state_table)[:32]
    if goal_contract_sha256:
        result["goal_contract_sha256"] = str(goal_contract_sha256)
    if model_goal_checks_advisory is not None:
        result["model_goal_checks_advisory"] = dict(model_goal_checks_advisory)
    return result


def _readback(loop, root: str | Path, *, allow_open: bool = False,
              max_actions: int = _MAX_CLAIMS) -> tuple[list[dict], list[str], dict | None, str]:
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
    event_total = int(board.get("event_count_total", len(board.get("events", []))))
    if event_total > 10000 or event_total < len(board.get("events", [])):
        return [], [], None, "EVIDENCE_EVENT_HISTORY_UNBOUNDED_OR_DAMAGED"
    if event_total != len(board.get("events", [])):
        from .executor_run_blackboard import read_executor_event
        try:
            source_events = [
                read_executor_event(
                    root, task_id=loop.task_id, request_id=loop.run_id,
                    task_epoch=loop.task_epoch, sequence=index,
                ) for index in range(1, event_total + 1)
            ]
        except (OSError, ValueError, KeyError):
            return [], [], None, "EVIDENCE_EVENT_ARCHIVE_READBACK_FAILED"
    else:
        source_events = list(board.get("events", []))
    events = [row for row in source_events if row.get("kind") == "TOOL_RESULT"]
    denied = [row for row in source_events if row.get("kind") == "ADMISSION_REJECTED"]
    native_denials = [
        row for row in source_events
        if row.get("kind") == "TURBO_NATIVE_CONFIRMATION_BLOCKED"
        and row.get("toolName") == "run_command"
        and row.get("executed") is False
        and row.get("authorization_granted") is False
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
            for row in source_events
        )):
            return [], [], None, "NO_TOOL_FINISH_WITH_PENDING_ACTION"
        if (not allow_open and
                board["terminal"].get("tool_evidence_refs") != []):
            return [], [], None, "BLACKBOARD_EVIDENCE_SET_MISMATCH"
        return [], [], {"board": board, "results": []}, ""
    if len(events) > max_actions:
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
        history.append({"action": action, "receipt": receipt,
                        "evidence_ref": ref, "raw_result": raw})
    if set(actions) != {row["action_id"] for row in results} or set(ledger) != set(actions):
        return [], [], None, "ACTION_COVERAGE_INCOMPLETE"
    if not allow_open and list(board["terminal"].get("tool_evidence_refs", [])) != refs:
        return [], [], None, "BLACKBOARD_EVIDENCE_SET_MISMATCH"
    return history, refs, {
        "board": board, "results": results,
        "admission_denials": denied,
        "native_denials": native_denials,
    }, ""


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
                or criterion in observed_criteria
                or not isinstance(ref, str) or ref not in index
                or not isinstance(field, str) or not field.startswith("data.")
                or not _FIELD.fullmatch(field)
                or not isinstance(op, str) or op not in {"eq", "ne", "ge", "le"}
                or type(expected) not in (str, int, float, bool, type(None))
                or (claimed is not None and field not in claimed.get(ref, {}))):
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


def _partial_inspection_missing_paths(item: dict) -> dict[str, str] | None:
    action, receipt = item["action"], item["receipt"]
    if (action["tool"] != "inspect_directory" or action.get("recursive") is not False
            or receipt.get("executed") is not True
            or receipt.get("reason") != "DIRECTORY_INSPECTION_INCOMPLETE"):
        return None
    try:
        raw = json.loads(item["raw_result"])
    except (TypeError, ValueError, KeyError):
        return None
    paths = action.get("paths")
    if not isinstance(paths, list):
        return None
    expected = {_path(path) for path in paths}
    rows = raw.get("paths") if isinstance(raw, dict) else None
    if (not isinstance(rows, list) or not expected or "" in expected
            or len(paths) != len(expected) or len(rows) != len(expected)
            or raw.get("tool") != action["tool"] or raw.get("recursive") is not False):
        return None
    missing, observed = {}, set()
    for entry in rows:
        if not isinstance(entry, dict):
            return None
        path = _path(entry.get("path"))
        if (path not in expected or path in observed or entry.get("authorized") is not True
                or type(entry.get("inaccessible_count")) is not int
                or entry["inaccessible_count"] != 0):
            return None
        observed.add(path)
        if entry.get("exists") is False and entry.get("errors") in (
            ["path_not_found"], ["not_a_directory"],
        ):
            missing[path] = entry["errors"][0]
        elif entry.get("exists") is not True or entry.get("errors") != []:
            return None
    return missing or None


_EXACT_SHA256_READ = re.compile(
    r"Get-FileHash\s+-LiteralPath\s+'(?P<path>[^'\r\n]+)'\s+-Algorithm\s+SHA256"
    r"(?:\s*\|\s*Select-Object\s+-ExpandProperty\s+Hash)?",
    re.IGNORECASE,
)


def _observed_paths(item: dict) -> dict[str, str]:
    action, receipt = item["action"], item["receipt"]
    if receipt.get("status") != "SUCCEEDED" or receipt.get("executed") is not True:
        return {}
    tool, data = action["tool"], receipt["data"]
    if tool == "list_directory" and receipt["reason"] == "DIRECTORY_LISTED":
        path = _path(action.get("path"))
        entries = data.get("entries")
        if (not path or _path(receipt.get("requested_path")) != path
                or _path(receipt.get("resolved_path")) != path
                or data.get("recursive") is not False
                or data.get("terminal_eligible") is not True or not isinstance(entries, list)):
            return {}
        return {path: "DIRECTORY"} | {
            ntpath.join(path, name.casefold()): row["kind"] for row in entries
            if isinstance(row, dict) and row.get("kind") in {"FILE", "DIRECTORY"}
            and isinstance((name := row.get("name")), str)
            and re.fullmatch(r"[^\\/:]+", name) and name not in {".", ".."}
        }
    if tool == "inspect_directory" and receipt["reason"] in {
        "DIRECTORY_INSPECTED", "DIRECTORY_ABSENCE_OBSERVED",
    }:
        paths = action.get("paths")
        path = _path(data.get("observed_path"))
        if (path and isinstance(paths, list) and len(paths) == 1
                and _path(paths[0]) == path and data.get("observation_status") == "CONFIRMED"):
            if (receipt["reason"] == "DIRECTORY_INSPECTED" and data.get("observed_exists") is True
                    and data.get("observed_is_directory") is True):
                return {path: "DIRECTORY"}
            if receipt["reason"] == "DIRECTORY_ABSENCE_OBSERVED" and data.get("observed_exists") is False:
                return {path: "ABSENT"}
    if tool == "observe_fact" and receipt["reason"] in {"FACT_OBSERVED", "FACT_PREDICATE_PASSED"}:
        path = _path(action.get("path"))
        facts = data.get("observations")
        if (path and action.get("action_id") == receipt.get("action_id")
                and _path(receipt.get("requested_path")) == path
                and _path(receipt.get("resolved_path")) == path
                and data.get("execution_finality") == "COMPLETED"
                and data.get("terminal_eligible") is True
                and data.get("observation_status") == "CONFIRMED"
                and isinstance(facts, list) and len(facts) == 1):
            fact = facts[0]
            if (isinstance(fact, dict)
                    and fact.get("schema") == "SMARTAGENT_EVIDENCE_V1"
                    and fact.get("field") == "file.exists"
                    and fact.get("source") == "filesystem.stat"
                    and fact.get("observation_status") == "CONFIRMED"
                    and fact.get("freshness") == "CURRENT"
                    and fact.get("raw_evidence_ref") == action["action_id"]
                    and _path(fact.get("scope")) == path):
                if fact.get("value") is True and fact.get("path_kind") in ("FILE", "DIRECTORY", "OTHER"):
                    return {path: fact["path_kind"]}
                if fact.get("value") is False and "path_kind" not in fact:
                    return {path: "ABSENT"}
    if tool == "run_command" and receipt["reason"] == "COMMAND_EXITED":
        command = str(action.get("command") or "").strip()
        if command.casefold().endswith(").hash.tolowerinvariant()") and command.startswith("("):
            command = command[1:-len(").Hash.ToLowerInvariant()")]
        match = _EXACT_SHA256_READ.fullmatch(command)
        path = _path(match["path"]) if match else ""
        verify = action.get("verify")
        safe_verify = verify is None or verify == [] or (
            isinstance(verify, list) and len(verify) == 1 and isinstance(verify[0], dict)
            and set(verify[0]) == {"action", "path", "expect"}
            and verify[0]["action"] == "file_exists" and verify[0]["expect"] is True
            and _path(verify[0]["path"]) == path
        )
        if (path and action.get("operation") == data.get("operation") == "INSPECT"
                and safe_verify and data.get("process_state") == "EXITED"
                and type(data.get("exit_code")) is int and data["exit_code"] == 0
                and data.get("timed_out") is False and data.get("output_complete") is True
                and data.get("stdout_truncated") is False
                and isinstance(data.get("stdout"), str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", data["stdout"].strip())):
            return {path: "FILE"}
    return {}


def _execution_uncertainty(history: list[dict]) -> tuple[list[dict], list[dict]]:
    # Walk newest first, so each observation can only resolve an older one.
    observations = {}
    blocked, recovered = [], []
    for item in reversed(history):
        ref, status = item["evidence_ref"], item["receipt"]["status"]
        if status in {"UNKNOWN", "RUNNING", "PARTIAL"}:
            missing = _partial_inspection_missing_paths(item) if status == "PARTIAL" else None
            unresolved = {
                path for path, reason in (missing or {}).items()
                if path not in observations
                or observations[path][0] not in ("FILE", "DIRECTORY")
                or (reason == "not_a_directory" and observations[path][0] != "FILE")
            }
            if missing and not unresolved:
                recovered.append({"evidence_ref": ref, "resolved_by": sorted({
                    observations[path][1] for path in missing
                })})
            else:
                blocked.append({
                    "evidence_ref": ref, "status": status, "tool": item["action"]["tool"],
                    "reason_code": "FOLLOWUP_SCOPE_NOT_OBSERVED" if missing
                                   else "UNCERTAINTY_NOT_RECOVERABLE_AS_READ",
                    "unobserved_path_count": len(unresolved),
                })
        for path, kind in _observed_paths(item).items():
            observations.setdefault(path, (kind, ref))
    return blocked[::-1], recovered[::-1]


def compare_runtime_finish(loop, root: str | Path, goal: str,
                           expected_runtime: object, goal_checks: object = None,
                           *, allow_open: bool = False,
                           answer: str = "", canonical_finish: object = None) -> dict:
    """Binary verdict. Dynamic model fields, Runtime facts and goal validation."""
    v2 = canonical_finish is not None
    history, refs, context, error = _readback(
        loop, root, allow_open=allow_open,
        max_actions=_MAX_CANONICAL_ACTIONS if v2 else _MAX_CLAIMS,
    )
    if error:
        return _fail(error)
    if v2:
        from .finish_contract_adapter_v2 import validate_finish_contract_v2
        if not validate_finish_contract_v2(canonical_finish, loop, context["board"]):
            return _fail("CANONICAL_FINISH_BINDING_INVALID", refs,
                         tool_results=context["results"])
        if expected_runtime not in (None, []):
            return _fail("V2_LEGACY_CLAIMS_FORBIDDEN", refs,
                         tool_results=context["results"])
        intent = canonical_finish["planner_intent"]
        if intent["summary"] != answer or intent["goal_checks"] != goal_checks:
            return _fail("CANONICAL_FINISH_PROPOSAL_MISMATCH", refs,
                         tool_results=context["results"])
    if context["board"].get("request_sha256") != hashlib.sha256(
        str(goal).encode("utf-8")
    ).hexdigest():
        return _fail("REQUEST_DIGEST_MISMATCH", refs, tool_results=context["results"])
    results = context["results"]
    from .requirement_goal_state import (
        validate_frozen_goal_contract, evaluate_frozen_goal_contract,
    )
    contract = context["board"].get("goal_state_contract")
    if not validate_frozen_goal_contract(contract, goal):
        return _fail("FROZEN_GOAL_CONTRACT_MISSING_OR_TAMPERED", refs,
                     tool_results=results)
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
            "verdict": "PASS", "failure_origin": "NONE",
            "reason_code": "ANSWER_ONLY_RESPONSE_ADMITTED",
            "goal_kind": "ANSWER_ONLY",
            "goal_evidence_reason": "TEXT_RESPONSE_ACCEPTED_NO_TOOL_NEEDED",
            "coverage_basis": "RUNTIME_TEXT_ADMISSION",
            "completion_kind": "ANSWER_ONLY",
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "evidence_refs": [], "mismatches": [], "tool_results": [],
        }
    # V2 never asks the Planner to reproduce the Runtime's own receipts.
    # V1 remains byte-for-byte compatible, including its 64-claim safeguard.
    claimed = {} if v2 else None
    if not v2:
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
    blocked, recovered = _execution_uncertainty(history)
    if blocked:
        return _fail("RUNTIME_EXECUTION_NOT_CONFIRMED", refs, tool_results=results,
                     finish_diagnostic={"unconfirmed_evidence": blocked,
                                        "authorization_granted": False})
    # An executed FAILED command can be valid evidence of an explicitly
    # requested negative test. Unexecuted REJECTED operations are NOT facts
    # about execution and never satisfy completion requirements.
    valid_goal_history = [
        item for item in history
        if item["receipt"]["executed"] is True
        and item["receipt"]["status"] in {"SUCCEEDED", "FAILED"}
    ]
    if not valid_goal_history:
        return _fail("NO_EXECUTED_GOAL_EVIDENCE", refs, tool_results=results)
    good_refs = {item["evidence_ref"] for item in valid_goal_history}
    model_declares_goal_fact = (
        bool(goal_checks) if v2 else any(
            field.startswith("data.") for ref in good_refs for field in claimed[ref]
        )
    )
    if any(item["receipt"]["status"] not in {"SUCCEEDED", "FAILED", "REJECTED", "PARTIAL"}
           for item in history):
        return _fail("RUNTIME_RESULT_STATUS_UNSUPPORTED", refs, tool_results=results)
    # The same source-grounded contract is fixed before execution. For a
    # compiled requirement the Planner may describe its preferred checks, but
    # those checks have no authority to rewrite desired conditions.
    if contract["status"] == "COMPILED":
        state = evaluate_frozen_goal_contract(
            contract, [item for item in history if item["receipt"]["status"] != "PARTIAL"], answer,
            admission_denials=(
                list(context.get("native_denials") or [])
                + list(context.get("admission_denials") or [])
            ),
        )
        advisory = None
        if goal_checks is not None:
            check_ok, check_reason, check_mismatch = _evaluate_model_goal_checks(
                history, refs, None if v2 else claimed, goal_checks)
            advisory = {
                "model_checks_match_actual_values": bool(check_ok),
                "reason_code": check_reason,
                "mismatches": check_mismatch[:12],
                "authoritative": False,
            }
        if not model_declares_goal_fact and not v2:
            return _fail("MODEL_FINISH_GOAL_FACT_NOT_DECLARED", refs,
                         tool_results=results, goal_state_table=state["state_table"],
                         goal_contract_sha256=contract["contract_sha256"],
                         model_goal_checks_advisory=advisory)
        if state["verdict"] != "PASS":
            return _fail(
                "FROZEN_GOAL_STATE_NOT_SATISFIED", refs,
                tool_results=results, goal_state_table=state["state_table"],
                goal_contract_sha256=contract["contract_sha256"],
                model_goal_checks_advisory=advisory,
                finish_diagnostic={
                    "repair_class": "GOAL_STATE_EVIDENCE_OR_REPORT_REPAIR",
                    "unsatisfied_condition_ids": state["unsatisfied_condition_ids"],
                    "state_table": [
                        row for row in state["state_table"] if row["verdict"] != "PASS"
                    ],
                    "frozen_contract": True, "authorization_granted": False,
                },
            )
        return {
            "verdict": "PASS",
            "failure_origin": "NONE",
            "reason_code": "FROZEN_GOAL_STATE_SATISFIED",
            "goal_kind": contract["goal_kind"],
            "goal_evidence_reason": state["reason_code"],
            "coverage_basis": "RUNTIME_FROZEN_GOAL_CONTRACT",
            "goal_contract_sha256": contract["contract_sha256"],
            "goal_state_table": state["state_table"],
            "model_goal_checks_advisory": advisory,
            "evidence_refs": refs, "mismatches": [], "tool_results": results,
            **({"reconciled_observations": recovered} if recovered else {}),
        }
    proof = _goal_proof(goal, valid_goal_history)
    if proof is None and _recognized_but_unsatisfied(goal, valid_goal_history):
        return _fail("KNOWN_GOAL_POSTCONDITION_NOT_SATISFIED", refs, tool_results=results)
    # For uncompiled requirements preserve legacy checks but never claim that
    # a universal natural-language state contract was verified.
    if not model_declares_goal_fact and not (v2 and proof is not None):
        return _fail("MODEL_FINISH_GOAL_FACT_NOT_DECLARED", refs, tool_results=results)
    if goal_checks is not None:
        # A rejected/unexecuted tool may be faithfully reported, but may
        # not satisfy goal checks; an executed negative-test failure can.
        binding = _goal_binding_diagnostic(history, good_refs, goal_checks)
        if binding["invalid_checks"] or not goal_checks:
            return _fail("GOAL_CHECK_LACKS_SUCCEEDED_EVIDENCE", refs,
                         tool_results=results, finish_diagnostic=binding)
        ok, reason, deviations = _evaluate_model_goal_checks(
            history, refs, None if v2 else claimed, goal_checks,
        )
        if not ok:
            diagnostic = None
            if reason == "MODEL_GOAL_CHECK_SCHEMA_INVALID":
                from .finish_schema_diagnostic import diagnose_goal_check_schema
                diagnostic = diagnose_goal_check_schema(history, claimed, goal_checks)
            return _fail(reason, refs, mismatches=deviations,
                         tool_results=results, finish_diagnostic=diagnostic)
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
        "verdict": "PASS", "failure_origin": "NONE",
        "reason_code": "MODEL_RUNTIME_AND_GOAL_MATCH",
        "goal_kind": kind, "goal_evidence_reason": reason,
        "coverage_basis": basis,
        "evidence_refs": refs, "mismatches": [],
        "tool_results": results,
        **({"reconciled_observations": recovered} if recovered else {}),
    }


__all__ = ["compare_runtime_finish", "finish_runtime_field_catalog"]
