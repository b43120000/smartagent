"""Git51: WebDirect Minimal Core final chat presentation regression."""
from __future__ import annotations

import json

from WebAgent.controller import (
    build_final_render_prompt,
    minimal_final_presentation_valid,
)


TASK_ID = "TASK-GIT51"
REQUEST_ID = "RR-GIT51"


def _answer(state="COMPLETED", acceptance="PASS"):
    return (
        f"狀態：🟢 {state}\n\n"
        "使用者原始需求：\n列出 E:\\Sample 第一層內容。\n\n"
        "Base：\n已存在的資料夾。\n\n"
        f"驗收結果：{acceptance}\n\n"
        "任務結果：\n已列出第一層，未遞迴。\n\n"
        "調整需求：無\n\n"
        f"task_id: {TASK_ID}\nrequest_id: {REQUEST_ID}"
    )


def _check(text, state="COMPLETED", outcome="SUCCESS"):
    return minimal_final_presentation_valid(
        text, task_id=TASK_ID, request_id=REQUEST_ID,
        state=state, outcome=outcome,
    )


def test_minimal_prompt_uses_runtime_reply_and_no_legacy_ack():
    prompt = build_final_render_prompt(
        "已列出第一層，未遞迴。",
        original_request="列出 E:\\Sample 第一層內容。",
        base_evaluation="已存在的資料夾。",
        task_id=TASK_ID, request_id=REQUEST_ID,
        state="COMPLETED", outcome="SUCCESS",
        terminal_acceptance={}, minimal_core=True,
    )
    assert prompt.startswith("[WEBAGENT_MINIMAL_FINAL_PRESENTATION_V1]")
    assert "[WEBAGENT_ACK]" not in prompt
    assert "不是 Planner 的普通任務回合" in prompt
    assert "不可以新增工具" not in prompt  # Only explicitly forbidden tool calls
    data = json.loads(prompt.split("payload=", 1)[1].split(
        "\n[/WEBAGENT_MINIMAL_FINAL_PRESENTATION_V1]", 1)[0])
    assert data["state"] == "COMPLETED"
    assert data["outcome"] == "SUCCESS"
    assert data["task_id"] == TASK_ID
    assert data["request_id"] == REQUEST_ID
    assert "使用者原始需求" in data["display_content"]
    assert "Base：" in data["display_content"]
    assert "結果／中斷原因" in data["display_content"]
    assert "驗收結果：PASS" in data["display_content"]
    assert "task_id: " + TASK_ID in data["display_content"]


def test_legacy_render_prompt_unchanged_by_default():
    prompt = build_final_render_prompt("完成", request_id=REQUEST_ID, round_count=2, ack_id="WEBACK-1")
    assert prompt.startswith("[WEBAGENT_FINAL_RENDER]")
    assert "[WEBAGENT_ACK]" in prompt


def test_minimal_success_visible_reply_is_accepted():
    assert _check(_answer())


def test_paused_must_remain_paused_and_unverified():
    paused = _answer("PAUSED", "尚未驗證")
    assert _check(paused, state="PAUSED", outcome="UNKNOWN")
    assert not _check(_answer("COMPLETED", "PASS"), state="PAUSED", outcome="UNKNOWN")
    assert not _check(_answer("PAUSED", "PASS"), state="PAUSED", outcome="UNKNOWN")


def test_terminal_failed_cannot_be_presented_as_pass():
    assert _check(_answer("FAILED", "FAIL"), state="FAILED", outcome="FAILED")
    assert not _check(_answer("FAILED", "PASS"), state="FAILED", outcome="FAILED")
    assert not _check(_answer("COMPLETED", "FAIL"), state="COMPLETED", outcome="SUCCESS")


def test_missing_fields_or_transport_json_are_rejected():
    assert not _check('{"type":"finish","summary":"完成"}')
    assert not _check(_answer().replace("調整需求：無", "下一步：無"))
    assert not _check(_answer().replace("request_id: " + REQUEST_ID, "request_id: RR-OTHER"))
    assert not _check(_answer().replace("驗收結果：PASS", "驗收結果：FAIL"))
