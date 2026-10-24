"""Round3 regressions for exact unquoted paths, prerequisite hints and bounded repair.

Run with PYTHONPATH=source python source/WebAgent/tests/validate_round3_prereq_repair.py.
No filesystem writes, browser changes, or model calls.
"""
from __future__ import annotations

from agent_core.bounded_repair import BoundedRepairBudget
from agent_core.prerequisite_resolution import assess_prerequisites, render_prerequisites
from WebAgent.protocol_loop import extract_authorized_paths


def run():
    fixture = r"C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1\fixture"
    file_path = fixture + r"\parent_ok\missing.txt"
    assert extract_authorized_paths(
        "請檢查 " + file_path + " 是否不存在；完成後回報"
    ) == [file_path]
    assert extract_authorized_paths(
        "請在 " + fixture + " 測試範圍內透過 Runtime 正式 run_command"
    ) == [fixture]
    assert extract_authorized_paths(
        '請讀取 "C:\\My Projects\\sample data.txt" 並回報'
    ) == [r"C:\My Projects\sample data.txt"]
    assert extract_authorized_paths(
        "請在 " + fixture + " 範圍內使用 Python 執行"
    ) == [fixture]

    env = {"tools": {"python": {"status": "AVAILABLE"}}}
    prereq = assess_prerequisites(
        "在受授權範圍內使用 run_command 執行 Python 並回報 exit code 7",
        [fixture], env,
    )
    assert prereq["request_class"] == "COMMAND"
    assert prereq["status"] == "CHECK_REQUIRED"
    assert prereq["executed"] is False
    assert prereq["authorization_granted"] is False
    assert prereq["verification_complete"] is False
    checks = {entry["id"]: entry for entry in prereq["checks"]}
    assert checks["python_capability"]["status"] == "AVAILABLE_IN_LEDGER"
    assert checks["working_directory"]["status"] == "NEEDS_RUNTIME_CONFIRMATION"
    missing = assess_prerequisites("run_command with unknown tool", [], {})
    assert {x["id"]: x["status"] for x in missing["checks"]}["python_capability"] == "REQUIRES_ACTUAL_PROBE"
    # Plain natural-language requests must not be sent a needless host-preflight prompt.
    assert render_prerequisites("你好", [], {}) == ""
    assert "SMARTAGENT_PREREQUISITE_RESOLUTION" in render_prerequisites(
        "run_command executes a bounded test", [fixture], env
    )

    budget = BoundedRepairBudget(max_without_evidence=3, max_total=10, max_same_reason=8)
    for i in range(2):
        state = budget.observe([{"reason": "WORKING_DIRECTORY_REJECTED"}], evidence_generation=0)
        assert state["status"] == "REPAIR_ALLOWED"
        assert state["rejected_turns_without_new_evidence"] == i + 1
    state = budget.observe([{"reason": "ACTION_REPAIR_REJECTED"}], evidence_generation=0)
    assert state["status"] == "EXHAUSTED"
    assert state["terminal_outcome_inferred"] is False
    assert state["action_replay_allowed"] is False
    # New actual evidence resets the no-evidence budget, not the total budget.
    state = budget.observe([{"reason": "ACTION_REPAIR_REJECTED"}], evidence_generation=1)
    assert state["status"] == "REPAIR_ALLOWED"
    assert state["rejected_turns_without_new_evidence"] == 1
    assert state["rejected_turns_total"] == 4
    return {"round3_prerequisite_bounded_repair": "PASS"}


if __name__ == "__main__":
    print(run())
