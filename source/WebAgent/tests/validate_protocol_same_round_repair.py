#!/usr/bin/env python3
"""A malformed model reply gets one repair in the same logical round."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import ProtocolLoopInterrupted, WebAgentProtocolLoop
from agent_core.protocol_v8 import parse_v8_tool_transport, recover_explicit_json_transport


def fence(value: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(value) + "\n```"


def run() -> None:
    explicit = '{"tool":"final_response","action_id":"A-JSON","content":"done"}'
    wrapped = recover_explicit_json_transport(explicit)
    assert wrapped is not None
    calls, errors = parse_v8_tool_transport(wrapped)
    assert not errors and calls[-1] == {"tool": "turn_commit", "action_count": 1}
    assert recover_explicit_json_transport("Explanation: " + explicit) is None
    assert recover_explicit_json_transport('{"tool":"final_response","content":"done"}') is None
    assert recover_explicit_json_transport('{"tool":"final_response","action_id":"A","content":"x","content":"y"}') is None
    with tempfile.TemporaryDirectory(prefix="webagent-repair-") as temp:
        seen = []

        def repaired(prompt: str, expected: dict, attachments: list[str]) -> str:
            seen.append((prompt, dict(expected), list(attachments)))
            if len(seen) == 1:
                return "The answer is complete, but the envelope is missing."
            assert "attempt=2" in prompt
            assert "missing_smartagent_tool_envelope" in prompt
            return fence({"tool": "final_response", "action_id": "A-FINAL", "content": "done"}) + "\n" + fence(
                {"tool": "turn_commit", "action_count": 1}
            )

        loop = WebAgentProtocolLoop(temp, repaired)
        assert loop.run("Say done") == "done"
        assert len(seen) == 2
        assert seen[0][1] == seen[1][1]
        assert seen[0][1]["turn_id"] == 1
        assert seen[0][1]["local_nonce"] == seen[1][1]["local_nonce"]

        seen.clear()

        def json_only(prompt: str, expected: dict, attachments: list[str]) -> str:
            seen.append(dict(expected))
            return "not a protocol response" if len(seen) == 1 else explicit

        loop = WebAgentProtocolLoop(temp, json_only)
        assert loop.run("Say done") == "done"
        assert len(seen) == 2 and seen[0] == seen[1]

        seen.clear()

        def always_bad(prompt: str, expected: dict, attachments: list[str]) -> str:
            seen.append(dict(expected))
            return "not a protocol response"

        loop = WebAgentProtocolLoop(temp, always_bad)
        try:
            loop.run("Say done")
        except ProtocolLoopInterrupted as exc:
            assert exc.round_id == 1
            assert exc.first_diagnostics[0]["reason"] == "missing_smartagent_tool_envelope"
            assert exc.final_diagnostics[0]["reason"] == "missing_smartagent_tool_envelope"
        else:
            raise AssertionError("twice-malformed reply must interrupt")
        assert len(seen) == 2
        assert seen[0] == seen[1]
        assert loop.action_ledger == {}
        assert loop.turn_id == 1

        def mixed_final(prompt: str, expected: dict, attachments: list[str]) -> str:
            return (
                fence({"tool": "list_directory", "action_id": "A-LIST", "path": temp})
                + "\n" + fence({"tool": "final_response", "action_id": "A-FINAL", "content": "done"})
                + "\n" + fence({"tool": "turn_commit", "action_count": 2})
            )

        loop = WebAgentProtocolLoop(temp, mixed_final)
        try:
            loop.run("Say done")
        except ProtocolLoopInterrupted as exc:
            assert exc.final_diagnostics[0]["reason"] == "final_response_must_be_exclusive"
        else:
            raise AssertionError("mixed final_response must be rejected")
        assert loop.action_ledger == {}


if __name__ == "__main__":
    run()
    print("WEBAGENT_SAME_ROUND_REPAIR_OK")
