"""Safe field-level diagnostics for Runtime-owned Finish goal-check contracts.

This module only explains why a model-authored predicate is structurally
invalid. It does not repair claims, manufacture evidence, or grant completion.
Never echo model-supplied values, commands, or raw Runtime payloads.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_SCHEMA = "SMARTAGENT_FINISH_SCHEMA_DIAGNOSTIC_V1"
_KEYS = {"criterion", "evidence_ref", "field", "operator", "expected"}
_OPS = ("eq", "ne", "ge", "le")
_FIELD = re.compile(r"^[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|[0-9]+))*$")
_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SECRET = re.compile(r"secret|password|token|api.?key|credential|authorization", re.I)


def _type_name(value) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unsupported"


def _safe_field_names(fields) -> list[str]:
    if not isinstance(fields, Mapping):
        return []
    return [
        name for name in fields
        if isinstance(name, str) and _SAFE_FIELD.fullmatch(name)
        and not _SECRET.search(name)
    ][:24]


def _issue(index: int, key: str, code: str, *,
           expected_type: str = "", actual_type: str = "",
           allowed_values=()) -> dict:
    return {
        "schema": _SCHEMA, "repair_class": "FINISH_SCHEMA_REPAIR",
        "check_index": index, "field": key, "constraint_code": code,
        "expected_type": expected_type, "actual_type": actual_type,
        "allowed_values": list(allowed_values)[:24],
        "evidence_unmodified": True, "authorization_granted": False,
    }


def diagnose_goal_check_schema(history: list[dict], claimed: Mapping,
                               checks: object) -> dict | None:
    """Return first precise constraint failure, or None when structurally valid.

    Only the original model's claim set and verified Runtime history are used.
    The comparator remains the sole authority for evidence value comparisons.
    """
    if not isinstance(checks, list) or not (1 <= len(checks) <= 64):
        return _issue(-1, "goal_checks", "CHECK_ARRAY_LENGTH",
                      expected_type="array[1..64]", actual_type=_type_name(checks))
    valid_refs = {
        row.get("evidence_ref") for row in history
        if isinstance(row, Mapping) and isinstance(row.get("evidence_ref"), str)
    }
    seen = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            return _issue(index, "goal_checks", "CHECK_NOT_OBJECT",
                          expected_type="object", actual_type=_type_name(check))
        keys = set(check)
        if keys != _KEYS:
            return _issue(index, "goal_checks", "CHECK_KEYS_MISMATCH",
                          expected_type="exact_five_fields", actual_type="object",
                          allowed_values=sorted(_KEYS))
        criterion = check["criterion"]
        if not isinstance(criterion, str) or not (1 <= len(criterion) <= 240):
            return _issue(index, "criterion", "CRITERION_TYPE_OR_LENGTH",
                          expected_type="string[1..240]", actual_type=_type_name(criterion))
        if criterion in seen:
            return _issue(index, "criterion", "DUPLICATED_CRITERION",
                          expected_type="unique_string", actual_type="string")
        seen.add(criterion)
        ref = check["evidence_ref"]
        if not isinstance(ref, str) or ref not in valid_refs or ref not in claimed:
            return _issue(index, "evidence_ref", "EVIDENCE_REF_NOT_IN_RUNTIME_CLAIMS",
                          expected_type="existing_evidence_ref",
                          actual_type=_type_name(ref),
                          allowed_values=sorted(x for x in valid_refs
                                                if x in claimed)[:24])
        field = check["field"]
        if not isinstance(field, str) or not field.startswith("data.") or not _FIELD.fullmatch(field):
            return _issue(index, "field", "FIELD_SYNTAX_NOT_DATA_SCALAR",
                          expected_type="data.<valid_scalar_field>",
                          actual_type=_type_name(field))
        operator = check["operator"]
        if not isinstance(operator, str) or operator not in _OPS:
            return _issue(index, "operator", "INVALID_OPERATOR",
                          expected_type="enum", actual_type=_type_name(operator),
                          allowed_values=_OPS)
        expected = check["expected"]
        if type(expected) not in (str, int, float, bool, type(None)):
            return _issue(index, "expected", "EXPECTED_NOT_SCALAR",
                          expected_type="string|number|boolean|null",
                          actual_type=_type_name(expected))
        if field not in claimed[ref]:
            return _issue(index, "field", "FIELD_NOT_DECLARED_IN_EXPECTED_RUNTIME",
                          expected_type="claimed_data_field",
                          actual_type="string",
                          allowed_values=[f for f in _safe_field_names(claimed[ref])
                                          if f.startswith("data.")])
    return None


__all__ = ["diagnose_goal_check_schema"]
