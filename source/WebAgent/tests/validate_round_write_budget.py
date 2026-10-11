"""Whole-round admission must reject oversized content before any execution."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core.chunked_write import ChunkedWriteManager, ChunkedWriteError
from agent_core.smartagent_protocol import validate_tool_envelope, SYSTEM_PROMPT_TEMPLATE
from agent_core.write_budget import validate_round_write_budget, write_budget_contract
from agent_core.recovery_protocol import build_action_replay_prompt
from WebAgent.protocol import render_initial_planner_toolkit, render_action_response_constraints, WEBAGENT_PROTOCOL_BODY
from WebAgent.protocol_loop import WebAgentProtocolLoop


def chunk(action_id, content):
    return {"tool": "write_file_chunk", "action_id": action_id, "write_id": "WRITE", "content": content}


def run_enabled():
    assert validate_round_write_budget([chunk("a", "B" * 1000), chunk("b", "B" * 1000)]) is None
    assert validate_round_write_budget([chunk("a", "圖" * 1000), chunk("b", "B" * 1000)]) is None
    bad = validate_round_write_budget([chunk("a", "B" * 1000), chunk("b", "B" * 1000), chunk("c", "B")])
    assert bad["reason"] == "round_write_budget_exceeded" and not bad["action_executed"]
    assert json.loads(bad["detail"])["actual_chars"] == 2001
    assert validate_round_write_budget([{"tool": "report_progress", "content": "B" * 10000}]) is None
    assert validate_round_write_budget([{"tool": "write_file", "content": "B" * 2001}]) is not None
    valid, error = validate_tool_envelope(chunk("a", "B" * 1001))
    assert not valid and error["reason"] == "chunk_content_too_large"
    valid, _ = validate_tool_envelope({"tool": "write_file", "action_id": "w", "path": r"E:\file.txt", "content": "B" * 4096})
    assert valid  # Tool limit retained; whole-round admission imposes the lower budget.

    for prompt in (render_initial_planner_toolkit(), render_action_response_constraints(),
                   build_action_replay_prompt({})):
        assert "[SMARTAGENT_WRITE_BUDGET]" in prompt
        assert '"round_write_max_chars":2000' in prompt

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        loop = WebAgentProtocolLoop(root, lambda *_: "")
        loop.run_id = "REQ"
        # A rejected batch must not even normalize/persist progress or admit siblings.
        loop._normalize_report_progress_envelope = lambda *_: (_ for _ in ()).throw(AssertionError("progress changed"))
        calls = [{"tool": "report_progress"}, {"tool": "list_directory", "path": str(root)},
                 chunk("a", "B" * 1000), chunk("b", "B" * 1000), chunk("c", "B"),
                 {"tool": "turn_commit", "action_count": 5}]
        actions, diagnostics = loop._accept_ack(calls, {})
        assert actions == [] and diagnostics[0]["reason"] == "round_write_budget_exceeded"
        assert loop.v8_admitted_actions == {} and loop.progress_ledger is None

        manager = ChunkedWriteManager(root / "state", [root])
        loop.tools._chunked_write_manager = manager
        output = root / "probe.txt"
        content = "A" * 1000 + "B" * 1000 + "C" * 1000
        manager.begin({"action_id": "begin", "write_id": "WRITE", "path": str(output), "encoding": "utf-8",
                       "overwrite": False, "expected_size": 3000, "chunk_count": 3,
                       "expected_sha256": hashlib.sha256(content.encode()).hexdigest()}, request_id="REQ", run_id="REQ")
        first_round = [chunk("a", "A" * 1000), chunk("b", "B" * 1000)]
        assert validate_round_write_budget(first_round) is None
        for action in first_round:
            manager.write_chunk(action)
        assert not output.exists()
        continuation = json.loads(loop._write_continuation_context().split("\n")[1])
        session = continuation["open_transactions"][0]
        assert session["next_offset"] == 2000 and session["remaining_bytes"] == 1000
        assert manager.open_sessions(request_id="OTHER", run_id="OTHER") == []
        try:
            manager.write_chunk(chunk("oversized", "D" * 1001))
        except ChunkedWriteError as error:
            assert str(error) == "chunk_content_too_large"
        else:
            raise AssertionError("oversized chunk accepted")
        assert manager.open_sessions(request_id="REQ", run_id="REQ")[0]["next_offset"] == 2000
        # A fresh manager demonstrates persisted state survives the next round.
        manager = ChunkedWriteManager(root / "state", [root])
        manager.write_chunk(chunk("c", "C" * 1000))
        manager.commit({"action_id": "commit", "write_id": "WRITE"})
        assert output.read_bytes() == content.encode()
        assert manager.open_sessions(request_id="REQ", run_id="REQ") == []
        # Pre-budget durable state: 1500 accepted bytes with only two original chunks.
        legacy_output = root / "legacy.txt"
        legacy = {"action_id": "legacy-begin", "write_id": "LEGACY", "path": str(legacy_output),
                  "encoding": "utf-8", "overwrite": False, "expected_size": 3000,
                  "chunk_count": 2, "expected_sha256": hashlib.sha256(b"L" * 3000).hexdigest()}
        manager.begin(legacy, request_id="REQ", run_id="REQ")
        with patch("agent_core.chunked_write.CHUNK_CONTENT_MAX_CHARS", 3000):
            manager.write_chunk({"action_id": "legacy-first", "write_id": "LEGACY", "content": "L" * 1500})
        manifest = manager._load_manifest("LEGACY")
        manifest.pop("write_budget_version")
        manager._save_manifest(manifest)
        manager = ChunkedWriteManager(root / "state", [root])
        manager.write_chunk({"action_id": "legacy-second", "write_id": "LEGACY", "content": "L" * 1000})
        try:
            manager.commit({"action_id": "legacy-incomplete", "write_id": "LEGACY"})
        except ChunkedWriteError as error:
            assert str(error) == "assembled_size_mismatch"
        else:
            raise AssertionError("legacy incomplete data committed")
        assert not legacy_output.exists()
        manager.write_chunk({"action_id": "legacy-third", "write_id": "LEGACY", "content": "L" * 500})
        manager.commit({"action_id": "legacy-commit", "write_id": "LEGACY"})
        assert legacy_output.read_bytes() == b"L" * 3000
    print("ROUND_WRITE_BUDGET_ENABLED_PASS")


def main():
    # Default state must stop advertising/enforcing the temporary limits.
    assert not write_budget_contract()["extra_limits_enabled"]
    assert write_budget_contract()["chunk_max_chars"] is None
    assert write_budget_contract()["round_write_max_chars"] is None
    assert validate_round_write_budget([chunk("large", "B" * 5000)]) is None
    valid, error = validate_tool_envelope(chunk("large", "B" * 1500))
    assert valid, error
    valid, error = validate_tool_envelope({"tool": "write_file", "action_id": "large", "path": r"E:\file.txt", "content": "B" * 4097})
    assert not valid and error["reason"] == "write_file_content_too_large"
    for prompt in (render_initial_planner_toolkit(), render_action_response_constraints(), SYSTEM_PROMPT_TEMPLATE, WEBAGENT_PROTOCOL_BODY):
        assert '"chunk_max_chars":null' in prompt
        assert '"round_write_max_chars":null' in prompt
    with patch("agent_core.write_budget.WRITE_BUDGET_LIMITS_ENABLED", True):
        run_enabled()
    print("ROUND_WRITE_BUDGET_DISABLED_PASS")


if __name__ == "__main__":
    main()
