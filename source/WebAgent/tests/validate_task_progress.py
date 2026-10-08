#!/usr/bin/env python3
"""Offline validation for the software-owned task progress ledger."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.remote_control_dispatcher import RemoteControlDispatcher
from agent_core.task_progress import (
    TaskProgressError,
    delete_progress,
    format_telegram_status_view,
    get_ledger_path,
    initialize_progress,
    read_progress,
    record_model_progress,
    set_runtime_state,
    validate_model_progress,
)


def _expect_progress_error(callback) -> None:
    try:
        callback()
    except TaskProgressError:
        return
    raise AssertionError("expected TaskProgressError")


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="task-progress-") as temp:
        root = Path(temp)
        task_id = "TASK-PROGRESS-1"
        request_id = "REQ-PROGRESS-1"
        goal = "修改專案 source code 並驗證"
        initial = initialize_progress(
            task_id, request_id=request_id, goal=goal, root=root
        )
        assert initial.current_step == 0

        _expect_progress_error(lambda: record_model_progress(
            task_id,
            {
                "current_step": 2.5,
                "total_steps": 6,
                "current_focus": "評估 Base",
            },
            request_id=request_id,
            goal=goal,
            round_id=1,
            root=root,
        ))

        steps = [
            {"step": number, "desc": f"階段 {number}", "status": status}
            for number, status in (
                (1, "COMPLETED"), (2, "COMPLETED"), (3, "IN_PROGRESS"),
                (4, "PENDING"), (5, "PENDING"), (6, "PENDING"),
            )
        ]
        completion_contract = {
            "success": ["修改完成且驗證成功"],
            "failure": ["修改工作已結束但驗證失敗"],
            "in_progress": ["仍有修改或驗證步驟尚未完成"],
            "interrupted": ["Runtime 無法繼續執行或取得證據"],
        }
        uniquely_bound = validate_model_progress({
            "base_evaluation": "測試唯一條件綁定",
            "current_step": 1,
            "total_steps": 2,
            "steps": [
                {"step": 1, "desc": "分析", "status": "IN_PROGRESS"},
                {"step": 2, "desc": "回報", "status": "PENDING"},
            ],
            "current_focus": "分析中",
            "next_action": "完成分析",
            "completion_contract": completion_contract,
            "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "Required work remains.",
            "evidence_refs": ["REQUEST_ACCEPTED"],
            "decision_reason": "尚未完成",
        })
        assert uniquely_bound["matched_condition"] == "仍有修改或驗證步驟尚未完成"

        ambiguous_contract = {
            **completion_contract,
            "in_progress": [
                "仍有修改或驗證步驟尚未完成",
                "等待外部工具結果",
            ],
        }
        try:
            validate_model_progress({
                "base_evaluation": "測試多條件拒絕",
                "current_step": 1,
                "total_steps": 2,
                "steps": [
                    {"step": 1, "desc": "分析", "status": "IN_PROGRESS"},
                    {"step": 2, "desc": "回報", "status": "PENDING"},
                ],
                "current_focus": "分析中",
                "next_action": "完成分析",
                "completion_contract": ambiguous_contract,
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "Required work remains.",
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "尚未完成",
            })
        except TaskProgressError as exc:
            assert exc.field == "matched_condition"
            assert exc.repair_context == {
                "condition_class": "in_progress",
                "actual_condition": "Required work remains.",
                "allowed_conditions": ambiguous_contract["in_progress"],
            }
        else:
            raise AssertionError("ambiguous matched_condition must remain fail-closed")

        ledger = record_model_progress(
            task_id,
            {
                "base_evaluation": "Base 已載入，位於第 2.5 階段",
                "current_step": 2.5,
                "total_steps": 6,
                "steps": steps,
                "current_focus": "完成第 3 階段",
                "next_action": "執行最小範圍程式修改",
                "completion_contract": completion_contract,
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "仍有修改或驗證步驟尚未完成",
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "Runtime 尚未提供修改與驗證完成證據",
            },
            request_id=request_id,
            goal=goal,
            round_id=1,
            root=root,
        )
        assert ledger.current_step == 2.5
        assert read_progress(task_id, root).base_evaluation.startswith("Base 已載入")

        ledger = record_model_progress(
            task_id,
            {
                "current_step": 3,
                "total_steps": 6,
                "current_focus": "驗證修改",
                "next_action": "執行測試",
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "仍有修改或驗證步驟尚未完成",
                "evidence_refs": ["RUNTIME_STATUS"],
                "decision_reason": "修改完成但測試尚未執行",
            },
            request_id=request_id,
            goal=goal,
            round_id=2,
            root=root,
        )
        assert len(ledger.history) == 2
        expanded_contract = {
            **completion_contract,
            "failure": [
                *completion_contract["failure"],
                "驗證工具完成但輸出不包含必要 evidence",
            ],
        }
        ledger = record_model_progress(
            task_id,
            {
                "current_step": 3.5,
                "total_steps": 6,
                "current_focus": "補充新發現的失敗條件",
                "next_action": "執行測試",
                "completion_contract": expanded_contract,
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "仍有修改或驗證步驟尚未完成",
                "evidence_refs": ["RUNTIME_STATUS"],
                "decision_reason": "新 evidence 顯示完成契約需要擴充",
            },
            request_id=request_id,
            goal=goal,
            round_id=3,
            root=root,
        )
        assert len(ledger.completion_contract["failure"]) == 2
        ledger = record_model_progress(
            task_id,
            {
                "current_step": 3.5,
                "total_steps": 6,
                "current_focus": "錯誤縮小契約",
                "next_action": "執行測試",
                "completion_contract": completion_contract,
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "仍有修改或驗證步驟尚未完成",
                "evidence_refs": ["RUNTIME_STATUS"],
                "decision_reason": "Runtime 應保留模型本輪省略的既有條件",
            },
            request_id=request_id,
            goal=goal,
            round_id=4,
            root=root,
        )
        assert len(ledger.completion_contract["failure"]) == 2
        assert "驗證工具完成但輸出不包含必要 evidence" in ledger.completion_contract["failure"]
        _expect_progress_error(lambda: record_model_progress(
            task_id,
            {
                "current_step": 2,
                "total_steps": 6,
                "current_focus": "倒退",
                "next_action": "重新執行先前步驟",
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "仍有修改或驗證步驟尚未完成",
                "evidence_refs": ["RUNTIME_STATUS"],
                "decision_reason": "測試倒退應被拒絕",
            },
            request_id=request_id,
            goal=goal,
            round_id=5,
            root=root,
        ))

        # Telegram status must use the ledger without touching the browser.
        task = SimpleNamespace(
            task_id=task_id,
            state="RUNNING",
            workspace=str(root),
            created_at=1.0,
            metadata={},
            reply_route={"chat_id": 7},
        )
        dispatcher = RemoteControlDispatcher.__new__(RemoteControlDispatcher)
        dispatcher.root = root
        dispatcher._latest_telegram_task_for_status = lambda message: (None, task)
        dispatcher._execution_status_for_task = lambda selected: {}
        dispatcher._latest_telegram_attachment_names = lambda message: []
        dispatcher._run_browser_control = lambda command: (_ for _ in ()).throw(
            AssertionError("browser status fallback must not run when ledger exists")
        )
        status = dispatcher._run_status_control(
            SimpleNamespace(reply_context={"chat_id": 7})
        )
        assert status["status_source"] == "task_progress_ledger"
        assert "3.5 / 6" in status["message"]
        assert status["generation_active"] is True

        interrupted = set_runtime_state(
            task_id, "INTERRUPTED", reason="測試中斷", root=root
        )
        assert interrupted is not None
        assert interrupted.interruption_reason == "測試中斷"
        assert "已中斷" in format_telegram_status_view(interrupted)
        assert get_ledger_path(task_id, root).is_file()
        assert delete_progress(task_id, root) is True
        assert read_progress(task_id, root) is None

        return {
            "fractional_stage": True,
            "runtime_persistence": True,
            "backward_progress_rejected": True,
            "telegram_software_status": True,
            "interruption_preserved": True,
            "completion_cleanup_primitive": True,
            "unique_condition_runtime_binding": True,
            "ambiguous_condition_fail_closed": True,
        }


if __name__ == "__main__":
    import json

    print("TASK_PROGRESS_VALIDATION_OK")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
