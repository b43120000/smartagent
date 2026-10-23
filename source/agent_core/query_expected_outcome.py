"""Predeclared, exact negative assertions for snapshot-bound project reads.

This is a bounded VPCS MVP, not a general-purpose error-to-success mapper.
The query's original business response is never rewritten.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

SUPPORTED_ERRORS = {"context_ref_snapshot_mismatch"}
STALE_PREFIX = "project_snapshot_stale:"


class QueryExpectationError(ValueError):
    pass


def validate_query_expected_outcome(action: Mapping[str, object]) -> dict | None:
    """Validate before any tool invocation; only one pinned read is supported."""
    if "expected_outcome" not in action:
        return None
    if action.get("tool") != "query_project":
        raise QueryExpectationError("query_expected_outcome_tool_unsupported")
    expected = action["expected_outcome"]
    if not isinstance(expected, dict) or set(expected) != {"query_index", "status", "error"}:
        raise QueryExpectationError("query_expected_outcome_shape_invalid")
    if type(expected["query_index"]) is not int or expected["query_index"] != 1:
        raise QueryExpectationError("query_expected_outcome_index_invalid")
    if expected["status"] != "ERROR" or type(expected["error"]) is not str:
        raise QueryExpectationError("query_expected_outcome_status_invalid")
    error = expected["error"]
    if error not in SUPPORTED_ERRORS:
        if not error.startswith(STALE_PREFIX):
            raise QueryExpectationError("query_expected_outcome_error_unsupported")
        relative = error[len(STALE_PREFIX):]
        pure = PurePosixPath(relative)
        if (not relative or len(relative) > 512 or pure.is_absolute()
                or ".." in pure.parts or "\\" in relative
                or pure.as_posix() != relative or any(c in relative for c in "\r\n\x00")):
            raise QueryExpectationError("query_expected_outcome_path_invalid")
    queries = action.get("queries")
    if (not isinstance(queries, list) or len(queries) != 1
            or not isinstance(queries[0], dict)
            or queries[0].get("operation") != "read_context_ref"
            or not isinstance(queries[0].get("context_ref"), str)
            or not queries[0]["context_ref"].startswith("ctx:v1:")):
        raise QueryExpectationError("query_expected_outcome_requires_single_pinned_read")
    if not isinstance(action.get("condition_id"), str) or not action["condition_id"].strip():
        raise QueryExpectationError("query_expected_outcome_condition_required")
    return dict(expected)


def evaluate_query_expected_outcome(expected: Mapping[str, object], payload: Mapping[str, object]) -> dict:
    """Evaluate trusted query output against a previously validated assertion.

    No matches are guessed from prose, incomplete batches, status only or
    provider-generated markers. Observed ERROR is preserved for audit.
    """
    rows = payload.get("results")
    valid = (payload.get("schema") == "PROJECT_ACCESS_QUERY_RESULT_V1"
             and payload.get("status") == "READY"
             and payload.get("result_count") == 1
             and payload.get("response_truncated") is False
             and isinstance(rows, list) and len(rows) == 1
             and isinstance(rows[0], dict)
             and rows[0].get("query_index") == 1
             and rows[0].get("operation") == "read_context_ref"
             and rows[0].get("status") in {"OK", "ERROR", "NOT_FOUND"})
    if not valid:
        return {
            "expectation_status": "UNVERIFIED",
            "verification_status": "UNVERIFIED",
            "expected_outcome": dict(expected),
            "observation_status": "INCOMPLETE",
            "terminal_eligible": False,
        }
    actual = rows[0]
    matched = actual["status"] == expected["status"] and actual.get("error") == expected["error"]
    return {
        "expectation_status": "PASS" if matched else "FAIL",
        "verification_status": "PASS" if matched else "FAIL",
        "expected_outcome": dict(expected),
        "observation_status": actual["status"],
        "observation_error": str(actual.get("error", "") or ""),
        "terminal_eligible": matched,
    }
