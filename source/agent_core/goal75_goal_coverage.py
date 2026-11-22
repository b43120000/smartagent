"""Conservative Runtime ownership of original multi-stage Goal coverage.

The Planner may select strategies and propose typed Stage evidence predicates,
but it cannot shrink an explicit P0..Pn / Case A..D user requirement into
one successful shell command. Never changes Git74.1 Finish Comparator rules.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_PHASE = re.compile(r"(?<![\w])P(\d{1,2})(?![\w])", re.IGNORECASE)
_PHASE_RANGE = re.compile(
    r"(?<![\w])P(\d{1,2})\s*(?:-|–|—|~|～|至|到)\s*P(\d{1,2})(?![\w])",
    re.IGNORECASE,
)
_CASE = re.compile(r"\bCASE\s*[-_ ]?([A-D])\b", re.IGNORECASE)
_MAX_EXPANDED = 64
_NONPROOF_FIELDS = frozenset({
    "status", "executed", "data.exit_code",
    "data.process_started", "data.process_state",
})


def original_goal_stage_ids(request: str) -> tuple[str, ...]:
    """Extract explicit phase labels without interpreting unspecified prose."""
    goal = str(request or "")
    phases = {int(m.group(1)) for m in _PHASE.finditer(goal)}
    for m in _PHASE_RANGE.finditer(goal):
        start, stop = int(m.group(1)), int(m.group(2))
        if 0 <= start <= stop <= 63 and stop - start < _MAX_EXPANDED:
            phases.update(range(start, stop + 1))
    cases = {m.group(1).upper() for m in _CASE.finditer(goal)}
    names = [f"P{n}" for n in sorted(phases)]
    names.extend(f"Case{c}" for c in sorted(cases))
    return tuple(names[:_MAX_EXPANDED])


def requires_stage_control(
    request: str, *, goal_state_contract: Mapping | None = None,
    answer_only_eligible: bool = False,
) -> bool:
    """No new Stage gate for small or answer-only Git74.1 goals."""
    if answer_only_eligible:
        return False
    names = original_goal_stage_ids(request)
    if len(names) > 1:
        return True
    source = str(request or "")
    compiled = (goal_state_contract or {}).get("status") == "COMPILED"
    return bool(
        not compiled and len(source) >= 650
        and (source.count("\n") >= 3 or len(re.findall(r"(?m)^\s*\d+[.、)]\s+", source)) >= 3)
    )


def validate_stage_plan_coverage(request: str, stages: object) -> dict:
    """Necessary, not sufficient: no planner-selected subset may be accepted."""
    required = original_goal_stage_ids(request)
    if not isinstance(stages, list):
        return {"status": "REJECT", "reason_code": "STAGES_NOT_A_LIST",
                "required_stage_ids": list(required), "missing_stage_ids": list(required)}
    ids = [s.get("stage_id", "") for s in stages if isinstance(s, Mapping)]
    lower_ids = {str(i).casefold() for i in ids}
    missing = [s for s in required if s.casefold() not in lower_ids]
    if missing:
        return {"status": "REJECT", "reason_code": "ORIGINAL_GOAL_STAGE_COVERAGE_MISSING",
                "required_stage_ids": list(required), "missing_stage_ids": missing}
    if not required and len(stages) < 2:
        return {"status": "REJECT", "reason_code": "COMPOUND_GOAL_REQUIRES_MULTIPLE_STAGES",
                "required_stage_ids": [], "missing_stage_ids": []}
    weak = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        fields = [check.get("field", "") for check in stage.get("acceptance", [])
                  if isinstance(check, Mapping)]
        if not any(f.startswith("data.") and f not in _NONPROOF_FIELDS
                   for f in fields):
            weak.append(str(stage.get("stage_id", "")))
    if weak:
        return {"status": "REJECT", "reason_code": "STAGE_LACKS_SUBSTANTIVE_EVIDENCE",
                "required_stage_ids": list(required), "weak_stage_ids": weak,
                "missing_stage_ids": []}
    return {"status": "PASS", "reason_code": "EXPLICIT_USER_PHASE_COVERAGE_PRESENT",
            "required_stage_ids": list(required), "missing_stage_ids": [],
            "observed_stage_ids": ids}


def final_stage_coverage(request: str, stage_record: Mapping | None,
                         *, control_required: bool) -> dict:
    """Fail closed until every required original phase is frozen with evidence."""
    if not control_required:
        return {"status": "NOT_REQUIRED", "reason_code": "GIT74_1_LEGACY_GOAL"}
    if not isinstance(stage_record, Mapping):
        return {"status": "NOT_VERIFIED", "reason_code": "GOAL75_STAGE_PLAN_MISSING"}
    stages = stage_record.get("stages")
    coverage = validate_stage_plan_coverage(request, stages)
    if coverage["status"] != "PASS":
        return {"status": "NOT_VERIFIED", **{
            k: v for k, v in coverage.items() if k != "status"}}
    if not all(
        isinstance(s, Mapping)
        and s.get("status") == "VERIFIED_COMMITTED"
        and isinstance(s.get("commit"), Mapping)
        and s["commit"].get("commit_sha256")
        for s in stages
    ):
        return {"status": "NOT_VERIFIED",
                "reason_code": "GOAL75_STAGE_COMMIT_INCOMPLETE",
                "required_stage_ids": coverage["required_stage_ids"]}
    return {"status": "PASS", "reason_code": "FROZEN_STAGES_AND_EXPLICIT_GOAL_COVERED",
            "required_stage_ids": coverage["required_stage_ids"],
            "stage_count": len(stages)}
