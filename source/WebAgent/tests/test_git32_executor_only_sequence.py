"""Git32 compatibility tests after Git33 retires task-specific sequence control.

Do not test an inactive hard-coded six-step state machine: the supported
Executor-only route is now the tool-agnostic PokeClaw loop, with the security
tool admission and request path parser preserved.
"""
from __future__ import annotations

from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.protocol_loop import extract_authorized_paths
from agent_core.executor_run_blackboard import read_executor_run
from WebAgent.tests.test_git33_pokeclaw_executor_loop import (
    REQUEST, TARGET, _loop,
)
from WebAgent.tests.test_minimal_core_v10 import tool_json


def test_git32_exact_user_path_still_bound_without_following_task_prose():
    assert extract_authorized_paths(REQUEST) == [TARGET]


def test_git32_retired_sequence_not_invoked_by_active_pokeclaw_loop(
    tmp_path, monkeypatch,
):
    def obsolete(*args, **kwargs):
        raise AssertionError("Retired task-specific sequence gate must never run")
    monkeypatch.setattr(
        "WebAgent.executor_only_validation.directory_sequence_contract", obsolete
    )
    loop, state = _loop(tmp_path, monkeypatch, [
        tool_json("observe_fact", path=TARGET),
        '{"type":"finish","summary":"已確認不存在"}',
    ])
    reply = run_minimal_core(loop, REQUEST)
    assert state["mutations"] == 0
    assert loop.terminal_outcome == "UNKNOWN"
    assert "POKECLAW_EXECUTOR_LOOP_V1" in loop.prompts[0]
    assert "已確認不存在" in reply
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert board["executor_state"] == "MODEL_REPORTED_COMPLETE_UNVERIFIED"
    assert board["goal_driver_state"] == "DISABLED"
    assert board["prerequisite_state"] == "DISABLED"
