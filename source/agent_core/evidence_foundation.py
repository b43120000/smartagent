"""Runtime Evidence Foundation v1: facts, safe predicates, and read-only adapters.

Fact normalization is deterministic. Neither a model nor arbitrary stdout may
manufacture a confirmed fact. Predicates never execute code.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

SCHEMA = "SMARTAGENT_EVIDENCE_V1"
PREDICATE_OPERATORS = frozenset({"equals", "not_equals", "contains", "greater_than", "less_than", "in_range"})
SUBJECT_FIELDS = frozenset({"file.exists", "process.exit_code", "project.query_status", "project.snapshot_id"})
NO_VALUE = object()


class EvidenceSpecError(ValueError):
    pass


def validate_predicate(raw: object, *, field: str | None = None) -> dict:
    """Validate a declarative claim before running a tool."""
    if not isinstance(raw, dict) or set(raw) != {"field", "operator", "expected"}:
        raise EvidenceSpecError("evidence_predicate_shape_invalid")
    actual_field, op, expected = raw["field"], raw["operator"], raw["expected"]
    if actual_field not in SUBJECT_FIELDS or (field is not None and actual_field != field):
        raise EvidenceSpecError("evidence_field_unsupported")
    if op not in PREDICATE_OPERATORS:
        raise EvidenceSpecError("evidence_operator_unsupported")
    if type(expected) not in {str, int, float, bool}:
        raise EvidenceSpecError("evidence_expected_value_invalid")
    if op == "contains" and type(expected) is not str:
        raise EvidenceSpecError("evidence_contains_string_required")
    if op in {"greater_than", "less_than"} and type(expected) not in {int, float}:
        raise EvidenceSpecError("evidence_numeric_operator_required")
    if op == "in_range":
        raise EvidenceSpecError("evidence_range_requires_explicit_bounded_schema")
    if actual_field == "file.exists" and (op not in {"equals", "not_equals"} or type(expected) is not bool):
        raise EvidenceSpecError("evidence_file_exists_boolean_required")
    if actual_field == "process.exit_code" and (type(expected) is not int or isinstance(expected, bool)):
        raise EvidenceSpecError("evidence_exit_code_integer_required")
    return dict(raw)


def evaluate_predicate(predicate: Mapping[str, object], observation: Mapping[str, object]) -> dict:
    """Tri-valued evaluation. Unknown, stale, mismatched subject => UNKNOWN."""
    spec = validate_predicate(dict(predicate))
    if (observation.get("field") != spec["field"]
            or observation.get("observation_status") != "CONFIRMED"
            or observation.get("freshness") != "CURRENT"
            or "value" not in observation):
        return {"verification_status": "UNKNOWN", "comparison": "NOT_EVALUATED"}
    actual = observation["value"]
    expected = spec["expected"]
    op = spec["operator"]
    if op in {"equals", "not_equals"} and type(actual) is not type(expected):
        return {"verification_status": "UNKNOWN", "comparison": "TYPE_MISMATCH"}
    if op == "contains" and type(actual) is not str:
        return {"verification_status": "UNKNOWN", "comparison": "TYPE_MISMATCH"}
    if op in {"greater_than", "less_than"} and type(actual) not in {int, float}:
        return {"verification_status": "UNKNOWN", "comparison": "TYPE_MISMATCH"}
    if op == "equals":
        passed = actual == expected
    elif op == "not_equals":
        passed = actual != expected
    elif op == "contains":
        passed = expected in actual
    elif op == "greater_than":
        passed = actual > expected
    else:
        passed = actual < expected
    return {"verification_status": "PASS" if passed else "FAIL", "comparison": "MATCH" if passed else "MISMATCH"}


def observation(field: str, value: object = NO_VALUE, *, source: str,
                scope: str, version: str = "", observed_at: str = "",
                status: str = "CONFIRMED", raw_ref: str = "") -> dict:
    """Keep unknown values absent, never coerce UNKNOWN to False."""
    result = {"schema": SCHEMA, "field": field, "observation_status": status,
              "source": source, "scope": scope, "version": str(version),
              "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
              "freshness": "CURRENT" if status == "CONFIRMED" else "UNKNOWN",
              "raw_evidence_ref": raw_ref}
    if status == "CONFIRMED" and value is not NO_VALUE:
        result["value"] = value
    return result


def probe_file_exists(path: Path, *, raw_ref: str = "") -> dict:
    """Stat an already authorized path; ENOENT != EACCES or timeouts.

    The parent must itself be a directory that can be examined. An
    inaccessible parent does not establish absence.
    """
    try:
        parent_info = path.parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode):
            return observation("file.exists", source="filesystem.stat", scope=str(path),
                               status="UNKNOWN", raw_ref=raw_ref)
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        # A missing final path is confirmed only if its parent was stat-able.
        if not path.parent.is_dir():
            return observation("file.exists", source="filesystem.stat", scope=str(path),
                               status="UNKNOWN", raw_ref=raw_ref)
        return observation("file.exists", False, source="filesystem.stat", scope=str(path), raw_ref=raw_ref)
    except (PermissionError, OSError):
        return observation("file.exists", source="filesystem.stat", scope=str(path),
                           status="UNKNOWN", raw_ref=raw_ref)
    return observation("file.exists", True, source="filesystem.stat", scope=str(path), raw_ref=raw_ref)


def validate_observe_fact_action(action: Mapping[str, object]) -> dict | None:
    if action.get("tool") != "observe_fact":
        raise EvidenceSpecError("observe_fact_tool_required")
    if type(action.get("path")) is not str or not str(action["path"]).strip():
        raise EvidenceSpecError("observe_fact_path_required")
    expected = action.get("predicate")
    if expected is None:
        if "condition_id" in action:
            raise EvidenceSpecError("observe_fact_condition_without_predicate")
        return None
    spec = validate_predicate(expected, field="file.exists")
    if not isinstance(action.get("condition_id"), str) or not action["condition_id"].strip():
        raise EvidenceSpecError("observe_fact_condition_required")
    return spec


def adapt_project_query(payload: object, *, raw_ref: str = "") -> list[dict]:
    """Structured query metadata, never inferred from text or malformed rows."""
    if not isinstance(payload, dict) or payload.get("schema") != "PROJECT_ACCESS_QUERY_RESULT_V1":
        return []
    if payload.get("status") != "READY" or payload.get("response_truncated") is True:
        return []
    rows = payload.get("results")
    if not isinstance(rows, list) or not rows:
        return []
    snapshot = str(payload.get("snapshot_id") or "")
    if not snapshot:
        return []
    facts = [observation("project.snapshot_id", snapshot, source="query_project",
                         scope=str(payload.get("project_handle", "")), version=snapshot, raw_ref=raw_ref)]
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in {"OK", "ERROR", "NOT_FOUND"}:
            return []
        facts.append(observation("project.query_status", row["status"], source="query_project",
                                 scope=str(row.get("operation", "")), version=snapshot, raw_ref=raw_ref))
    return facts


def adapt_command_outcome(outcome: object, *, raw_ref: str = "") -> list[dict]:
    """Only trust a Runtime-owned EXITED/exit_code receipt, never stdout."""
    if not isinstance(outcome, dict) or outcome.get("tool") != "run_command":
        return []
    data = outcome.get("data")
    if not isinstance(data, dict) or data.get("process_state") != "EXITED":
        return []
    code = data.get("exit_code")
    if type(code) is not int:
        return []
    return [observation("process.exit_code", code, source="run_command",
                        scope=str(data.get("operation", "")), raw_ref=raw_ref)]
