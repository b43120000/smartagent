"""Git73 regression: rejected P7 Timeout contracts must be precisely repairable.

No command execution, and no loosening of the Git71 bounded reconciliation scope.
"""
from __future__ import annotations

import copy

import pytest

from agent_core.smartagent_protocol import validate_tool_envelope


CWD = r"E:\AIspace\git73_p7_fixture"
GOAL = CWD + r"\goal_dir"


def _p7():
    return {
        "tool": "run_command",
        "action_id": "MIN-G73-ADMISSION-TEST",
        "operation": "TEST",
        "shell_mode": "POWERSHELL",
        "command": "Write-Output 'Git73-Admission-Only'",
        "working_directory": CWD,
        "timeout": 1,
        "expected": {
            "contract_version": 1,
            "aggregation": "ALL",
            "assertions": [
                {"field": "data.timed_out", "operator": "eq", "value": True},
                {"field": "data.process_started", "operator": "eq", "value": True},
                {"field": "data.exit_code", "operator": "is_null", "value": None},
            ],
        },
        "reconciliation": {
            "version": 1,
            "checks": [{"tool": "observe_fact", "path": GOAL, "expected_exists": True}],
        },
    }


def test_valid_p7_reconciliation_admits_without_executing():
    valid, diag = validate_tool_envelope(_p7(), native_broker=True)
    assert valid, diag


@pytest.mark.parametrize("modify,code,field", [
    (lambda a: a["reconciliation"].update(extra=True),
     "RECONCILIATION_SCHEMA_INVALID", "reconciliation"),
    (lambda a: a["reconciliation"].update(checks=[]),
     "RECONCILIATION_CHECK_COUNT_INVALID", "reconciliation.checks"),
    (lambda a: a["reconciliation"]["checks"][0].update(extra=True),
     "RECONCILIATION_CHECK_SCHEMA_INVALID", "reconciliation.checks"),
    (lambda a: a["reconciliation"]["checks"][0].update(expected_exists="true"),
     "RECONCILIATION_CHECK_OPERATOR_INVALID", "reconciliation.checks"),
    (lambda a: a["reconciliation"]["checks"][0].update(path=r"C:\other\goal_dir"),
     "RECONCILIATION_CHECK_OUTSIDE_CWD", "reconciliation.checks"),
    (lambda a: a["reconciliation"]["checks"][0].update(path=GOAL + r"\..\goal_dir"),
     "RECONCILIATION_PARENT_TRAVERSAL", "reconciliation.checks"),
    (lambda a: a.pop("working_directory"),
     "RECONCILIATION_PATH_INVALID", "working_directory"),
])
def test_invalid_contract_has_scoped_typed_repair_evidence(modify, code, field):
    action = _p7()
    modify(action)
    valid, diag = validate_tool_envelope(action, native_broker=True)
    assert not valid
    assert diag["reason"] == "timeout_reconciliation_invalid"
    assert diag["detail"] == code
    assert diag["field_issues"][0]["field"] == field
    assert diag["field_issues"][0]["constraint_code"] == code
    assert diag["repair_mode"] == "BATCH_FIELDS"
    # Do not leak absolute paths or commands into the repair diagnostic.
    assert CWD not in str(diag)
    assert GOAL not in str(diag)
    assert "Git73-Admission-Only" not in str(diag)


def test_subsequent_corrected_proposal_is_admitted_without_security_bypass():
    bad = _p7()
    bad["reconciliation"]["checks"][0]["path"] = r"C:\outside\goal_dir"
    rejected, diagnostic = validate_tool_envelope(bad, native_broker=True)
    assert not rejected
    assert diagnostic["field_issues"][0]["constraint_code"] == "RECONCILIATION_CHECK_OUTSIDE_CWD"
    repaired = copy.deepcopy(bad)
    repaired["reconciliation"]["checks"][0]["path"] = GOAL
    admitted, evidence = validate_tool_envelope(repaired, native_broker=True)
    assert admitted, evidence
