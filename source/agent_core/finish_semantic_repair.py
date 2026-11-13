"""Git56: explain Planner/Runtime semantic mismatches without rewriting evidence.

This module never changes source request, Goal Contract, ToolResults or
acceptance decisions. It classifies bounded, typed model-authored claim errors
and emits an instruction for a fresh non-executing Planner Finish. Missing
observations or unmet known postconditions remain real Goal failures.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_FINISH_SEMANTIC_REPAIR_V1"
MAX_MISMATCHES = 16
SAFE_FACT = re.compile(
    r"^(?:status|data\.(?:"
    r"verification_status|execution_status|process_state|"
    r"exit_code|timed_out|observation_status|observed_exists|"
    r"observed_is_directory|directory_created|"
    r"observation_value_confirmed|observed_boolean|output_complete|"
    r"stdout_truncated|stderr_truncated|stdout_bytes|stderr_bytes|"
    r"sandbox_evidence\.(?:sandbox_identity_verified|"
    r"turbo_effective_for_action|sandbox_backend|native_error_class|"
    r"native_executable_identity_verified|"
    r"native_executable_identity_reason|"
    r"kernel_outside_read_denied|kernel_outside_write_denied)"
    r"))$"
)
_SAFE_ENUM = re.compile(r"^[A-Za-z_][A-Za-z0-9_:-]{0,79}$")


def _safe_actual(field: str, value: object) -> tuple[bool, object]:
    """Never echo raw stdout/stderr, paths, tokens, commands or secrets."""
    if not isinstance(field, str) or not SAFE_FACT.fullmatch(field):
        return False, None
    if value is None or type(value) in (bool, int, float):
        return True, value
    if isinstance(value, str) and _SAFE_ENUM.fullmatch(value):
        return True, value
    return False, None


def _explicit_desired_state(goal: str) -> bool:
    """Conservative veto: do not turn desired success into a fact query."""
    text = str(goal or "")
    return bool(re.search(
        r"確保|保證|必須|一定要|應該要|要求成功|修復|修正|"
        r"部署|建立|創建|新增|刪除|移除|寫入|覆蓋|"
        r"must\b|ensure\b|require(?:d)?\b|"
        r"create\b|delete\b|fix\b|install\b|deploy\b",
        text, re.I,
    ))


def diagnose_finish_semantic_mismatch(comparison: Mapping,
                                      goal: str) -> dict | None:
    """Identify fixable Planner assertions, not prove arbitrary Goal semantics.

    For an unknown Goal, proposed predicates may be repaired, but independent
    Goal coverage remains the responsibility of the original comparator.
    """
    if not isinstance(comparison, Mapping):
        return None
    reason = comparison.get("reason_code")
    if reason not in {"MODEL_RUNTIME_MISMATCH", "GOAL_CHECK_RUNTIME_MISMATCH"}:
        return None
    rows = comparison.get("mismatches")
    if not isinstance(rows, list) or not (1 <= len(rows) <= MAX_MISMATCHES):
        return None
    if reason == "GOAL_CHECK_RUNTIME_MISMATCH" and _explicit_desired_state(goal):
        return {
            "schema": SCHEMA, "repair_class": "GOAL_STATE_NOT_RELABELABLE",
            "failure_origin": "UNRESOLVED_GOAL_STATE",
            "original_reason_code": reason,
            "repair_only": False, "evidence_unmodified": True,
            "authorization_granted": False,
        }
    corrections = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        field, actual = row.get("field"), row.get("actual")
        ref = row.get("evidence_ref")
        safe, value = _safe_actual(field, actual)
        if not safe or not isinstance(ref, str) or not ref.startswith("MIN-EV-"):
            return None
        # Never re-label an absent, redacted or unknown observation as proof.
        if actual == "[MISSING]" or row.get("expected") == actual:
            return None
        corrections.append({
            "evidence_ref": ref,
            "field": field,
            "runtime_actual_value": value,
            "criterion": (str(row.get("criterion") or "")[:100]
                          if reason == "GOAL_CHECK_RUNTIME_MISMATCH" else ""),
        })
    return {
        "schema": SCHEMA,
        "repair_class": (
            "MODEL_FACT_DECLARATION_REPAIR" if reason == "MODEL_RUNTIME_MISMATCH"
            else "MODEL_GOAL_PREDICATE_SEMANTIC_REVIEW"
        ),
        "failure_origin": "PLANNER_ASSERTION_MISMATCH",
        "original_reason_code": reason,
        "corrections": corrections,
        "mismatch_count": len(corrections),
        "repair_only": True,
        "new_tool_result_required": False,
        "evidence_unmodified": True,
        "goal_contract_unchanged": True,
        "goal_acceptance_not_granted": True,
        "authorization_granted": False,
        "instruction": (
            "Re-read the original user goal. For an observation/report goal, "
            "describe the actual Runtime value without imposing a desired "
            "successful environment state. For a desired state, do not "
            "weaken its postcondition or claim it is satisfied. Re-submit "
            "Finish only using existing evidence; never invent values."
        ),
    }


__all__ = ["SCHEMA", "diagnose_finish_semantic_mismatch"]
