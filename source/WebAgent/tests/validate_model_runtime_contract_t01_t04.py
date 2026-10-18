#!/usr/bin/env python3
"""Model-call contract coverage for the first four read-only Runtime tools."""
from __future__ import annotations

from agent_core.smartagent_protocol import (
    normalize_tool_numeric_fields,
    validate_tool_envelope,
)


def accepted(action: dict) -> dict:
    normalized = normalize_tool_numeric_fields(action)
    valid, diagnostic = validate_tool_envelope(normalized)
    assert valid, diagnostic
    return normalized


def rejected(action: dict, reason: str, *, fields: set[str] | None = None) -> dict:
    normalized = normalize_tool_numeric_fields(action)
    valid, diagnostic = validate_tool_envelope(normalized)
    assert not valid and diagnostic, action
    assert diagnostic["reason"] == reason, diagnostic
    if fields is not None:
        actual = {row["field"] for row in diagnostic.get("field_issues", [])}
        assert fields <= actual, diagnostic
    return diagnostic


def run() -> None:
    # C01: canonical calls map to the intended tool and retain exact paths.
    canonical = [
        {"tool": "read_file", "action_id": "T01-C01", "path": "E:/card/runtime_it/a.txt"},
        {"tool": "list_directory", "action_id": "T02-C01", "path": "E:/card/runtime_it"},
        {
            "tool": "inspect_directory", "action_id": "T03-C01",
            "paths": ["E:/card/runtime_it"], "recursive": False, "sample_limit": 1,
        },
        {
            "tool": "find_file", "action_id": "T04-C01", "name": "a.txt",
            "root": "E:/card/runtime_it", "max_results": 5,
        },
    ]
    for action in canonical:
        assert accepted(action) == action

    # C02: only schema-declared numeric strings are normalized.  Paths, names
    # and booleans are never guessed or coerced.
    inspect = accepted({
        "tool": "inspect_directory", "action_id": "T03-C02",
        "paths": ["E:/card/runtime_it"], "recursive": False, "sample_limit": "3",
    })
    find = accepted({
        "tool": "find_file", "action_id": "T04-C02", "name": "3",
        "root": "E:/card/runtime_it", "max_results": "7",
    })
    assert inspect["sample_limit"] == 3 and inspect["recursive"] is False
    assert find["max_results"] == 7 and find["name"] == "3"

    # C03: all determinable field issues are reported together, even though
    # the leading reason identifies the first missing/wrong field.
    rejected({"tool": "read_file", "action_id": "T01-C03"},
             "missing_required_field", fields={"path"})
    rejected({"tool": "inspect_directory", "action_id": "T03-C03"},
             "missing_required_field", fields={"paths"})
    rejected({"tool": "find_file", "action_id": "T04-C03"},
             "missing_required_field", fields={"name"})

    # C04: unsupported legacy fields and ambiguous path/path(s) shapes are
    # rejected instead of silently widening scope or guessing semantics.
    rejected({"tool": "list_directory", "action_id": "T02-C04", "path": "E:/card", "depth": 1},
             "unexpected_field")
    rejected({
        "tool": "inspect_directory", "action_id": "T03-C04",
        "path": "E:/card", "paths": ["E:/card"],
    }, "unexpected_field")
    rejected({
        "tool": "find_file", "action_id": "T04-C04", "name": 3,
        "root": "E:/card", "max_results": "not-a-number",
    }, "wrong_field_type", fields={"name", "max_results"})

    # Request-context references are expanded only by request-bound admission;
    # a leaked unresolved reference must never reach a generic dispatcher.
    rejected({
        "tool": "read_file", "action_id": "T01-C04-REF",
        "field_refs": {"path": {"ref_id": "CTX-X", "version": 1}},
    }, "UNEXPANDED_REQUEST_CONTEXT_REFERENCE")

    print("MODEL_RUNTIME_CONTRACT_T01_T04_PASS cases=16")


if __name__ == "__main__":
    run()
