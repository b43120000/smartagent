#!/usr/bin/env python3
"""Verify that the WebAgent prompt cannot classify turn_commit as an action."""
from __future__ import annotations

from WebAgent.protocol import WEBAGENT_PROTOCOL_BODY


def run() -> dict:
    prompt = WEBAGENT_PROTOCOL_BODY
    assert "turn_commit is a protocol control block, not an action" in prompt
    assert "Never put action_id in turn_commit" in prompt
    assert "only allowed\nturn_commit fields are exactly `tool` and `action_count`" in prompt
    assert "`action_id` is\nexplicitly forbidden" in prompt
    assert '{"tool":"turn_commit","action_count":N}' in prompt
    result = {
        "turn_commit_declared_control_block": True,
        "action_id_explicitly_forbidden": True,
        "compact_shape_preserved": True,
    }
    print("TURN_COMMIT_CONTROL_PROMPT_OK")
    return result


if __name__ == "__main__":
    run()
