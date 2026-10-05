#!/usr/bin/env python3
"""Focused contract checks for the three-mode v9 recovery gate."""
from __future__ import annotations

import inspect

from agent_core.recovery_protocol import (
    ACTION_EXECUTION_MODE,
    INITIALIZATION_CAPABILITY_MODE,
    RECOVERY_FIELD_RECONSTRUCTION_MODE,
    build_action_replay_prompt,
    build_context_rebase_prompt,
    is_matching_rebase_ready,
)
from agent_core.web_runtime import WebLLMScraper
from WebAgent.protocol import WEBAGENT_PROTOCOL_BODY
from WebAgent.protocol_loop import WebAgentProtocolLoop


def run() -> dict:
    expected = {
        "run_id": "RR-THREE-MODE",
        "turn_id": 4,
        "narrative_recovery_context": {
            "goal": "列出 C:/workspace 的檔案數量",
            "progress": {
                "current_step": 1,
                "total_steps": 2,
                "steps": [
                    {"step": 1, "desc": "list files", "status": "COMPLETED"},
                    {"step": 2, "desc": "report count", "status": "IN_PROGRESS"},
                ],
                "evidence_refs": ["A-LIST-1"],
            },
            "latest_runtime_context": "A-LIST-1 returned 12 files",
            "authorized_paths": ["C:/workspace"],
        },
    }
    rebase_prompt, token = build_context_rebase_prompt(expected, "我看到一些檔案。")
    assert "quarantine" in rebase_prompt.lower()
    assert "Do not solve" in rebase_prompt
    assert is_matching_rebase_ready(f"[SMARTAGENT_REBASE_READY] {token}", token)
    assert not is_matching_rebase_ready("OK", token)
    assert not is_matching_rebase_ready(
        f"收到\n[SMARTAGENT_REBASE_READY] {token}", token
    )

    replay = build_action_replay_prompt(expected)
    assert f"[SMARTAGENT_MODE] {ACTION_EXECUTION_MODE}" in replay
    assert "A-LIST-1 returned 12 files" in replay
    assert "do not repeat" in replay.lower()
    assert "RECOVERY_FIELD_RECONSTRUCTION_MODE" not in replay
    assert "NARRATIVE_BRIDGE" not in replay

    assert f"[SMARTAGENT_MODE] {INITIALIZATION_CAPABILITY_MODE}" in WEBAGENT_PROTOCOL_BODY
    assert "bounded, one-slot" not in WEBAGENT_PROTOCOL_BODY
    assert "FIELD_REPAIR" not in WEBAGENT_PROTOCOL_BODY
    loop_source = inspect.getsource(WebAgentProtocolLoop._with_commit)
    assert "ACTION_EXECUTION_MODE" in loop_source
    runtime_source = inspect.getsource(WebLLMScraper._wait_for_response_complete)
    assert 'context_rebase_phase = "awaiting_ready"' in runtime_source
    assert 'context_rebase_phase = "awaiting_replay"' in runtime_source
    assert 'context_rebase_phase = "failed_to_mode3"' in runtime_source
    assert 'and not context_rebase_phase' in runtime_source
    assert 'protocol_recovery_mode_used == "narrative_continuation"' in runtime_source

    print("THREE_MODE_RECOVERY_OK")
    return {
        "initialization_mode_explicit": True,
        "normal_action_mode_explicit": True,
        "normal_prompts_hide_mode3": True,
        "exact_rebase_handshake": True,
        "runtime_state_replayed_without_completed_action_duplication": True,
        "second_invalid_response_enters_mode3": True,
        "recovery_mode": RECOVERY_FIELD_RECONSTRUCTION_MODE,
    }


if __name__ == "__main__":
    run()
