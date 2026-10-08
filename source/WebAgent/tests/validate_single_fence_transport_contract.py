#!/usr/bin/env python3
"""Verify every model-facing v9 surface shares one transport contract."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol import WEBAGENT_PROTOCOL_BODY  # noqa: E402
from agent_core.protocol_v9 import (  # noqa: E402
    SINGLE_FENCE_TRANSPORT_CONTRACT,
    SINGLE_FENCE_TRANSPORT_MARKER,
)
from agent_core.recovery_protocol import build_action_replay_prompt  # noqa: E402
from agent_core.smartagent_protocol import SYSTEM_PROMPT_TEMPLATE  # noqa: E402
from agent_core.web_runtime import WebLLMScraper  # noqa: E402


def run() -> dict:
    marker = f"[{SINGLE_FENCE_TRANSPORT_MARKER}]"
    assert marker in SINGLE_FENCE_TRANSPORT_CONTRACT
    assert "exactly one opening ```smartagent_tool fence" in SINGLE_FENCE_TRANSPORT_CONTRACT
    assert "exactly one closing ``` fence" in SINGLE_FENCE_TRANSPORT_CONTRACT
    assert "Do not open a separate fence for each object" in SINGLE_FENCE_TRANSPORT_CONTRACT
    assert WEBAGENT_PROTOCOL_BODY.count(marker) == 1
    assert SYSTEM_PROMPT_TEMPLATE.count(marker) == 1

    replay = build_action_replay_prompt(
        {
            "narrative_recovery_context": {
                "goal": "list one directory",
                "progress": {"current_step": 1, "total_steps": 2},
            }
        }
    )
    assert replay.count(marker) == 1

    diagnostic = {
        "kind": "malformed",
        "transport_kind": "dom_rendered",
        "diagnostics": [
            {"block": 2, "reason": "JSON_DECODE_ERROR", "detail": "line 4 column 1"}
        ],
        "block_map": [
            {"block": 0, "object_count": 1, "tools": ["report_progress"]},
            {"block": 1, "object_count": 1, "tools": ["list_directory"]},
        ],
        "normalizations": ["TERMINAL_FENCE_RESIDUE_REMOVED"],
    }
    repair = WebLLMScraper._protocol_recovery_prompt(
        {"run_id": "RR-TEST", "turn_id": 1},
        recovery_mode="format_repair",
        diagnostic=diagnostic,
    )
    assert repair.count(marker) == 1
    assert "[TRANSPORT_REPAIR_DIAGNOSTIC]" in repair
    assert "No action was executed" in repair
    assert "Correct only the reported transport defect" in repair
    quoted = repair.split("[TRANSPORT_REPAIR_DIAGNOSTIC]\n", 1)[1].split(
        "\n[/TRANSPORT_REPAIR_DIAGNOSTIC]", 1
    )[0]
    parsed = json.loads(quoted)
    assert parsed["kind"] == diagnostic["kind"]
    assert parsed["diagnostics"] == diagnostic["diagnostics"]
    assert parsed["transport_kind"] == diagnostic["transport_kind"]
    assert parsed["block_map"] == diagnostic["block_map"]
    assert parsed["normalizations"] == diagnostic["normalizations"]
    assert parsed["reason"] == ""
    assert parsed["detail"] == ""

    protocol_loop_source = (ROOT / "WebAgent" / "protocol_loop.py").read_text(encoding="utf-8")
    request_ownership_source = (ROOT / "agent_core" / "request_ownership.py").read_text(
        encoding="utf-8"
    )
    web_runtime_source = (ROOT / "agent_core" / "web_runtime.py").read_text(encoding="utf-8")
    assert "SINGLE_FENCE_TRANSPORT_CONTRACT" in protocol_loop_source
    assert "SINGLE_FENCE_TRANSPORT_CONTRACT" in request_ownership_source
    assert 'if protocol_recovery_mode_used == "format_repair"' in web_runtime_source
    assert "diagnostic=recovery_diagnostic" in web_runtime_source

    return {
        "one_shared_contract": True,
        "normal_and_recovery_prompts_covered": True,
        "repair_diagnostic_is_precise_and_bounded": True,
        "repair_preserves_action_semantics": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
