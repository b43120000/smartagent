"""Git70: tool-neutral, evidence-grounded Expected/Observed matching.

Expected is frozen before dispatch. Observed is a typed Runtime receipt read back
from the immutable evidence store; Planner text is NEVER observational authority.
Action matches never imply task completion or permission to replay a command.
Only terminal task acceptance may return PASS/FAIL; intermediate states are
MATCH, MISMATCH, RECOLLECT_EVIDENCE, and PAUSE_AND_RECONCILE.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "SMARTAGENT_GIT70_EXPECTED_OBSERVED_V1"
MAX_ASSERTIONS = 48
_FIELD = re.compile(r"^(?:status|reason|executed|data(?:\.[A-Za-z_][A-Za-z_0-9]*|\.[0-9]+)+)$")
_REF = re.compile(r"^MIN-EV-[a-f0-9]{24}$")
_OPERATORS = {"eq", "ne", "ge", "gt", "le", "lt", "in", "contains", "is_null"}
_SAFE_SCALAR = (str, bool, int, float, type(None))


class ExpectedContractError(ValueError):
    """No operation may acquire a retroactively changed Expected contract."""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _scalar(value: object) -> bool:
    return type(value) in _SAFE_SCALAR and (
        not isinstance(value, str) or len(value) <= 1024
    )


def validate_expected(spec: object) -> dict:
    """Validate/canonicalize a bounded, pure-data, pre-dispatch contract.

    Unknown fields/operators are rejected; no eval, code or arbitrary paths.
    Missing observed facts are *not* matching facts, even with expected null.
    """
    if not isinstance(spec, dict) or set(spec) != {"contract_version", "assertions", "aggregation"}:
        raise ExpectedContractError("EXPECTED_SCHEMA_INVALID")
    if spec["contract_version"] != 1 or spec["aggregation"] not in {"ALL", "ANY"}:
        raise ExpectedContractError("EXPECTED_VERSION_OR_AGGREGATION_INVALID")
    rows = spec["assertions"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ASSERTIONS:
        raise ExpectedContractError("EXPECTED_ASSERTION_COUNT_INVALID")
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"field", "operator", "value"}:
            raise ExpectedContractError(f"EXPECTED_ASSERTION_SCHEMA_INVALID:{index}")
        field, op, value = row["field"], row["operator"], row["value"]
        if not isinstance(field, str) or len(field) > 120 or not _FIELD.fullmatch(field):
            raise ExpectedContractError(f"EXPECTED_FIELD_INVALID:{index}")
        if field in {"data.stdout", "data.stderr", "data.command_error"}:
            raise ExpectedContractError(f"EXPECTED_RAW_TEXT_FIELD_FORBIDDEN:{index}")
        if op not in _OPERATORS:
            raise ExpectedContractError(f"EXPECTED_OPERATOR_INVALID:{index}")
        if op == "is_null":
            if value is not None:
                raise ExpectedContractError(f"EXPECTED_IS_NULL_VALUE_INVALID:{index}")
        elif op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= 48 or not all(_scalar(v) for v in value):
                raise ExpectedContractError(f"EXPECTED_IN_VALUES_INVALID:{index}")
        elif op == "contains":
            if not _scalar(value) or value is None:
                raise ExpectedContractError(f"EXPECTED_CONTAINS_VALUE_INVALID:{index}")
        elif op in {"ge", "gt", "le", "lt"}:
            if type(value) not in {int, float}:
                raise ExpectedContractError(f"EXPECTED_ORDER_VALUE_INVALID:{index}")
        elif not _scalar(value):
            raise ExpectedContractError(f"EXPECTED_VALUE_INVALID:{index}")
        normalized.append({"field": field, "operator": op, "value": value})
    return {"contract_version": 1, "aggregation": spec["aggregation"], "assertions": normalized}


def frozen_expected_sha256(spec: object) -> str:
    return _digest(validate_expected(spec))


def _lookup(value: object, field: str) -> tuple[bool, object]:
    node = value
    for segment in field.split("."):
        if isinstance(node, Mapping) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdecimal() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return False, None
    return True, node


def _compare(op: str, expected: object, actual: object) -> bool | None:
    if op == "is_null":
        return actual is None
    if op == "eq":
        return type(expected) is type(actual) and expected == actual
    if op == "ne":
        return type(expected) is type(actual) and expected != actual
    if op == "in":
        return any(type(actual) is type(v) and actual == v for v in expected)
    if op == "contains":
        if type(actual) is str and type(expected) is str:
            return expected in actual[:8192]
        if isinstance(actual, (list, tuple)):
            return expected in actual[:128]
        return None
    if type(actual) not in {int, float} or type(expected) not in {int, float}:
        return None
    return {"gt": lambda: actual > expected, "ge": lambda: actual >= expected,
            "lt": lambda: actual < expected, "le": lambda: actual <= expected}[op]()


def _shown(value: object) -> object:
    # Non-secret scalar fields are useful locally; never leak stdout/stderr text.
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str) and len(value) <= 90 and re.fullmatch(r"[A-Za-z0-9_./:-]+", value):
        return value
    return {"sha256": _digest(value), "type": type(value).__name__}


def safety_decision(receipt: object) -> tuple[str, str]:
    """Safety is orthogonal to assertions; matching cannot override replay rules."""
    if not isinstance(receipt, Mapping):
        return "PAUSE_AND_RECONCILE", "UNTRUSTED_RUNTIME_RECEIPT"
    data = receipt.get("data")
    data = data if isinstance(data, Mapping) else {}
    state = data.get("process_state")
    if data.get("possible_partial_execution") is True:
        return "PAUSE_AND_RECONCILE", "POSSIBLE_PARTIAL_EXECUTION"
    if state in {"RUNNING", "UNKNOWN"}:
        return "PAUSE_AND_RECONCILE", "PROCESS_LIVENESS_UNCONFIRMED"
    if receipt.get("status") in {"UNKNOWN", "RUNNING", "PARTIAL", "QUEUED"}:
        return "PAUSE_AND_RECONCILE", "EXECUTION_FINALITY_UNCONFIRMED"
    return "CONTINUE_OR_REPLAN", "NO_UNCONFIRMED_EXECUTION_DETECTED"


def match_expected(
    expected: object, receipt: object, *,
    evidence_ref: str, evidence_store_status: str,
    frozen_sha256: str | None = None, action_id: str = "",
) -> dict:
    """Return independently auditable assertion and safety results.

    If evidence cannot be read back/typed, request *observation recovery*,
    never replay the original Action. No task-level FAIL is returned here.
    """
    contract = validate_expected(expected)
    digest = _digest(contract)
    if frozen_sha256 is not None and frozen_sha256 != digest:
        raise ExpectedContractError("EXPECTED_FROZEN_DIGEST_MISMATCH")
    rows = []
    credible = (bool(_REF.fullmatch(str(evidence_ref))) and
                evidence_store_status == "PERSISTED_AND_READBACK_VERIFIED" and
                isinstance(receipt, Mapping) and
                isinstance(receipt.get("data"), Mapping) and
                receipt.get("status") in {"SUCCEEDED","FAILED","REJECTED","UNKNOWN","RUNNING","PARTIAL","QUEUED"})
    for index, assertion in enumerate(contract["assertions"], 1):
        present, observed = _lookup(receipt, assertion["field"]) if credible else (False, None)
        evaluated = _compare(assertion["operator"], assertion["value"], observed) if present else None
        state = "MATCH" if evaluated is True else "MISMATCH" if evaluated is False else "UNOBSERVED"
        rows.append({
            "index": index, "field": assertion["field"], "operator": assertion["operator"],
            "expected": _shown(assertion["value"]),
            "observed": _shown(observed) if present else "[UNOBSERVED]",
            "observed_present": present, "match_state": state,
        })
    missing = [r["field"] for r in rows if r["match_state"] == "UNOBSERVED"]
    outcomes = [r["match_state"] for r in rows]
    # ALL: a trustworthy contradictory fact is a mismatch even if another is missing.
    # ANY: only mismatch when every comparison is definitively false.
    if contract["aggregation"] == "ALL":
        match_state = ("MISMATCH" if "MISMATCH" in outcomes else
                       "RECOLLECT_EVIDENCE" if missing else "MATCH")
    else:
        match_state = ("MATCH" if "MATCH" in outcomes else
                       "RECOLLECT_EVIDENCE" if missing else "MISMATCH")
    if not credible:
        match_state = "RECOLLECT_EVIDENCE"
    safety, safety_reason = safety_decision(receipt)
    action_next = ("RECOLLECT_EVIDENCE" if match_state == "RECOLLECT_EVIDENCE" else
                   "REPLAN" if match_state == "MISMATCH" else "PROCEED")
    if safety == "PAUSE_AND_RECONCILE":
        action_next = "RECONCILE_WITHOUT_REPLAY"
    return {
        "schema": SCHEMA,
        "action_id": action_id, "evidence_ref": evidence_ref,
        "frozen_expected_sha256": digest,
        "evidence_store_status": evidence_store_status,
        "observed_integrity": "VERIFIED" if credible else "UNVERIFIED",
        "match_state": match_state,
        "action_assertion_verdict": "PASS" if match_state == "MATCH" else None,
        "final_verdict": None,  # Reserved for terminal Goal Acceptance only
        "assertions": rows, "unobserved_fields": missing,
        "safety_decision": safety, "safety_reason": safety_reason,
        "next_action": action_next,
        "recovery": {
            "strategy": "READBACK_MIN_EV_THEN_REOBSERVE",
            "replay_original_action": False,
            "evidence_ref": evidence_ref,
            "fields_to_reobserve": missing,
            "recovery_needed": match_state == "RECOLLECT_EVIDENCE",
        },
    }


def terminal_goal_verdict(*, goal_satisfied: bool, all_required_evidence_verified: bool,
                          infeasibility_proven: bool = False,
                          infeasibility_evidence_refs: tuple[str, ...] = ()) -> str | None:
    """Only terminal Goal Acceptance has PASS/FAIL. Otherwise no verdict."""
    if goal_satisfied and all_required_evidence_verified:
        return "PASS"
    if (not goal_satisfied and infeasibility_proven and
            bool(infeasibility_evidence_refs) and
            all(bool(_REF.fullmatch(ref)) for ref in infeasibility_evidence_refs)):
        return "FAIL"
    return None


def append_match_report(root: str | Path, task_id: str, request_id: str, report: dict) -> Path:
    """Local plain-JSONL diagnostic, scoped to the task; never command output."""
    if not re.fullmatch(r"TASK-[A-Za-z0-9-]{8,100}", task_id):
        raise ValueError("TASK_ID_INVALID")
    if not re.fullmatch(r"RR-[A-Za-z0-9-]{8,100}", request_id):
        raise ValueError("REQUEST_ID_INVALID")
    folder = Path(root) / "localdata" / "runtime" / "goal_evidence_matching"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / (task_id + ".jsonl")
    row = {"task_id": task_id, "request_id": request_id, **report}
    with out.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
    return out
