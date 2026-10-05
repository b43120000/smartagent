#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression checks for completed Progress terminal lifecycle handling."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.task_progress import read_progress


def fence(payload: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def completed_progress(action_id: str) -> dict:
    return {
        "tool": "report_progress",
        "action_id": action_id,
        "base_evaluation": "編譯驗證已執行，結果可回報",
        "total_steps": 1,
        "current_step": 1,
        "steps": [{"step": 1, "desc": "執行並回報編譯", "status": "COMPLETED"}],
        "current_focus": "編譯驗證已完成",
        "next_action": "回覆編譯失敗摘要",
        "completion_contract": {
            "success": ["編譯結束且 exit code=0，APK 已產生"],
            "failure": ["編譯已結束但 exit code 非零或 APK 未產生"],
            "in_progress": ["編譯程序仍在執行"],
            "interrupted": ["Runtime 無法取得編譯程序結果"],
        },
        "decision": "COMPLETE",
        "outcome": "FAILED",
        "matched_condition": "編譯已結束但 exit code 非零或 APK 未產生",
        "evidence_refs": ["RUNTIME_STATUS"],
        "decision_reason": "Runtime 顯示編譯已結束且 exit code=1",
    }


def response(*blocks: dict) -> str:
    action_count = len(blocks)
    return "\n".join(
        [*(fence(block) for block in blocks), fence({"tool": "turn_commit", "action_count": action_count})]
    )


def validate_failed_verification_can_complete() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-terminal-failed-") as temp:
        root = Path(temp)

        def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
            loop.tools.last_verification_status = "FAIL"
            return response(
                completed_progress("P-COMPLETE"),
                {
                    "tool": "final_response",
                    "action_id": "A-FINAL",
                    "content": "驗證工作完成；編譯失敗，exit code=1。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        result = loop.run("只驗證編譯並回報結果")
        assert result == "驗證工作完成；編譯失敗，exit code=1。"
        assert loop.terminal_outcome == "FAILED"
        ledger = read_progress(loop.task_id, root=root)
        assert ledger is not None and ledger.runtime_state == "COMPLETED"


def validate_repeated_completed_progress_is_paused() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-terminal-bounded-") as temp:
        root = Path(temp)
        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            return response(completed_progress(f"P-{len(prompts)}"))

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=20)
        try:
            loop.run("只驗證編譯並回報結果")
        except RuntimeError as exc:
            assert "連續回傳相同且不符合完成契約" in str(exc)
        else:
            raise AssertionError("completed Progress without final_response must be bounded")
        assert len(prompts) == 2
        assert "[WEBAGENT_ACK_REJECTED]" in prompts[1]
        ledger = read_progress(loop.task_id, root=root)
        assert ledger is not None and ledger.runtime_state == "PAUSED"
        assert ledger.interruption_reason == "semantic_stagnation:repeated_progress_contract_rejection"


def validate_model_interrupt_is_persisted() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-model-interrupt-") as temp:
        root = Path(temp)

        def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
            progress = completed_progress("P-INTERRUPT")
            progress.update({
                "current_step": 0.5,
                "steps": [{"step": 1, "desc": "取得編譯狀態", "status": "IN_PROGRESS"}],
                "decision": "INTERRUPT",
                "outcome": "UNKNOWN",
                "matched_condition": "Runtime 無法取得編譯程序結果",
                "decision_reason": "Runtime process identity 已遺失",
            })
            return response(
                progress,
                {
                    "tool": "final_response",
                    "action_id": "A-INTERRUPTED",
                    "content": "無法取得編譯程序結果，任務已中斷。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        try:
            loop.run("只驗證編譯並回報結果")
        except RuntimeError as exc:
            assert "模型決策中斷" in str(exc)
        else:
            raise AssertionError("INTERRUPT decision must stop the protocol loop")
        ledger = read_progress(loop.task_id, root=root)
        assert ledger is not None and ledger.runtime_state == "INTERRUPTED"
        assert ledger.interruption_reason == "Runtime process identity 已遺失"


def validate_success_cannot_contradict_failed_runtime() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-runtime-contradiction-") as temp:
        root = Path(temp)
        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            loop.tools.last_verification_status = "FAIL"
            progress = completed_progress(f"P-SUCCESS-{len(prompts)}")
            progress.update({
                "outcome": "SUCCESS",
                "matched_condition": "編譯結束且 exit code=0，APK 已產生",
                "decision_reason": "錯誤宣告成功",
            })
            return response(
                progress,
                {"tool": "final_response", "action_id": f"A-SUCCESS-{len(prompts)}", "content": "成功"},
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=20)
        try:
            loop.run("只驗證編譯並回報結果")
        except RuntimeError as exc:
            assert "不符合完成契約" in str(exc)
        else:
            raise AssertionError("SUCCESS must not override failed Runtime verification")
        assert len(prompts) == 2
        assert "success_contradicts_runtime_verification" in prompts[1]


def validate_unverified_success_requests_and_obtains_evidence() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-verification-gap-") as temp:
        root = Path(temp)
        prompts: list[str] = []
        calls = 0

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            nonlocal calls
            calls += 1
            prompts.append(prompt)
            if calls == 1:
                loop.tools.last_verification_status = "UNVERIFIED"
                progress = completed_progress("P-UNVERIFIED")
                progress.update({
                    "outcome": "SUCCESS",
                    "matched_condition": "checkpoint commit 已建立且 HEAD 可解析",
                    "decision_reason": "commit command returned zero",
                })
                progress["completion_contract"]["success"].append(
                    "checkpoint commit 已建立且 HEAD 可解析"
                )
                return response(progress, {
                    "tool": "final_response",
                    "action_id": "A-EARLY-SUCCESS",
                    "content": "checkpoint completed",
                })
            if calls == 2:
                assert "WEBAGENT_VERIFICATION_REQUIRED" in prompt
                assert '"missing_condition":"checkpoint commit 已建立且 HEAD 可解析"' in prompt
                assert '"required_action":"run_command"' in prompt
                assert '"required_fields":["command","success_criteria","verify"]' in prompt
                progress = completed_progress("P-VERIFY")
                progress.update({
                    "current_step": 0.5,
                    "steps": [{"step": 1, "desc": "verify checkpoint", "status": "IN_PROGRESS"}],
                    "current_focus": "obtain structured verification",
                    "next_action": "verify HEAD",
                    "decision": "CONTINUE",
                    "outcome": "PENDING",
                    "matched_condition": "編譯程序仍在執行",
                    "decision_reason": "Runtime requested PASS/FAIL evidence",
                })
                return response(progress, {
                    "tool": "run_command",
                    "action_id": "A-VERIFY-HEAD",
                    "command": "git rev-parse --verify HEAD",
                    "success_criteria": "HEAD resolves to an existing commit",
                    "verify": [{
                        "action": "run_command",
                        "command": "git rev-parse --verify HEAD",
                        "expect_exit_code": 0,
                    }],
                })
            progress = completed_progress("P-VERIFIED")
            progress.update({
                "outcome": "SUCCESS",
                "matched_condition": "編譯結束且 exit code=0，APK 已產生",
                "evidence_refs": ["A-VERIFY-HEAD"],
                "decision_reason": "Runtime returned PASS evidence",
            })
            return response(progress, {
                "tool": "final_response",
                "action_id": "A-VERIFIED-FINAL",
                "content": "checkpoint verified",
            })

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=6)

        def fake_execute(_action: dict) -> str:
            loop.tools.last_verification_status = "PASS"
            return "VERIFICATION_STATUS: PASS"

        loop._execute_action = fake_execute
        result = loop.run("create and verify checkpoint")
        assert result == "checkpoint verified"
        assert calls == 3
        assert loop.pending_verification_requirement == {}


def validate_verification_gap_rejects_progress_only() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-verification-action-required-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.tools.last_verification_status = "UNVERIFIED"
        loop.pending_verification_requirement = {
            "missing_condition": "checkpoint HEAD exists",
            "current_verification_status": "UNVERIFIED",
            "required_action": "run_command",
            "required_fields": ["command", "success_criteria", "verify"],
            "supported_verify_actions": ["run_command"],
            "evidence_refs": ["A-COMMIT"],
        }
        progress = completed_progress("P-WAIT")
        progress.update({
            "current_step": 0.5,
            "steps": [{"step": 1, "desc": "verify", "status": "IN_PROGRESS"}],
            "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "編譯程序仍在執行",
        })
        accepted, diagnostics = loop._accept_ack(
            [progress, {"tool": "turn_commit", "action_count": 1}],
            {"ack_web_ack_id": ""},
        )
        assert not accepted
        assert diagnostics[0]["reason"] == "verification_evidence_action_required"


def validate_changed_repair_is_not_a_stall() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-semantic-repair-") as temp:
        root = Path(temp)
        calls = 0

        def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
            nonlocal calls
            calls += 1
            loop.tools.last_verification_status = "FAIL"
            progress = completed_progress(f"P-REPAIR-{calls}")
            if calls == 1:
                progress.update({
                    "outcome": "SUCCESS",
                    "matched_condition": "編譯結束且 exit code=0，APK 已產生",
                    "decision_reason": "第一次錯誤宣告成功",
                })
            return response(
                progress,
                {
                    "tool": "final_response",
                    "action_id": f"A-REPAIR-{calls}",
                    "content": "驗證完成；編譯失敗。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=20)
        result = loop.run("只驗證編譯並回報結果")
        assert result == "驗證完成；編譯失敗。"
        assert calls == 2
        assert loop.terminal_outcome == "FAILED"


def validate_runtime_binds_latest_terminal_evidence() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-terminal-binding-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.run_id = "RR-EVIDENCE-BIND"
        loop.task_id = "TASK-EVIDENCE-BIND"
        loop.task_epoch = "EPOCH-EVIDENCE-BIND"
        loop.intent_digest = "a" * 64
        loop.turn_id = 2
        loop.action_result_ledger["A-LIST-COMPLETED"] = {"status": "COMPLETED"}
        progress = completed_progress("P-BIND")
        final = {
            "tool": "final_response",
            "action_id": "A-FINAL-BIND",
            "content": "盤點完成。",
        }
        calls = [progress, final, {"tool": "turn_commit", "action_count": 2}]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert not diagnostics
        assert accepted[0]["evidence_refs"] == ["RUNTIME_STATUS", "A-LIST-COMPLETED"]


if __name__ == "__main__":
    validate_failed_verification_can_complete()
    validate_repeated_completed_progress_is_paused()
    validate_model_interrupt_is_persisted()
    validate_success_cannot_contradict_failed_runtime()
    validate_unverified_success_requests_and_obtains_evidence()
    validate_verification_gap_rejects_progress_only()
    validate_changed_repair_is_not_a_stall()
    validate_runtime_binds_latest_terminal_evidence()
    print("WEBAGENT_PROGRESS_TERMINAL_LIFECYCLE_OK")
