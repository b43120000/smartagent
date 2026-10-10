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
from agent_core.task_progress import condition_id_for, read_progress, record_model_progress


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
        turns = 0

        def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(
                    {
                        "tool": "report_progress", "action_id": "P-RUN",
                        "base_evaluation": "需要執行編譯驗證",
                        "total_steps": 1, "current_step": 0.5,
                        "steps": [{"step": 1, "desc": "執行並回報編譯", "status": "IN_PROGRESS"}],
                        "current_focus": "執行編譯驗證", "next_action": "run_command",
                        "completion_contract": completed_progress("P-TEMPLATE")["completion_contract"],
                        "decision": "CONTINUE", "outcome": "PENDING",
                        "matched_condition": "編譯程序仍在執行",
                        "evidence_refs": ["REQUEST_ACCEPTED"],
                        "decision_reason": "尚未取得實際編譯結果",
                    },
                    {
                        "tool": "run_command", "action_id": "A-BUILD",
                        "operation": "BUILD",
                        "command": "exit 1",
                        "success_criteria": "command exits with zero",
                        "verify": [{"action": "run_command", "command": "exit 1", "expect_exit_code": 0}],
                    },
                )
            terminal = completed_progress("P-COMPLETE")
            terminal["evidence_refs"] = ["A-BUILD"]
            return response(
                terminal,
                {
                    "tool": "final_response",
                    "action_id": "A-FINAL",
                    "content": "驗證工作完成；編譯失敗，exit code=1。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        loop._execute_action = lambda _action: (
            "[COMMAND_RESULT]\nexit_code: 1\nVERIFICATION_STATUS: FAIL"
        )
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
        assert ledger.interruption_reason.startswith(
            "semantic_stagnation:repeated_progress_contract_rejection:"
        )


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
            if len(prompts) == 1:
                return response(
                    {
                        "tool": "report_progress", "action_id": "P-FAIL-RUN",
                        "base_evaluation": "需要執行編譯驗證", "total_steps": 1,
                        "current_step": 0.5,
                        "steps": [{"step": 1, "desc": "執行並回報編譯", "status": "IN_PROGRESS"}],
                        "current_focus": "執行編譯驗證", "next_action": "run_command",
                        "completion_contract": completed_progress("P-TEMPLATE")["completion_contract"],
                        "decision": "CONTINUE", "outcome": "PENDING",
                        "matched_condition": "編譯程序仍在執行",
                        "evidence_refs": ["REQUEST_ACCEPTED"],
                        "decision_reason": "尚未取得實際編譯結果",
                    },
                    {
                        "tool": "run_command", "action_id": "A-FAILED-BUILD",
                        "operation": "BUILD",
                        "command": "exit 1", "success_criteria": "command exits with zero",
                        "verify": [{"action": "run_command", "command": "exit 1", "expect_exit_code": 0}],
                    },
                )
            progress = completed_progress(f"P-SUCCESS-{len(prompts)}")
            progress.update({
                "outcome": "SUCCESS",
                "matched_condition": "編譯結束且 exit code=0，APK 已產生",
                "evidence_refs": ["A-FAILED-BUILD"],
                "decision_reason": "錯誤宣告成功",
            })
            return response(
                progress,
                {"tool": "final_response", "action_id": f"A-SUCCESS-{len(prompts)}", "content": "成功"},
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=20)
        loop._execute_action = lambda _action: (
            "[COMMAND_RESULT]\nexit_code: 1\nVERIFICATION_STATUS: FAIL"
        )
        try:
            loop.run("只驗證編譯並回報結果")
        except RuntimeError as exc:
            assert "不符合完成契約" in str(exc)
        else:
            raise AssertionError("SUCCESS must not override failed Runtime verification")
        assert len(prompts) == 3
        assert "success_contradicts_runtime_verification" in prompts[2]


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
                assert '"required_fields":["operation","command","success_criteria","verify"]' in prompt
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
                    "operation": "VERIFY",
                    "condition_id": "checkpoint commit 已建立且 HEAD 可解析",
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
        # This unit isolates changed semantic repair behavior.  Action-scoped
        # evidence routing is covered separately.
        loop._terminal_evidence_verdict = lambda _refs, **_kwargs: "FAIL"
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
        # Terminal FAILED may only bind to concrete Runtime failure evidence;
        # a generic COMPLETED marker is intentionally insufficient.
        loop.action_result_ledger["A-LIST-COMPLETED"] = {
            "tool": "run_command",
            "execution_status": "FAILED",
            "verification_status": "FAIL",
            "effective_verification_status": "FAIL",
            "evidence_state": "AVAILABLE",
        }
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


def validate_complete_step_alias_is_canonicalized() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-complete-alias-") as temp:
        root = Path(temp)

        def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
            progress = completed_progress("P-COMPLETE-ALIAS")
            progress["steps"][0]["status"] = "COMPLETE"
            return response(
                progress,
                {
                    "tool": "final_response",
                    "action_id": "A-COMPLETE-ALIAS",
                    "content": "驗證完成。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        # This case only verifies status-alias normalization; terminal
        # evidence semantics are covered by the action-scoped cases above.
        loop._terminal_evidence_verdict = lambda _refs, **_kwargs: "FAIL"
        assert loop.run("只驗證並回報結果") == "驗證完成。"
        ledger = read_progress(loop.task_id, root=root)
        assert ledger is not None
        assert ledger.steps[0]["status"] == "COMPLETED"


def validate_terminal_progress_repair_is_explicit() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-terminal-repair-") as temp:
        root = Path(temp)
        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            progress = completed_progress(f"P-TERMINAL-REPAIR-{len(prompts)}")
            if len(prompts) == 1:
                progress["steps"][0]["status"] = "IN_PROGRESS"
            return response(
                progress,
                {
                    "tool": "final_response",
                    "action_id": f"A-TERMINAL-REPAIR-{len(prompts)}",
                    "content": "驗證完成。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        # This case isolates terminal Progress repair prompting rather than
        # Runtime evidence classification.
        loop._terminal_evidence_verdict = lambda _refs, **_kwargs: "FAIL"
        assert loop.run("只驗證並回報結果") == "驗證完成。"
        assert len(prompts) == 2
        assert "steps[].status 合法值只有 PENDING、IN_PROGRESS、COMPLETED" in prompts[1]
        assert "所有 steps[].status 都必須是 COMPLETED" in prompts[1]


def validate_rejection_signature_tracks_repair_progress() -> None:
    complete_steps = [{"step": 1, "desc": "verify", "status": "COMPLETE"}]
    pending_steps = [{"step": 1, "desc": "verify", "status": "IN_PROGRESS"}]
    base = {
        "tool": "report_progress",
        "decision": "COMPLETE",
        "outcome": "SUCCESS",
        "evidence_refs": ["RUNTIME_STATUS"],
    }
    first = WebAgentProtocolLoop._rejected_exchange_signature(
        [{**base, "steps": complete_steps}],
        [{"reason": "invalid_progress_payload", "detail": "invalid step status: COMPLETE"}],
        "",
    )
    changed_steps = WebAgentProtocolLoop._rejected_exchange_signature(
        [{**base, "steps": pending_steps}],
        [{"reason": "invalid_progress_payload", "detail": "COMPLETE requires every progress step to be COMPLETED"}],
        "",
    )
    changed_detail = WebAgentProtocolLoop._rejected_exchange_signature(
        [{**base, "steps": complete_steps}],
        [{"reason": "invalid_progress_payload", "detail": "different concrete schema error"}],
        "",
    )
    changed_condition = WebAgentProtocolLoop._rejected_exchange_signature(
        [{**base, "steps": complete_steps, "matched_condition": "another condition"}],
        [{
            "reason": "invalid_progress_payload",
            "detail": "matched_condition is not declared in completion_contract.in_progress",
            "condition_class": "in_progress",
            "actual_condition": "another condition",
            "allowed_conditions": ["still running", "waiting for evidence"],
        }],
        "",
    )
    assert first != changed_steps
    assert first != changed_detail
    assert first != changed_condition


def validate_nested_progress_wrapper_is_safely_normalized() -> None:
    nested = {
        "tool": "report_progress",
        "action_id": "P-NESTED",
        "progress": {
            "base_evaluation": "等待列出目錄",
            "total_steps": 2,
            "current_step": 1,
            "steps": [
                {"step": 1, "desc": "列出目錄", "status": "IN_PROGRESS"},
                {"step": 2, "desc": "回報", "status": "PENDING"},
            ],
            "current_focus": "列出目錄",
            "next_action": "執行 list_directory",
            "completion_contract": {
                "success": ["目錄內容已取得"],
                "failure": ["列出目錄失敗"],
                "in_progress": ["尚未取得目錄內容"],
                "interrupted": ["Runtime 無法繼續"],
            },
            "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "尚未取得目錄內容",
            "evidence_refs": ["REQUEST_ACCEPTED"],
            "decision_reason": "尚未執行 action",
        },
    }
    normalized, diagnostic = WebAgentProtocolLoop._normalize_report_progress_envelope(nested)
    assert diagnostic is None
    assert "progress" not in normalized
    assert normalized["action_id"] == "P-NESTED"
    assert normalized["total_steps"] == 2
    assert normalized["decision"] == "CONTINUE"

    with tempfile.TemporaryDirectory(prefix="nested-progress-accept-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.run_id = "RR-NESTED-PROGRESS"
        loop.task_id = "TASK-NESTED-PROGRESS"
        loop.task_epoch = "EPOCH-NESTED-PROGRESS"
        loop.intent_digest = "DIGEST-NESTED-PROGRESS"
        accepted, diagnostics = loop._accept_ack(
            [
                nested,
                {"tool": "list_directory", "action_id": "A-LIST", "path": str(root)},
                {"tool": "turn_commit", "action_count": 2},
            ],
            {"ack_web_ack_id": ""},
        )
        assert diagnostics == []
        assert accepted[0]["tool"] == "report_progress"
        assert "progress" not in accepted[0]
        assert accepted[0]["total_steps"] == 2

    conflicting = {**nested, "total_steps": 3}
    normalized, diagnostic = WebAgentProtocolLoop._normalize_report_progress_envelope(conflicting)
    assert normalized == {}
    assert diagnostic is not None
    assert diagnostic["reason"] == "progress_wrapper_conflict"
    assert diagnostic["detail"] == "conflicting_fields=total_steps"

    changed_nested = json.loads(json.dumps(nested, ensure_ascii=False))
    changed_nested["progress"]["current_step"] = 1.5
    first_signature = WebAgentProtocolLoop._rejected_exchange_signature(
        [nested], [{"reason": "invalid_progress_payload", "detail": "test"}], "",
    )
    changed_signature = WebAgentProtocolLoop._rejected_exchange_signature(
        [changed_nested], [{"reason": "invalid_progress_payload", "detail": "test"}], "",
    )
    assert first_signature != changed_signature


def validate_missing_capability_claim_gets_runtime_guidance() -> None:
    guidance = WebAgentProtocolLoop._progress_capability_guidance({
        "current_focus": "等待可用修改 action。",
        "next_action": "取得檔案修改能力後再繼續。",
        "decision_reason": "目前沒有提供可執行修改能力。",
    })
    assert guidance.startswith("[SMARTAGENT_CAPABILITY_GUIDANCE]")
    assert "write_file" in guidance
    assert "不得再以『缺少修改/命令能力』等待" in guidance
    assert WebAgentProtocolLoop._progress_capability_guidance({
        "current_focus": "正在分析檔案",
        "next_action": "執行 query_project",
    }) == ""


def validate_nested_progress_capability_recovery_reaches_next_prompt() -> None:
    with tempfile.TemporaryDirectory(prefix="nested-progress-capability-") as temp:
        root = Path(temp)
        prompts: list[str] = []

        def nested_progress(action_id: str) -> dict:
            return {
                "tool": "report_progress",
                "action_id": action_id,
                "progress": {
                    "base_evaluation": "需要修改程式碼",
                    "total_steps": 2,
                    "current_step": 1,
                    "steps": [
                        {"step": 1, "desc": "確認範圍", "status": "COMPLETED"},
                        {"step": 2, "desc": "修改與驗證", "status": "IN_PROGRESS"},
                    ],
                    "current_focus": "等待可用修改 action。",
                    "next_action": "取得檔案修改能力後執行修改。",
                    "completion_contract": {
                        "success": ["修改與驗證完成"],
                        "failure": ["修改或驗證失敗"],
                        "in_progress": ["尚未執行修改"],
                        "interrupted": ["Runtime 無法繼續"],
                    },
                    "decision": "CONTINUE",
                    "outcome": "PENDING",
                    "matched_condition": "尚未執行修改",
                    "evidence_refs": ["REQUEST_ACCEPTED"],
                    "decision_reason": "目前沒有提供可執行修改能力。",
                },
            }

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return response(
                    nested_progress("P-NESTED-CAP-1"),
                    {
                        "tool": "final_response",
                        "action_id": "A-PREMATURE",
                        "content": "目前沒有修改能力。",
                    },
                )
            if len(prompts) == 2:
                assert "TERMINAL_DECISION_REPAIR" in prompt
                return response(nested_progress("P-NESTED-CAP-2"))
            assert "[SMARTAGENT_CAPABILITY_GUIDANCE]" in prompt
            assert "write_file" in prompt
            raise RuntimeError("STOP_AFTER_CAPABILITY_GUIDANCE_ASSERT")

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        try:
            loop.run("修改開發版程式碼")
        except RuntimeError as exc:
            assert "STOP_AFTER_CAPABILITY_GUIDANCE_ASSERT" in str(exc)
        else:
            raise AssertionError("test planner must stop after capability guidance")
        assert len(prompts) == 3


def validate_progress_repair_is_condition_specific() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-condition-repair-") as temp:
        root = Path(temp)
        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            if len(prompts) == 2:
                assert "TERMINAL_DECISION_REPAIR" in prompt
                assert "必須二選一" in prompt
                raise RuntimeError("STOP_AFTER_PROGRESS_REPAIR_ASSERT")
            progress = {
                "tool": "report_progress",
                "action_id": "P-CONDITION-REPAIR",
                "base_evaluation": "架構規劃仍在進行",
                "total_steps": 2,
                "current_step": 1,
                "steps": [
                    {"step": 1, "desc": "分析架構", "status": "IN_PROGRESS"},
                    {"step": 2, "desc": "整理規劃", "status": "PENDING"},
                ],
                "current_focus": "分析架構",
                "next_action": "完成整合點規劃",
                "completion_contract": {
                    "success": ["規劃已完成"],
                    "failure": ["無法形成規劃"],
                    "in_progress": ["仍需完成模組邊界與資料流分析。"],
                    "interrupted": ["必要資訊無法取得"],
                },
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "Required architecture analysis remains.",
                "evidence_refs": ["REQUEST_ACCEPTED", "RUNTIME_STATUS"],
                "decision_reason": "尚未完成分析",
            }
            return response(
                progress,
                {
                    "tool": "final_response",
                    "action_id": "A-PREMATURE-FINAL",
                    "content": "初步規劃完成。",
                },
            )

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        try:
            loop.run("規劃紀錄簿功能")
        except RuntimeError as exc:
            assert "STOP_AFTER_PROGRESS_REPAIR_ASSERT" in str(exc)
        else:
            raise AssertionError("test planner must stop after observing repair prompt")
        assert len(prompts) == 2


def validate_ambiguous_condition_repair_lists_candidates() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-condition-list-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        progress = {
            "tool": "report_progress",
            "action_id": "P-AMBIGUOUS-CONDITION",
            "base_evaluation": "分析仍在進行",
            "total_steps": 2,
            "current_step": 1,
            "steps": [
                {"step": 1, "desc": "分析", "status": "IN_PROGRESS"},
                {"step": 2, "desc": "回報", "status": "PENDING"},
            ],
            "current_focus": "分析",
            "next_action": "繼續分析",
            "completion_contract": {
                "success": ["完成"],
                "failure": ["失敗"],
                "in_progress": ["仍在分析", "等待工具結果"],
                "interrupted": ["中斷"],
            },
            "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "Analysis remains.",
            "evidence_refs": ["REQUEST_ACCEPTED"],
            "decision_reason": "尚未完成",
        }
        calls = [
            progress,
            {"tool": "list_directory", "action_id": "A-LIST", "path": str(root)},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert accepted == []
        assert diagnostics[0]["condition_class"] == "in_progress"
        assert diagnostics[0]["actual_condition"] == "Analysis remains."
        assert diagnostics[0]["allowed_conditions"] == ["仍在分析", "等待工具結果"]


def validate_abnormal_exit_releases_request_ownership() -> None:
    import agent_core.request_ownership as ownership

    released: list[str] = []
    original_release = ownership.release_active_request
    ownership.release_active_request = lambda rid: released.append(str(rid)) or True
    try:
        with tempfile.TemporaryDirectory(prefix="progress-release-on-error-") as temp:
            root = Path(temp)

            def planner(_prompt: str, _expected: dict, _attachments: list[str]) -> str:
                raise RuntimeError("planner transport failed")

            loop = WebAgentProtocolLoop(root, planner, progress_root=root)
            try:
                loop.run("觸發 transport failure", request_id="RR-RELEASE-ON-ERROR")
            except RuntimeError as exc:
                assert "planner transport failed" in str(exc)
            else:
                raise AssertionError("planner failure must propagate")
    finally:
        ownership.release_active_request = original_release
    assert released == ["RR-RELEASE-ON-ERROR"]


def validate_verify_string_list_reports_precise_schema_error() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-verify-schema-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        progress = completed_progress("P-VERIFY-SCHEMA")
        calls = [
            progress,
            {
                "tool": "run_command",
                "action_id": "A-VERIFY-SCHEMA",
                "operation": "GIT_INSPECT",
                "command": "git -C 'E:\\repo' status --short",
                "success_criteria": "git status exits successfully",
                "verify": ["exit_code == 0", "stdout contains repository state"],
            },
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert not accepted
        assert diagnostics[0]["reason"] == "run_command_verification_contract_required"
        assert "invalid=verify[0]:expected_non_empty_object;actual=str" in diagnostics[0]["detail"]
        assert '"verify":[{"action":"run_command"' in diagnostics[0]["suggestion"]
        assert "不得使用自然語言字串" in diagnostics[0]["suggestion"]


def validate_cmd_syntax_is_rejected_before_execution() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-shell-mismatch-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        progress = completed_progress("P-SHELL-MISMATCH")
        calls = [
            progress,
            {
                "tool": "run_command",
                "action_id": "A-SHELL-MISMATCH",
                "operation": "GIT_INSPECT",
                "command": "cd /d E:\\repo && git status --short",
                "success_criteria": "git status exits successfully",
                "verify": [{
                    "action": "run_command",
                    "command": "git -C 'E:\\repo' rev-parse --is-inside-work-tree",
                    "expect_exit_code": 0,
                    "expect_contains": "true",
                }],
            },
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(calls, {"ack_web_ack_id": ""})
        assert not accepted
        assert diagnostics[0]["reason"] == "run_command_shell_syntax_mismatch"
        assert '"executor":"Windows PowerShell 5.1"' in diagnostics[0]["detail"]
        assert "cmd_cd_d" in diagnostics[0]["detail"]
        assert "cmd_and_operator" in diagnostics[0]["detail"]
        assert "git -C" in diagnostics[0]["suggestion"]


def validate_quoted_shell_operator_is_not_a_false_positive() -> None:
    action = {
        "command": "Write-Output 'literal && value || fallback'",
    }
    assert WebAgentProtocolLoop._run_command_shell_mismatch_detail(action) == ""


def validate_project_source_read_is_routed_before_attachment_execution() -> None:
    with tempfile.TemporaryDirectory(prefix="evidence-to-action-route-") as temp:
        root = Path(temp)
        source = root / "source" / "agent_core"
        source.mkdir(parents=True)
        target = source / "task_plan.py"
        target.write_text("def execute_frozen_task_plan():\n    return True\n", encoding="utf-8")
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.authorized_paths = [str(root)]
        progress = {
            "tool": "report_progress",
            "action_id": "P-ROUTE",
            "base_evaluation": "已定位檔案，缺少 implementation body",
            "total_steps": 2,
            "current_step": 1,
            "steps": [
                {"step": 1, "desc": "讀取實作", "status": "IN_PROGRESS"},
                {"step": 2, "desc": "完成規劃", "status": "PENDING"},
            ],
            "current_focus": "取得 source evidence",
            "next_action": "讀取 task_plan.py",
            "runtime_state": "PROCESSING",
            "completion_contract": {
                "success": ["完成規劃"],
                "failure": ["source evidence 無法取得"],
                "in_progress": ["尚未取得 implementation body"],
                "interrupted": ["Runtime 無法存取授權路徑"],
            },
            "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "尚未取得 implementation body",
            "evidence_refs": ["REQUEST_ACCEPTED", "RUNTIME_STATUS"],
            "decision_reason": "需要 bounded source evidence",
        }
        accepted, diagnostics = loop._accept_ack(
            [
                progress,
                {"tool": "read_file", "action_id": "A-READ", "path": str(target)},
                {"tool": "turn_commit", "action_count": 2},
            ],
            {"ack_web_ack_id": ""},
        )
        assert accepted == []
        assert diagnostics[0]["reason"] == "web_planner_project_read_requires_query_project"
        assert '"operation":"read_range"' in diagnostics[0]["suggestion"]
        assert '"path":"source/agent_core/task_plan.py"' in diagnostics[0]["suggestion"]
        assert "upload_file" in diagnostics[0]["suggestion"]


def validate_declared_next_action_must_be_emitted() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-action-consistency-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.authorized_paths = [str(root)]
        loop.run_id = "RR-PROGRESS-ACTION-CONSISTENCY"
        loop.task_id = "TASK-PROGRESS-ACTION-CONSISTENCY"
        loop.task_epoch = "EPOCH-PROGRESS-ACTION-CONSISTENCY"
        loop.intent_digest = "b" * 64

        def progress(action_id: str, next_action: str) -> dict:
            return {
                "tool": "report_progress", "action_id": action_id,
                "base_evaluation": "Source evidence is available.",
                "total_steps": 2, "current_step": 1,
                "steps": [
                    {"step": 1, "desc": "Inspect source", "status": "COMPLETED"},
                    {"step": 2, "desc": "Continue work", "status": "IN_PROGRESS"},
                ],
                "current_focus": "Continue with the declared tool",
                "next_action": next_action,
                "completion_contract": {
                    "success": ["Work completed"],
                    "failure": ["Action failed"],
                    "in_progress": ["Action has not run"],
                    "interrupted": ["Runtime cannot continue"],
                },
                "decision": "CONTINUE", "outcome": "PENDING",
                "matched_condition": "Action has not run",
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "The next action still needs to execute.",
            }

        missing, diagnostics = loop._accept_ack(
            [
                progress("P-MISSING-QUERY", "query_project：讀取剩餘 source evidence"),
                {"tool": "turn_commit", "action_count": 1},
            ],
            {"ack_web_ack_id": ""},
        )
        assert missing == []
        assert diagnostics[0]["reason"] == "declared_next_action_missing"
        assert diagnostics[0]["declared_next_tools"] == ["query_project"]
        assert diagnostics[0]["actual_operational_tools"] == []

        mismatched, diagnostics = loop._accept_ack(
            [
                progress("P-MISMATCH", "apply_edit_plan：套用已驗證的修改計畫"),
                {"tool": "list_directory", "action_id": "A-WRONG", "path": str(root)},
                {"tool": "turn_commit", "action_count": 2},
            ],
            {"ack_web_ack_id": ""},
        )
        assert mismatched == []
        assert diagnostics[0]["reason"] == "declared_next_action_mismatch"
        assert diagnostics[0]["actual_operational_tools"] == ["list_directory"]

        query = {
            "tool": "query_project", "action_id": "A-QUERY",
            "project_root": str(root),
            "queries": [{"operation": "list_tree", "path": "", "depth": 1}],
        }
        accepted, diagnostics = loop._accept_ack(
            [
                progress("P-MATCH", "query_project：讀取剩餘 source evidence"),
                query,
                {"tool": "turn_commit", "action_count": 2},
            ],
            {"ack_web_ack_id": ""},
        )
        assert diagnostics == []
        assert accepted[1]["tool"] == "query_project"
        # Acceptance only proves action emission.  Execution/result lifecycle
        # has not run yet and must remain independent.
        assert loop.action_ledger == {}


def validate_condition_id_repairs_progress_without_losing_action() -> None:
    with tempfile.TemporaryDirectory(prefix="progress-condition-id-") as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_args: "", progress_root=root)
        loop.run_id = "REQ-CONDITION-ID"
        loop.task_id = "TASK-CONDITION-ID"
        loop.task_epoch = "EPOCH-CONDITION-ID"
        loop.intent_digest = "0" * 64
        contract = {
            "success": ["分析完成"],
            "failure": ["指定目錄無法讀取"],
            "in_progress": [
                "尚未取得指定 working set 證據",
                "尚未確認 not_existing_file.py 狀態",
            ],
            "interrupted": ["Runtime 無法存取指定 working set"],
        }

        def progress(action_id: str, matched: str, *, condition_id: str = "") -> dict:
            payload = {
                "tool": "report_progress", "action_id": action_id,
                "base_evaluation": "需要確認指定檔案是否存在",
                "total_steps": 2, "current_step": 1,
                "steps": [
                    {"step": 1, "desc": "確認檔案", "status": "IN_PROGRESS"},
                    {"step": 2, "desc": "分析關係", "status": "PENDING"},
                ],
                "current_focus": "確認指定檔案",
                "next_action": "list_directory",
                "completion_contract": contract,
                "decision": "CONTINUE", "outcome": "PENDING",
                "matched_condition": matched,
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "尚缺檔案存在性證據",
            }
            if condition_id:
                payload["matched_condition_id"] = condition_id
            return payload

        loop.progress_ledger = record_model_progress(
            loop.task_id,
            progress("P-BASE", "尚未取得指定 working set 證據"),
            request_id=loop.run_id,
            goal="分析兩個指定檔案",
            round_id=1,
            root=root,
        )
        directory_action = {
            "tool": "list_directory", "action_id": "A-LIST-PRESERVED",
            "path": str(root),
        }
        rejected, diagnostics = loop._accept_ack(
            [
                progress("P-MISMATCH", "尚未取得 not_existing_file.py 存在性證據"),
                directory_action,
                {"tool": "turn_commit", "action_count": 2},
            ],
            {"ack_web_ack_id": ""},
        )
        assert rejected == []
        assert diagnostics[0]["reason"] == "invalid_progress_payload"
        assert diagnostics[0]["preserved_action_ids"] == ["A-LIST-PRESERVED"]
        choices = diagnostics[0]["condition_choices"]
        target_id = condition_id_for(
            "in_progress", "尚未確認 not_existing_file.py 狀態",
        )
        assert target_id in {item["condition_id"] for item in choices}

        repaired_calls = loop._restore_progress_repair_actions([
            progress(
                "P-REPAIRED",
                "尚未取得 not_existing_file.py 存在性證據",
                condition_id=target_id,
            ),
            {"tool": "turn_commit", "action_count": 1},
        ])
        assert repaired_calls[-1]["action_count"] == 2
        accepted, diagnostics = loop._accept_ack(
            repaired_calls, {"ack_web_ack_id": ""},
        )
        assert diagnostics == []
        assert [item["tool"] for item in accepted] == [
            "report_progress", "list_directory",
        ]
        repaired_ledger = record_model_progress(
            loop.task_id,
            accepted[0],
            request_id=loop.run_id,
            goal="分析兩個指定檔案",
            round_id=2,
            root=root,
        )
        assert repaired_ledger.matched_condition == "尚未確認 not_existing_file.py 狀態"
        assert repaired_ledger.matched_condition_id == target_id
        assert loop.pending_progress_repair_actions == []


if __name__ == "__main__":
    validate_failed_verification_can_complete()
    validate_repeated_completed_progress_is_paused()
    validate_model_interrupt_is_persisted()
    validate_success_cannot_contradict_failed_runtime()
    validate_unverified_success_requests_and_obtains_evidence()
    validate_verification_gap_rejects_progress_only()
    validate_changed_repair_is_not_a_stall()
    validate_runtime_binds_latest_terminal_evidence()
    validate_complete_step_alias_is_canonicalized()
    validate_terminal_progress_repair_is_explicit()
    validate_rejection_signature_tracks_repair_progress()
    validate_nested_progress_wrapper_is_safely_normalized()
    validate_missing_capability_claim_gets_runtime_guidance()
    validate_nested_progress_capability_recovery_reaches_next_prompt()
    validate_progress_repair_is_condition_specific()
    validate_ambiguous_condition_repair_lists_candidates()
    validate_abnormal_exit_releases_request_ownership()
    validate_verify_string_list_reports_precise_schema_error()
    validate_cmd_syntax_is_rejected_before_execution()
    validate_quoted_shell_operator_is_not_a_false_positive()
    validate_project_source_read_is_routed_before_attachment_execution()
    validate_declared_next_action_must_be_emitted()
    validate_condition_id_repairs_progress_without_losing_action()
    print("WEBAGENT_PROGRESS_TERMINAL_LIFECYCLE_OK")
