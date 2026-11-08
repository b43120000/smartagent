"""Minimal Core v10: offline parser, admission and PokeClaw-style loop tests."""
from __future__ import annotations

import json
import pytest

from WebAgent.minimal_decision import MinimalDecision, MinimalDecisionError, parse_minimal_decision
from WebAgent.minimal_admission import admit_minimal_action, action_is_mutating
from WebAgent.minimal_core_loop import run_minimal_core


class FakeTools:
    interface_name = "webdirect"

    def __init__(self, root, request):
        self.workspace_root = root
        self.current_request_text = request
        self._protocol_turn_seq = 0

    def web_file_capabilities(self):
        return {"browser_upload": False, "browser_download": False}

    def preview_tool_scope(self, action):
        return {"allowed": True}

    def take_pending_attachments(self):
        return []


class FakeLoop:
    def __init__(self, root, goal, replies, result):
        self.workspace = self.progress_root = root
        self.task_id = "TASK-MINIMAL-TEST"
        self.task_epoch = "epoch"
        self.run_id = "RR-MINIMAL-TEST"
        self.max_turns = 8
        self.tools = FakeTools(root, goal)
        self.replies = iter(replies)
        self.result = result
        self.executed = []
        self.expected_protocols = []
        self.terminal_outcome = "UNKNOWN"
        self.terminal_acceptance = {}
        self.protocol_state = "ACTIVE"
        self._planner_original_actions = {}
        self._canonical_action_proposals = {}
        self.action_result_ledger = {}
        self.continuation_store = None

    def planner(self, prompt, expected, attachments):
        self.expected_protocols.append(expected)
        assert len(prompt) < 24000
        return next(self.replies)

    def _check_cancelled(self):
        pass

    def _emit(self, event, **fields):
        pass

    def _execute_action(self, action):
        self.executed.append(action)
        return self.result


def tool_json(tool, **args):
    return json.dumps({"type": "tool_call", "name": tool, "arguments": args})


@pytest.mark.parametrize("bad", [
    "", "I ran it", '{"type":"tool_call","name":"read_file"}',
    '{"type":"tool_call","name":"read_file","arguments":{},"tool":"delete_path"}',
    '{"type":"tool_call","type":"finish","summary":"ok"}',
    '[{"type":"finish","summary":"ok"}]',
    '{"type":"finish","summary":"ok"} trailing',
])
def test_parser_rejects_ambiguous_transport(bad):
    with pytest.raises(MinimalDecisionError):
        parse_minimal_decision(bad)


def test_parser_single_object_and_fenced_json():
    value = tool_json("list_directory", path=r"C:\Repo")
    assert parse_minimal_decision(value).arguments["path"] == r"C:\Repo"
    fence = chr(96) * 3
    assert parse_minimal_decision(fence + "json\n" + value + "\n" + fence).tool == "list_directory"
    assert parse_minimal_decision('{"type":"finish","summary":"ok"}').kind == "finish"
    assert parse_minimal_decision('{"type":"blocked","reason":"scope"}').kind == "blocked"


def test_command_admission_and_proven_read_only(tmp_path):
    tools = FakeTools(tmp_path, "run tests")
    git, reason = admit_minimal_action(MinimalDecision(
        "tool_call", "run_command",
        {"command": "git status --short", "operation": "GIT_INSPECT"}), tools)
    assert git and not reason and not action_is_mutating(git)
    python, reason = admit_minimal_action(MinimalDecision(
        "tool_call", "run_command",
        {"command": "python -m pytest", "operation": "TEST"}), tools)
    assert python is None and reason == "mutation_success_criteria_required"
    cmd_read, reason = admit_minimal_action(MinimalDecision(
        "tool_call", "run_command",
        {"command": "cmd /c dir", "operation": "INSPECT"}), tools)
    assert cmd_read is not None and not reason and not action_is_mutating(cmd_read)
    cmd, reason = admit_minimal_action(MinimalDecision(
        "tool_call", "run_command",
        {"command": "cmd /c echo ok", "operation": "INSPECT"}), tools)
    assert cmd is None and reason == "command_not_proven_read_only"


def test_runtime_owned_field_rejected(tmp_path):
    action, reason = admit_minimal_action(MinimalDecision(
        "tool_call", "list_directory", {"action_id": "FORGED"}),
        FakeTools(tmp_path, "list"))
    assert action is None and reason == "runtime_owned_argument_rejected"


def test_two_model_rounds_single_directory_listing(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    request = "列出 " + str(tmp_path) + " 底下檔案"
    result = encode_result("SUCCEEDED", "DIRECTORY_LISTED",
                           tool="list_directory", executed=True,
                           requested_path=str(tmp_path),
                           data={"entries": [{"name": "main.py", "kind": "FILE"}],
                                 "terminal_eligible": True})
    loop = FakeLoop(tmp_path, request, [
        tool_json("list_directory", path=str(tmp_path)),
        '{"type":"finish","summary":"main.py"}',
    ], result)
    assert run_minimal_core(loop, request) == "main.py"
    assert loop.terminal_outcome == "SUCCESS"
    assert len(loop.executed) == 1
    assert loop.expected_protocols == [None, None]
    assert loop.executed[0]["action_id"].startswith("MIN-")
    assert len(loop.action_result_ledger) == 1
    assert list(loop.action_result_ledger.values())[0]["result_status"] == "SUCCEEDED"
    assert (tmp_path / "localdata/runtime/minimal_core/TASK-MINIMAL-TEST.jsonl").is_file()


def test_unknown_result_cannot_be_verified(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    request = "建立 " + str(tmp_path / "new")
    result = encode_result("UNKNOWN", "TOOL_OUTCOME_UNKNOWN",
                           tool="create_directory", executed=True)
    loop = FakeLoop(tmp_path, request, [
        tool_json("create_directory", path=str(tmp_path / "new")),
        '{"type":"finish","summary":"done"}',
    ], result)
    assert "尚無足夠證據" in run_minimal_core(loop, request)
    assert loop.terminal_outcome == "UNKNOWN"


def test_invalid_reply_never_runs_action(tmp_path):
    loop = FakeLoop(tmp_path, "list", ["bad", "bad"], "")
    with pytest.raises(RuntimeError, match="MINIMAL_DECISION_FORMAT_FAILED"):
        run_minimal_core(loop, "list")
    assert not loop.executed


def test_uncertain_mutation_not_replayed(tmp_path):
    from agent_core.tool_result_contract import encode_result
    request = "創立 " + str(tmp_path / "new")
    result = encode_result("UNKNOWN", "UNKNOWN", tool="create_directory", executed=True)
    call = tool_json("create_directory", path=str(tmp_path / "new"))
    loop = FakeLoop(tmp_path, request, [call, call], result)
    with pytest.raises(RuntimeError, match="MINIMAL_MUTATION_REPLAY_UNCERTAIN"):
        run_minimal_core(loop, request)
    assert len(loop.executed) == 1
