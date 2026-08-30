#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline end-to-end test for WebAgent-owned protocol loop."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from WebAgent.protocol_loop import WebAgentProtocolLoop


def fence(payload: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def commit(expected: dict, web_ack_id: str, action_count: int) -> dict:
    return {
        "tool": "turn_commit",
        "run_id": expected["run_id"],
        "turn_id": expected["turn_id"],
        "ack_local_nonce": expected["local_nonce"],
        "ack_result_id": expected["ack_result_id"],
        "ack_web_ack_id": expected["ack_web_ack_id"],
        "web_ack_id": web_ack_id,
        "action_count": action_count,
    }


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
                return fence(action) + "\n" + fence(commit(expected, "WEBACK-WA-1", 1))
            assert "alpha.txt" in prompt
            assert "[DIR]  nested" in prompt
            final = {"tool": "final_response", "action_id": "WA-A-FINAL", "content": "目錄包含 alpha.txt 與 nested 子目錄。"}
            return fence(final) + "\n" + fence(commit(expected, "WEBACK-WA-2", 1))

        loop = WebAgentProtocolLoop(root, planner)
        result = loop.run(f"webcopilot list 出這裡有哪些檔案 {root}")
        assert result == "目錄包含 alpha.txt 與 nested 子目錄。"
        assert len(turns) == 2
        assert turns[1]["expected"]["ack_result_id"].startswith("RES-")
        assert turns[1]["expected"]["ack_web_ack_id"] == "WEBACK-WA-1"
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
