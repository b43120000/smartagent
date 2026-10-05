#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline end-to-end test for WebAgent-owned protocol loop."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import WebAgentProtocolLoop


def fence(payload: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def commit(action_count: int) -> dict:
    return {"tool": "turn_commit", "action_count": action_count}


def progress(action_id: str, step: float, *, first: bool = False) -> dict:
    value = {
        "tool": "report_progress", "action_id": action_id,
        "current_step": step, "total_steps": 2,
        "current_focus": "列出並回報目錄" if step < 2 else "完成目錄回報",
        "next_action": "讀取目錄" if step < 2 else "回覆使用者",
        "decision": "CONTINUE" if step < 2 else "COMPLETE",
        "outcome": "PENDING" if step < 2 else "SUCCESS",
        "matched_condition": "目錄內容尚未取得" if step < 2 else "目錄內容已取得並可回報",
        "evidence_refs": ["REQUEST_ACCEPTED"] if step < 2 else ["WA-A-LIST"],
        "decision_reason": "依目前 Runtime evidence 判定是否仍需讀取目錄",
    }
    if first:
        value.update({
            "base_evaluation": "尚未讀取目錄內容",
            "steps": [
                {"step": 1, "desc": "讀取目錄", "status": "IN_PROGRESS"},
                {"step": 2, "desc": "整理並回覆", "status": "PENDING"},
            ],
            "completion_contract": {
                "success": ["目錄內容已取得並可回報"],
                "failure": ["目錄讀取已結束但未取得內容"],
                "in_progress": ["目錄內容尚未取得"],
                "interrupted": ["Runtime 無法繼續讀取目錄"],
            },
        })
    elif step >= 2:
        value["steps"] = [
            {"step": 1, "desc": "讀取目錄", "status": "COMPLETED"},
            {"step": 2, "desc": "整理並回覆", "status": "COMPLETED"},
        ]
    return value


def run() -> dict:
    assert "smart_agent" not in sys.modules
    assert "web_copilot" not in sys.modules
    with tempfile.TemporaryDirectory(prefix="webagent-") as temp:
        root = Path(temp)
        (root / "alpha.txt").write_text("alpha", encoding="utf-8")
        (root / "nested").mkdir()
        turns = []

        def planner(prompt: str, expected: dict, attachments: list[str]) -> str:
            turns.append({"prompt": prompt, "expected": dict(expected), "attachments": attachments})
            if len(turns) == 1:
                action = {"tool": "list_directory", "action_id": "WA-A-LIST", "path": str(root)}
                loop.tools.last_verification_status = "PASS"
                return fence(progress("WA-P-1", 1, first=True)) + "\n" + fence(action) + "\n" + fence(commit(2))
            loop.tools.last_verification_status = None
            if len(turns) == 2:
                assert "alpha.txt" in prompt
                assert "[DIR]  nested" in prompt
            final = {"tool": "final_response", "action_id": "WA-A-FINAL", "content": "目錄包含 alpha.txt 與 nested 子目錄。"}
            return fence(progress("WA-P-2", 2)) + "\n" + fence(final) + "\n" + fence(commit(2))

        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        result = loop.run(f"webcopilot list 出這裡有哪些檔案 {root}")
        assert result == "目錄包含 alpha.txt 與 nested 子目錄。"
        assert len(turns) == 2
        assert turns[1]["expected"]["ack_result_id"].startswith("RES-")
        assert turns[1]["expected"]["ack_web_ack_id"].startswith("WEBACK-V8-")
        assert f"request_id={loop.run_id}" in turns[0]["prompt"]
        assert "round=1" in turns[0]["prompt"]
        assert "attempt=1" in turns[0]["prompt"]
        assert "previous_ack_id=WEBACK-V8-" in turns[1]["prompt"]
        assert '"current_step":1' in turns[1]["prompt"]
        assert "[RUNTIME_EVIDENCE]" in turns[1]["prompt"]
        assert "WA-A-LIST" in turns[1]["prompt"]
        assert "smart_agent" not in sys.modules
        assert "web_copilot" not in sys.modules
        return {
            "standalone_loop": True,
            "planner_turns": len(turns),
            "list_directory_executed": True,
            "ack_chain_verified": True,
            "imports_smart_agent": False,
            "imports_web_copilot": False,
            "final_response": result,
        }


if __name__ == "__main__":
    outcome = run()
    print("WEBAGENT_STANDALONE_LIST_E2E_OK")
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
