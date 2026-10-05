#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression coverage for the runtime-owned v8 request scope prompt."""
from __future__ import annotations

import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import agent_core.request_ownership as ownership


class FakePage:
    url = "https://chatgpt.com/c/request-ownership-v8-test"


class FakeScraper:
    _page = FakePage()


class FakeRegistry:
    waits: list[dict] = []
    advances: list[dict] = []

    def wait(self, cid, rid, owner, interface, **scope):
        self.__class__.waits.append({
            "cid": cid, "rid": rid, "owner": owner,
            "interface": interface, "scope": dict(scope),
        })
        return {
            "token": "before-advance",
            "task_id": scope["task_id"],
            "task_epoch": scope["task_epoch"],
            "intent_digest": scope["intent_digest"],
        }

    def advance(self, cid, rid, owner, token, stage):
        self.__class__.advances.append({
            "cid": cid, "rid": rid, "owner": owner,
            "token": token, "stage": stage,
        })
        return {
            "task_id": "TASK-41",
            "task_epoch": "EPOCH-41",
            "intent_digest": "intent-41",
            "stage": stage,
            "continuation_seq": 7,
        }


def run() -> dict:
    original_registry = ownership.RequestOwnershipRegistry
    ownership.RequestOwnershipRegistry = FakeRegistry
    try:
        expected = {
            "run_id": "RR-TEST-41",
            "task_id": "TASK-41",
            "task_epoch": "EPOCH-41",
            "intent_digest": "intent-41",
        }
        rendered = ownership.guard_web_prompt(
            FakeScraper(),
            "請檢查目前狀態並提出下一個 action。",
            expected,
        )
    finally:
        ownership.RequestOwnershipRegistry = original_registry

    lines = rendered.splitlines()
    assert lines[0] == "[WEBAGENT_ACTIVE_REQUEST]"
    scope = json.loads(lines[1])
    assert tuple(scope) == ownership.REQUEST_SCOPE_FIELDS
    assert scope == {
        "request_id": "RR-TEST-41",
        "task_id": "TASK-41",
        "task_epoch": "EPOCH-41",
        "intent_digest": "intent-41",
        "request_phase": "PLANNER_ROUND_PENDING",
        "continuation_seq": 7,
    }
    assert all(expected[field] == value for field, value in scope.items())
    assert FakeRegistry.waits[0]["scope"] == {
        "task_id": "TASK-41",
        "task_epoch": "EPOCH-41",
        "intent_digest": "intent-41",
    }
    assert FakeRegistry.advances[0]["stage"] == "PLANNER_ROUND_PENDING"

    prompt_text = "\n".join(lines[2:])
    assert "僅供你判斷目前回合" in prompt_text
    assert "不要將其中任何欄位回填到 action、final_response 或 turn_commit" in prompt_text
    assert '{"tool":"turn_commit","action_count":N}' in prompt_text
    assert "不得在 action、final_response 或 turn_commit 輸出 runtime-owned 欄位" in prompt_text
    assert "將以上六個欄位原樣加入" not in prompt_text
    assert rendered.endswith("請檢查目前狀態並提出下一個 action。")

    # Calls without protocol state remain a pure pass-through and do not
    # allocate a request lease.
    assert ownership.guard_web_prompt(FakeScraper(), "plain", {}) == "plain"

    result = {
        "runtime_scope_preserved_in_expected": True,
        "six_field_scope_shown_as_context": True,
        "model_scope_echo_forbidden": True,
        "compact_turn_commit_required": True,
        "no_run_id_passthrough_preserved": True,
    }
    print("REQUEST_OWNERSHIP_V8_PROMPT_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
