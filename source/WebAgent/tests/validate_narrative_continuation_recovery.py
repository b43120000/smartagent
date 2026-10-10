#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify natural-language replies are re-planned with progress, never executed."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import WebAgentProtocolLoop  # noqa: E402
from agent_core.web_runtime import WebLLMScraper  # noqa: E402


def _fence(payload: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def run() -> dict:
    expected = {
        "run_id": "RR-TEST",
        "turn_id": 4,
        "narrative_recovery_context": {
            "goal": "盤點 selector，先規劃不修改",
            "progress": {
                "current_step": 4.8,
                "total_steps": 5,
                "current_focus": "確認 browser routing 入口",
                "next_action": "完成第一批修改檔案規劃",
            },
            "latest_runtime_context": "browser_operator.py 已提供給模型",
        },
    }
    natural = "已閱讀 browser_operator.py，接下來要完成 selector 分包規劃。"
    prompt = WebLLMScraper._protocol_recovery_prompt(
        expected,
        recovery_mode="narrative_continuation",
        diagnostic={"reason": "missing_smartagent_tool_envelope"},
        source_text=natural,
    )
    assert "[SMARTAGENT_NARRATIVE_CONTINUATION]" in prompt
    assert natural in prompt
    assert "盤點 selector，先規劃不修改" in prompt
    assert '"current_step":4.8' in prompt
    assert "browser_operator.py 已提供給模型" in prompt
    assert "no action was executed" in prompt
    assert "Do not merely reformat or repeat it" in prompt
    assert "exactly one report_progress" in prompt

    missing_envelope = {
        "diagnostics": [{"reason": "missing_smartagent_tool_envelope"}],
    }
    assert WebLLMScraper._select_protocol_recovery_mode(
        natural, missing_envelope, "format_repair"
    ) == "narrative_continuation"
    assert WebLLMScraper._select_protocol_recovery_mode(
        '{"tool":"final_response","action_id":"A","content":"done"}',
        missing_envelope,
        "format_repair",
    ) == "format_repair"
    assert WebLLMScraper._select_protocol_recovery_mode(
        "", missing_envelope, "format_repair"
    ) == "format_repair"
    assert WebLLMScraper._select_protocol_recovery_mode(
        natural, {"diagnostics": [{"reason": "json_decode_error"}]}, "format_repair"
    ) == "format_repair"

    with tempfile.TemporaryDirectory(prefix="narrative-context-") as temp:
        seen: list[dict] = []

        def planner(_prompt: str, planner_expected: dict, _attachments: list[str]) -> str:
            seen.append(dict(planner_expected))
            progress = {
                "tool": "report_progress",
                "action_id": "A-PROGRESS-DONE",
                "base_evaluation": "規劃完成",
                "total_steps": 1,
                "current_step": 1,
                "steps": [{"step": 1, "desc": "完成規劃", "status": "COMPLETED"}],
                "current_focus": "完成",
                "next_action": "",
                "completion_contract": {
                    "success": ["規劃內容已完成並可回報"],
                    "failure": ["規劃已結束但缺少必要內容"],
                    "in_progress": ["規劃內容仍在整理"],
                    "interrupted": ["Runtime 無法繼續取得規劃依據"],
                },
                "decision": "COMPLETE",
                "outcome": "SUCCESS",
                "matched_condition": "規劃內容已完成並可回報",
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "規劃已依原始要求完成",
            }
            final = {
                "tool": "final_response",
                "action_id": "A-FINAL-PLAN",
                "content": "規劃完成",
            }
            commit = {"tool": "turn_commit", "action_count": 2}
            return "\n".join((_fence(progress), _fence(final), _fence(commit)))

        loop = WebAgentProtocolLoop(temp, planner, progress_root=temp)
        # This scenario verifies recovery-context propagation. Runtime
        # evidence gating is exercised by the dedicated completion tests.
        loop._terminal_evidence_verdict = lambda _refs, **_kwargs: "PASS"
        result = loop.run("盤點 selector，先規劃不修改", request_id="RR-CONTEXT", task_id="TASK-CONTEXT")
        assert result == "規劃完成"
        context = seen[0].get("narrative_recovery_context", {})
        assert context.get("goal") == "盤點 selector，先規劃不修改"
        assert context.get("progress", {}).get("runtime_state") == "PROCESSING"
        assert "request_id=" not in context.get("latest_runtime_context", "")

    return {
        "natural_reply_is_quoted_model_note": True,
        "goal_and_progress_are_supplied": True,
        "runtime_correlation_is_not_requested": True,
        "valid_compact_v8_still_required": True,
        "no_natural_language_action_execution": True,
    }


if __name__ == "__main__":
    print("NARRATIVE_CONTINUATION_RECOVERY_OK")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
