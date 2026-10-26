"""IT14 exclusive-observation regression: offline, no browser/Telegram.

Run: PYTHONPATH=source python source/WebAgent/tests/validate_it14_strict_observation_guard.py
"""
from pathlib import Path
import tempfile

from agent_core.strict_observation_guard import (
    forbidden_strict_actions, strict_observation_only_request,
)
from agent_core.task_progress import read_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def request(present, contradiction):
    return (
        "NLIT_CASE_ID=IT14\n"
        "請完成一筆具有兩個事前獨立 success condition 的驗證："
        f"A：{present} 的 file.exists equals true；"
        f"B：{contradiction} 的 file.exists equals false。"
        "先在 completion_contract.success 登記 A 與 B，再依序各使用一次 canonical observe_fact。"
        "不得將 A 的 PASS 當成 B 的 PASS，不能修改 B 的 Expected Predicate，"
        "也不允許任何檔案變更或補救操作。"
    )


def test_strict_selector_and_fail_closed_tools():
    original = request("/tmp/a/present.txt", "/tmp/a/contradiction.txt")
    assert strict_observation_only_request(original)
    assert forbidden_strict_actions(original, [
        {"tool": "read_file"}, {"tool": "inspect_project_ledger"},
        {"tool": "observe_fact"}, {"tool": "final_response"},
    ]) == ["read_file", "inspect_project_ledger"]
    assert not strict_observation_only_request(
        "請先讀取檔案，使用 read_file，再各使用一次 canonical observe_fact；"
        "先在 completion_contract.success 登記，不得進行補救操作"
    )
    assert not strict_observation_only_request(
        "只執行一次 canonical observe_fact，事前登記 completion_contract.success，"
        "不得修改任何檔案。"
    )


def test_native_first_registration_no_placeholder_read():
    with tempfile.TemporaryDirectory(prefix="it14-registration-") as temp:
        root = Path(temp)
        a = root / "present.txt"
        b = root / "contradiction.txt"
        a.write_text("present")
        b.write_text("contradiction")
        original = request(a, b)
        sent = []
        def planner(prompt, expected, attachments):
            sent.append(prompt)
            if len(sent) == 1:
                # Model proposed unauthorized filler to obtain condition IDs.
                return response(progress("P-READ", a, b),
                                {"tool":"read_file", "action_id":"ILLEGAL-READ", "path":str(a)})
            if len(sent) == 2:
                assert "strict_observation_extra_action_forbidden" in prompt, prompt
                # Register first: no extra Tool Action and no invented IDs.
                return response(progress("P-REGISTER", a, b))
            assert len(sent) == 3
            assert "[WEBAGENT_TOOL_RESULTS]" in prompt, prompt
            ledger = read_progress(loop.task_id, root)
            assert len(ledger.completion_contract["success"]) == 2
            assert not loop.action_result_ledger, "registration is NOT observation"
            raise RuntimeError("EXPECTED_AFTER_REGISTRATION")

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        try:
            loop.run(original, trusted_authorized_paths=[str(a),str(b)])
        except RuntimeError as exc:
            assert "EXPECTED_AFTER_REGISTRATION" in str(exc), exc
        else:
            raise AssertionError("registration loop should be interrupted by test")
        assert len(sent) == 3
        assert not loop.action_ledger
        assert not loop.action_result_ledger


def progress(action_id, a, b):
    return {
        "tool": "report_progress", "action_id": action_id,
        "base_evaluation": "只允許兩個 canonical observe_fact，先取得兩個條件",
        "total_steps": 3, "current_step": 1,
        "steps": [
            {"step":1, "desc":"登記兩個獨立成功條件", "status":"IN_PROGRESS"},
            {"step":2, "desc":"驗證 A", "status":"PENDING"},
            {"step":3, "desc":"驗證 B", "status":"PENDING"}
        ],
        "current_focus": "先取得正式 condition_id",
        "next_action": "observe_fact",
        "completion_contract": {
            "success":[f"A {a} file.exists equals true",f"B {b} file.exists equals false"],
            "failure":["任一事前成功條件 FAIL"],
            "in_progress":["尚待正式 condition_id"],
            "interrupted":["Runtime 無法安全登記條件"]
        },
        "decision":"CONTINUE", "outcome":"PENDING",
        "matched_condition":"尚待正式 condition_id",
        "evidence_refs":["RUNTIME_STATUS"],
        "decision_reason":"事前登記，不執行額外讀取或補救操作"
    }


if __name__ == "__main__":
    test_strict_selector_and_fail_closed_tools()
    test_native_first_registration_no_placeholder_read()
    print("IT14_STRICT_OBSERVATION_GUARD_OFFLINE_PASS")
