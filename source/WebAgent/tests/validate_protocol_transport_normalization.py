#!/usr/bin/env python3
"""Regression coverage for bounded v9 transport normalization."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.protocol_v8 import (  # noqa: E402
    parse_v8_tool_transport,
    parse_v8_tool_transport_detailed,
)
from agent_core.protocol_v9 import parse_v9_tool_transport_detailed  # noqa: E402
from agent_core.web_runtime import WebLLMScraper  # noqa: E402


def fence(value: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(value) + "\n```"


def run() -> dict:
    progress = {
        "tool": "report_progress",
        "action_id": "A-PROGRESS",
        "current_step": 1,
        "total_steps": 2,
        "current_focus": "list requested directory",
    }
    action = {
        "tool": "list_directory",
        "action_id": "A-LIST",
        "path": r"D:\workspace\SmartAgentv2",
    }
    commit = {"tool": "turn_commit", "action_count": 2}

    # Actual ChatGPT rendered-DOM failure shape: the last language-labelled
    # block exposes an action plus a bare commit, then leaks a closing fence.
    rendered = (
        "smartagent_tool\n" + json.dumps(progress)
        + "\nsmartagent_tool\n" + json.dumps(action)
        + "\n\n" + json.dumps(commit) + "\n```"
    )
    detailed = parse_v8_tool_transport_detailed(rendered)
    assert not detailed.diagnostics
    assert [call["tool"] for call in detailed.calls] == [
        "report_progress", "list_directory", "turn_commit",
    ]
    assert detailed.transport_kind == "dom_rendered"
    assert "TERMINAL_FENCE_RESIDUE_REMOVED" in detailed.normalizations
    assert "DOM_JSON_SEQUENCE_ACCEPTED" in detailed.normalizations
    assert detailed.block_map[-1] == {
        "block": 2,
        "object_count": 2,
        "tools": ["list_directory", "turn_commit"],
    }

    diagnostic = WebLLMScraper._protocol_commit_diagnostic(rendered, {"run_id": "RR-TEST"})
    assert diagnostic["kind"] == "matching"
    assert diagnostic["transport_kind"] == "dom_rendered"
    assert "TERMINAL_FENCE_RESIDUE_REMOVED" in diagnostic["normalizations"]
    assert diagnostic["block_map"][-1]["tools"] == ["list_directory", "turn_commit"]

    # One canonical fence containing a strict JSON sequence remains valid.
    single_fence = (
        "```smartagent_tool\n"
        + "\n".join(json.dumps(item) for item in (progress, action, commit))
        + "\n```"
    )
    calls, errors = parse_v8_tool_transport(single_fence)
    assert not errors and calls[-1] == commit
    assert calls[0]["action_id"] == "A-PROGRESS"
    assert calls[1]["action_id"] == "A-LIST"

    # Only an exact trailing control commit may be adopted outside fences.
    bare_commit = parse_v9_tool_transport_detailed(
        fence(progress) + "\n" + fence(action) + "\n" + json.dumps(commit)
    )
    assert not bare_commit.diagnostics
    assert "TRAILING_BARE_TURN_COMMIT_ADOPTED" in bare_commit.normalizations

    bare_action_errors = parse_v8_tool_transport(
        fence(progress) + "\n" + json.dumps(action)
    )[1]
    assert bare_action_errors[0]["reason"] == "transport_not_exclusive"
    prose_errors = parse_v8_tool_transport(fence(progress) + "\nexplanation")[1]
    assert prose_errors[0]["reason"] == "transport_not_exclusive"

    bad_count = dict(commit, action_count=3)
    errors = parse_v8_tool_transport(
        "smartagent_tool\n" + json.dumps(progress)
        + "\nsmartagent_tool\n" + json.dumps(action)
        + "\n" + json.dumps(bad_count) + "\n```"
    )[1]
    assert errors[0]["reason"] == "COMMIT_ACTION_COUNT_MISMATCH"

    extra_commit_field = {**commit, "action_id": "FORBIDDEN"}
    errors = parse_v8_tool_transport(
        fence(progress) + "\n" + fence(action) + "\n" + fence(extra_commit_field)
    )[1]
    assert errors[0]["reason"] == "V7_OR_RUNTIME_COMMIT_FIELDS_FORBIDDEN"
    missing_commit = parse_v8_tool_transport(fence(progress) + "\n" + fence(action))[1]
    assert missing_commit[0]["reason"] == "missing_turn_commit"

    missing_action_id = dict(progress)
    missing_action_id.pop("action_id")
    errors = parse_v8_tool_transport(
        "```smartagent_tool\n"
        + "\n".join(json.dumps(item) for item in (missing_action_id, action, commit))
        + "\n```"
    )[1]
    assert errors[0]["reason"] == "MISSING_OR_INVALID_FIELD"
    assert errors[0]["detail"] == "action_index=1;tool=report_progress;action_id"

    # v9 owns correlation IDs. A structurally valid compact response that only
    # omitted action_id must not require another model turn; Runtime assigns a
    # stable request-body/index/tool-derived value without changing semantics.
    missing_progress_id = dict(progress)
    missing_progress_id.pop("action_id")
    missing_action_id = dict(action)
    missing_action_id.pop("action_id")
    v9_recovered = parse_v9_tool_transport_detailed(
        "```smartagent_tool\n"
        + "\n".join(json.dumps(item) for item in (missing_progress_id, missing_action_id, commit))
        + "\n```",
        action_id_seed="RR-TEST:1",
    )
    assert not v9_recovered.diagnostics
    assert [item["tool"] for item in v9_recovered.calls] == [
        "report_progress", "list_directory", "turn_commit",
    ]
    assert v9_recovered.calls[0]["action_id"].startswith("V9-RUNTIME-")
    assert v9_recovered.calls[1]["action_id"].startswith("V9-RUNTIME-")
    assert v9_recovered.calls[0]["action_id"] != v9_recovered.calls[1]["action_id"]
    assert sum(
        item.startswith("RUNTIME_ACTION_ID_ASSIGNED:")
        for item in v9_recovered.normalizations
    ) == 2
    repeated_seed = parse_v9_tool_transport_detailed(
        "```smartagent_tool\n"
        + "\n".join(json.dumps(item) for item in (missing_progress_id, missing_action_id, commit))
        + "\n```",
        action_id_seed="RR-TEST:1",
    )
    other_round = parse_v9_tool_transport_detailed(
        "```smartagent_tool\n"
        + "\n".join(json.dumps(item) for item in (missing_progress_id, missing_action_id, commit))
        + "\n```",
        action_id_seed="RR-TEST:2",
    )
    assert repeated_seed.calls[0]["action_id"] == v9_recovered.calls[0]["action_id"]
    assert other_round.calls[0]["action_id"] != v9_recovered.calls[0]["action_id"]

    raw_json_fallback = parse_v9_tool_transport_detailed(json.dumps([
        progress,
        missing_action_id,
        commit,
    ]))
    assert not raw_json_fallback.diagnostics
    assert raw_json_fallback.calls[0]["action_id"].startswith("V9-RUNTIME-")
    assert raw_json_fallback.calls[0]["action_id"] != "A-PROGRESS"
    assert raw_json_fallback.calls[1]["action_id"].startswith("V9-RUNTIME-")

    return {
        "actual_dom_failure_normalized": True,
        "single_fence_sequence_valid": True,
        "bare_action_and_prose_rejected": True,
        "commit_schema_still_strict": True,
        "action_diagnostic_names_ordinal_and_tool": True,
        "v9_missing_action_id_runtime_normalized": True,
        "canonical_existing_action_id_preserved": True,
        "raw_json_fallback_runtime_owned": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
