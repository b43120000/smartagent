"""IT6: Project-root request scoping and direct Tool Condition Admission.

Deterministic only: no browser, Telegram, or long-running Runtime.
PYTHONPATH=source python source/WebAgent/tests/validate_runtime_evidence_it6.py
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from WebAgent.protocol_loop import extract_authorized_paths
from WebAgent.tool_context import WebAgentToolContext
from agent_core.tool_result_contract import decode_result
from agent_core.bounded_repair import SemanticStagnationGuard


def test_project_root_suffix() -> None:
    base = r"C:\fixture\positive_project"
    cases = [
        (base + " 內，使用正式 Runtime Project Tools", [base]),
        (base + " 內，透過 query_project 查詢", [base]),
        (base + " 內 使用正式工具", [base]),
        (base + " 內；確認 Snapshot", [base]),
        # An explicitly quoted path is literal; no guessing a shorter root.
        ('"' + base + " 內" + '"，請查看', [base + " 內"]),
        (base + r"\visible_code.py 的結構化狀態", [base + r"\visible_code.py"]),
        (base + r"\file.txt:secret", [base + r"\file.txt:secret"]),
    ]
    for request, expected in cases:
        actual = extract_authorized_paths(request)
        assert actual == expected, {"request": request, "expected": expected, "actual": actual}


def test_direct_condition_admission() -> None:
    with TemporaryDirectory(prefix="smartagent_it6_condition_") as tmp:
        root = Path(tmp).resolve()
        target = root / "present.txt"
        target.write_text("exists", encoding="utf-8")
        ctx = WebAgentToolContext(root)
        ctx.current_task_epoch = "IT6-EPOCH-1"
        ctx.begin_run("IT6-REQUEST", "Check " + str(target), [str(target)])
        predicate = {"field": "file.exists", "operator": "equals", "expected": True}
        valid_text = "IT6-success-file-exists"
        action = {
            "tool": "observe_fact", "action_id": "IT6-GOOD",
            "path": str(target), "predicate": predicate, "condition_id": valid_text,
        }
        # The direct dispatcher must fail closed even though the file exists.
        outcome = decode_result(ctx.execute(action))
        assert outcome["status"] == "REJECTED" and outcome["executed"] is False, outcome
        assert outcome["reason"] == "CONDITION_NOT_REGISTERED", outcome
        ctx.bind_runtime_completion_contract(
            {"success": [valid_text]},
            request_id="IT6-REQUEST", task_epoch=ctx.current_task_epoch,
        )
        outcome = decode_result(ctx.execute(action))
        assert outcome["status"] == "SUCCEEDED" and outcome["executed"] is True, outcome
        assert outcome["reason"] == "FACT_PREDICATE_PASSED", outcome
        bad = {**action, "action_id": "IT6-INVALID",
               "condition_id": "COND-SUCCESS-INVALID-NLIT-DO-NOT-ACCEPT"}
        outcome = decode_result(ctx.execute(bad))
        assert outcome["status"] == "REJECTED" and outcome["executed"] is False, outcome
        assert outcome["reason"] == "CONDITION_NOT_REGISTERED", outcome
        try:
            ctx.bind_runtime_completion_contract(
                {"success": [valid_text]},
                request_id="OTHER-REQUEST", task_epoch=ctx.current_task_epoch,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Foreign request tried to register success conditions")
        # New request must not inherit an old task's valid binding.
        ctx.current_task_epoch = "IT6-EPOCH-2"
        ctx.begin_run("IT6-NEXT", "Check " + str(target), [str(target)])
        outcome = decode_result(ctx.execute(action))
        assert outcome["status"] == "REJECTED" and outcome["executed"] is False, outcome
        ctx.bind_runtime_completion_contract(
            {"success": [valid_text]},
            request_id="IT6-NEXT", task_epoch=ctx.current_task_epoch,
        )
        ctx.current_task_epoch = "IT6-FORGED-EPOCH"
        outcome = decode_result(ctx.execute(action))
        assert outcome["status"] == "REJECTED" and outcome["executed"] is False, outcome
        assert outcome["reason"] == "CONDITION_NOT_REGISTERED", outcome


def test_project_root_cross_tool_blockers_pause_not_success() -> None:
    guard = SemanticStagnationGuard(max_same_blocker=4)
    root = r"C:\fixture\positive_project"
    samples = [
        ({"tool": "inspect_project_scope", "workspace": root}, "TOOL_SCOPE_REJECTED", "REJECTED"),
        ({"tool": "list_directory", "path": root + " 內"}, "DIRECTORY_NOT_FOUND", "FAILED"),
        ({"tool": "query_project", "project_root": root}, "PREFLIGHT_REJECTED", "REJECTED"),
        ({"tool": "read_file", "path": root + " 內"}, "FILE_NOT_FOUND", "FAILED"),
    ]
    for index, (action, reason, status) in enumerate(samples):
        actual = guard.observe(action, {
            "result_reason": reason, "result_status": status,
            "verification_status": "UNKNOWN",
        })
        assert actual["consecutive_same_blocker"] == index + 1, actual
        assert actual["status"] == ("EXHAUSTED" if index == 3 else "CONTINUE"), actual
        assert actual["terminal_outcome_inferred"] is False
        assert actual["authorization_changed"] is False
    # An unrelated project root must not inherit a previous blocker streak.
    other = guard.observe({"tool": "list_directory", "path": r"C:\fixture\other"}, {
        "result_reason": "DIRECTORY_NOT_FOUND", "result_status": "FAILED",
        "verification_status": "UNKNOWN",
    })
    assert other["consecutive_same_blocker"] == 1, other


if __name__ == "__main__":
    test_project_root_suffix()
    test_direct_condition_admission()
    test_project_root_cross_tool_blockers_pause_not_success()
    print("RUNTIME_EVIDENCE_IT6_REGRESSION_PASS")
