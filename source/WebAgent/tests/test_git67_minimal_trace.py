"""IT10 Git67 Minimal Executor boundary trace: real parser and admission, fake ToolResult.

No network, filesystem mutation, Telegram account, or ChatGPT call.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_core import path_integrity_trace as PIT
from agent_core.it10_raw_collector import collect
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
from WebAgent.tests.test_minimal_core_v10 import tool_json


PATH = r"E:\AIspace\.case_d_backups"
REQUEST = (
    "NLIT_CASE_ID=IT10\n"
    "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
    "IT10_SUBCASE_ID=GIT67-MINIMAL-REAL-PARSER-001\n"
    "受控的唯讀字元保真驗證，只允許 observe_fact\n"
    + PATH + "\n"
)


def test_real_minimal_planner_parser_admission_and_dispatch_boundaries(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    env = {
        "SMARTAGENT_IT10_PATH_TRACE": "1",
        "SMARTAGENT_IT10_PATH_TRACE_RAW": "YES",
        "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
        "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "GIT67-MINIMAL-TRACE-TEST",
        "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(private),
    }
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    model_reply = tool_json("observe_fact", path=PATH)
    loop, state = _loop(
        tmp_path, monkeypatch,
        [model_reply, '{"type":"blocked","reason":"offline test complete"}'],
        request=REQUEST,
    )
    # The legacy FakeLoop fixture caps all prompts at 24k characters;
    # these extra IT10 markers exceed that fixture-only bound by ~100 chars.
    # Keep exactly the fixture's reply iterator, without changing production
    # prompt length, decision grammar or Runtime semantics.
    def fixture_planner_without_legacy_size_cap(prompt, expected, attachments):
        loop.expected_protocols.append(expected)
        loop.prompts.append(prompt)
        return next(loop.replies)
    loop.planner = fixture_planner_without_legacy_size_cap
    PIT.begin(REQUEST, task_id=loop.task_id, request_id=loop.run_id, transport="LOCAL_TEST")
    run_minimal_core(loop, REQUEST)
    assert state["mutations"] == 0
    assert len(loop.executed) == 1
    assert loop.executed[0]["path"] == PATH

    rows = [json.loads(line) for source in private.glob("it10-path-*.jsonl")
            for line in source.read_text(encoding="utf-8").splitlines()]
    stages = {row["stage"] for row in rows}
    expected = (
        "08_minimal_prompt", "11_minimal_planner_response",
        "11_minimal_parser_input", "12_minimal_decision",
        "13_minimal_admission_input", "13_minimal_admission_output",
        "13_minimal_pre_dispatch_action",
    )
    assert set(expected).issubset(stages)
    def raw(stage):
        row = next(row for row in rows if row["stage"] == stage and row["raw_blob_status"] == "PERSISTED")
        blob = (private / row["raw_blob_ref"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == row["raw_blob_sha256"]
        return blob
    assert raw("11_minimal_planner_response") == model_reply.encode("utf-8")
    assert raw("11_minimal_parser_input") == raw("11_minimal_planner_response")
    for stage in ("12_minimal_decision", "13_minimal_admission_input",
                  "13_minimal_admission_output", "13_minimal_pre_dispatch_action"):
        assert PATH in raw(stage).decode("utf-8")
    stage = tmp_path / "collector_stage"
    stage.mkdir()
    index = collect(loop.task_id, loop.run_id, private, stage)
    assert index["execution_path"] == "MINIMAL_EXECUTOR"
    assert index["raw_blob_count"] >= len(expected)
    assert index["missing_refs"] == []
    assert index["status"] == "PARTIAL"  # Actual Telegram/browser stages unobserved.
