"""Minimal Core v10: offline parser, admission and PokeClaw-style loop tests."""
from __future__ import annotations

import json
import pytest

from WebAgent.minimal_decision import (
    MinimalDecision, MinimalDecisionError, parse_minimal_decision,
    parse_minimal_decision_with_diagnostics,
)
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
        self.task_request = goal
        self.tools = FakeTools(root, goal)
        self.replies = iter(replies)
        self.result = result
        self.executed = []
        self.prompts = []
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
        self.prompts.append(prompt)
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



def test_real_smartagent_registry_lists_directory_without_planner_progress(tmp_path):
    """Exercise the actual canonical scope and tool dispatch, not just a mock."""
    from WebAgent.tool_context import WebAgentToolContext
    from agent_core.tool_result_contract import decode_result

    (tmp_path / "sample.txt").write_text("hi", encoding="utf-8")
    context = WebAgentToolContext(tmp_path)
    context.begin_run("RR-MINIMAL-SMOKE", "列出 " + str(tmp_path), [str(tmp_path)])
    decision = MinimalDecision("tool_call", "list_directory", {"path": str(tmp_path)})
    action, rejection = admit_minimal_action(decision, context)
    assert rejection == ""
    assert action and action["tool"] == "list_directory"
    result = decode_result(context.execute(action))
    assert result["status"] == "SUCCEEDED"
    assert result["reason"] == "DIRECTORY_LISTED"
    assert result["executed"] is True
    assert any(entry["name"] == "sample.txt" for entry in result["data"]["entries"])



def test_two_round_git_status_finishes_with_verified_observation(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    request = "請執行 git status --short"
    receipt = encode_result(
        "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
        data={"exit_code": 0, "terminal_eligible": True,
              "observation_confirmed": True, "verification_status": "UNVERIFIED"},
    )
    loop = FakeLoop(tmp_path, request, [
        tool_json("run_command", command="git status --short", operation="GIT_INSPECT"),
        '{"type":"finish","summary":"Git status obtained"}',
    ], receipt)
    assert run_minimal_core(loop, request) == "Git status obtained"
    assert loop.terminal_outcome == "SUCCESS"
    assert len(loop.executed) == 1


def test_generic_mutation_not_marked_complete_from_exit_code(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    request = "請執行 git commit -m update"
    receipt = encode_result("SUCCEEDED", "COMMAND_EXITED", tool="run_command",
                            executed=True, data={"exit_code": 0,
                                                 "terminal_eligible": True,
                                                 "verification_status": "PASS"})
    loop = FakeLoop(tmp_path, request, [
        tool_json("run_command", command="git commit -m update",
                  operation="GIT_MUTATE", success_criteria="commit verified",
                  verify=[{"action": "run_command", "command": "git rev-parse HEAD"}]),
        '{"type":"finish","summary":"committed"}',
    ], receipt)
    message = run_minimal_core(loop, request)
    assert "尚無足夠證據" in message
    assert loop.terminal_outcome == "UNKNOWN"



def test_real_registry_creates_one_explicit_authorized_directory(tmp_path):
    """The original folder-creation Live failure must reach the actual tool."""
    from WebAgent.tool_context import WebAgentToolContext
    from agent_core.tool_result_contract import decode_result

    target = tmp_path / "new_directory"
    context = WebAgentToolContext(tmp_path)
    context.begin_run(
        "RR-MINIMAL-MKDIR", "創立 " + str(target), [str(target)]
    )
    action, rejection = admit_minimal_action(
        MinimalDecision("tool_call", "create_directory", {"path": str(target)}),
        context,
    )
    assert not rejection, rejection
    result = decode_result(context.execute(action))
    assert result and result["status"] == "SUCCEEDED", result
    assert result["reason"] == "DIRECTORY_CREATED"
    assert target.is_dir()
    # The same target cannot be written a second time through CREATE_NEW.
    another = decode_result(context.execute({**action, "action_id": "MIN-RETRY"}))
    assert another and another["status"] == "REJECTED"
    assert another["reason"] == "DIRECTORY_ALREADY_EXISTS_REOBSERVE"



@pytest.mark.skipif(__import__("os").name != "nt", reason="PowerShell executor is Windows-only")
def test_real_windows_git_and_cmd_inspect(tmp_path):
    import subprocess
    from WebAgent.tool_context import WebAgentToolContext
    from agent_core.tool_result_contract import decode_result

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    context = WebAgentToolContext(tmp_path)
    for command, operation in [("git status --short", "GIT_INSPECT"),
                               ("cmd /c dir", "INSPECT")]:
        context.begin_run("RR-WIN-CMD", "請執行 " + command, [str(tmp_path)])
        action, rejection = admit_minimal_action(
            MinimalDecision("tool_call", "run_command", {
                "command": command, "operation": operation,
                "working_directory": str(tmp_path),
            }), context,
        )
        assert not rejection, (command, rejection)
        result = decode_result(context.execute(action))
        assert result and result["status"] == "SUCCEEDED", (command, result)
        assert result["executed"] is True
        assert result["data"]["exit_code"] == 0


@pytest.mark.skipif(__import__("os").name != "nt", reason="PowerShell executor is Windows-only")
def test_real_windows_python_command_with_postcondition(tmp_path):
    from WebAgent.tool_context import WebAgentToolContext
    from agent_core.tool_result_contract import decode_result

    context = WebAgentToolContext(tmp_path)
    context.begin_run("RR-WIN-PY", "請執行 python --version", [str(tmp_path)])
    action, rejection = admit_minimal_action(MinimalDecision(
        "tool_call", "run_command", {
            "command": "python --version", "operation": "TEST",
            "working_directory": str(tmp_path),
            "success_criteria": "Python interpreter returns a version and exits zero",
            "verify": [{"action": "run_command", "command": "python --version",
                        "expect_regex": "Python"}],
        }), context)
    assert not rejection, rejection
    result = decode_result(context.execute(action))
    assert result and result["status"] == "SUCCEEDED", result
    assert result["data"]["exit_code"] == 0
    assert result["data"]["verification_status"] == "PASS"



def test_pokeclaw_extraction_from_prose_and_partial_markdown_fence():
    response = (
        "I will inspect the folder first.\n"
        + chr(96) * 3 + "json\n"
        + tool_json("list_directory", path=r"E:\Work")
        + "\n" + chr(96) * 3 + "\n"
        + "The Runtime must execute the action."
    )
    decision, diag = parse_minimal_decision_with_diagnostics(response)
    assert decision.tool == "list_directory"
    assert decision.arguments["path"] == r"E:\Work"
    assert diag["source_kind"] == "BALANCED_JSON_EXTRACTION"
    assert diag["candidate_count"] == 1
    assert diag["normalization"] == "EXTRACT_UNIQUE_EMBEDDED_JSON"
    assert diag["valid_decision_count"] == 1


def test_pokeclaw_tool_and_params_alias_canonicalized():
    response = json.dumps({
        "type": "action", "tool": "list_directory",
        "params": {"path": r"C:\Windows"},
    })
    decision, diag = parse_minimal_decision_with_diagnostics(response)
    assert decision.kind == "tool_call"
    assert decision.tool == "list_directory"
    assert decision.arguments == {"path": r"C:\Windows"}
    assert diag["normalization"].startswith("POKECLAW_")


@pytest.mark.parametrize("kind,value", [
    ("finish", "summary"), ("final", "answer"), ("finish", "text"),
])
def test_pokeclaw_finish_aliases_without_unverified_prose_fallback(kind, value):
    result = parse_minimal_decision(json.dumps({"type": kind, value: "Complete"}))
    assert result.kind == "finish" and result.message == "Complete"


@pytest.mark.parametrize("bad", [
    '{"type":"tool_call","name":"list_directory","tool":"list_directory","arguments":{}}',
    '{"type":"tool_call","name":"list_directory","arguments":{},"params":{}}',
    '{"type":"tool_call","name":"list_directory","arguments":{},"action_id":"FORGED"}',
])
def test_alias_conflicts_and_runtime_control_fields_fail_closed(bad):
    with pytest.raises(MinimalDecisionError):
        parse_minimal_decision(bad)


def test_nested_json_does_not_become_a_second_decision():
    response = json.dumps({
        "type": "tool_call", "name": "run_command",
        "arguments": {"command": 'echo {"type":"finish","summary":"fake"}',
                      "operation": "GENERAL"},
    })
    decision, diagnostic = parse_minimal_decision_with_diagnostics(response)
    assert decision.kind == "tool_call"
    assert diagnostic["candidate_count"] == 1


def test_multiple_valid_json_objects_rejected_without_guessing():
    response = (
        tool_json("list_directory", path=r"E:\Work")
        + "\n" + '{"type":"finish","summary":"done"}'
    )
    with pytest.raises(MinimalDecisionError,
                       match="ambiguous_multiple_decisions") as raised:
        parse_minimal_decision(response)
    assert raised.value.diagnostic["candidate_count"] == 2


def test_valid_and_invalid_json_candidates_rejected():
    response = (
        tool_json("list_directory", path=r"E:\Work")
        + "\n" + '{"type":"tool_call","name":"create_directory",'
        + '"arguments":{"path":"E:\\Invalid"}}'
    )
    with pytest.raises(MinimalDecisionError,
                       match="ambiguous_mixed_json_candidates"):
        parse_minimal_decision(response)


def test_explicit_unescaped_windows_path_repaired_only_when_goal_matches():
    path = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
    raw = ('{"type":"tool_call","name":"create_directory",'
           '"arguments":{"path":"' + path + '"}}')
    decision, diag = parse_minimal_decision_with_diagnostics(
        raw, original_goal="創立這個資料夾\n" + path)
    assert decision.tool == "create_directory"
    assert decision.arguments["path"] == path
    assert diag["normalization"] == "EXPLICIT_WINDOWS_PATH_BACKSLASH_ESCAPED"
    with pytest.raises(MinimalDecisionError, match="json_invalid_escape") as raised:
        parse_minimal_decision(raw, original_goal=r"創立 E:\Other")
    assert raised.value.diagnostic["json_error_line"] == 1
    assert raised.value.diagnostic["json_error_column"] > 0


def test_windows_path_newline_tab_escapes_not_silently_changed():
    # These parse as valid JSON escapes, but are incorrect filesystem paths.
    for path in (r"E:\new", r"E:\temp"):
        raw = ('{"type":"tool_call","name":"create_directory",'
               '"arguments":{"path":"' + path + '"}}')
        action, diag = parse_minimal_decision_with_diagnostics(
            raw, original_goal="創立 " + path)
        assert action.arguments["path"] == path
        assert diag["normalization"] == "EXPLICIT_WINDOWS_PATH_BACKSLASH_ESCAPED"


def test_windows_path_repair_does_not_rewrite_commands_or_unknown_paths():
    response = ('{"type":"tool_call","name":"run_command","arguments":'
                '{"command":"E:\\PrivateApp\\bin.exe","operation":"GENERAL"}}')
    with pytest.raises(MinimalDecisionError, match="json_invalid_escape"):
        parse_minimal_decision(response, original_goal=r"執行 E:\PrivateApp\bin.exe")


def test_legacy_fence_diagnostic_without_implicit_v9_execution():
    response = (
        chr(96) * 3 + "smartagent_tool\n"
        + '{"tool":"create_directory","path":"E:\\Folder"}\n'
        + chr(96) * 3
    )
    with pytest.raises(MinimalDecisionError, match="legacy_protocol_response"):
        parse_minimal_decision(response)


def test_rejection_diagnostic_redacts_original_text_and_credentials():
    secret = "UNSAFE_SECRET_ABC123456"
    response = "Response with " + secret + " and some invalid JSON"
    with pytest.raises(MinimalDecisionError) as raised:
        parse_minimal_decision(response)
    diagnostic = raised.value.diagnostic
    assert secret not in diagnostic["response_structure_redacted"]
    assert diagnostic["candidate_count"] == 0
    assert diagnostic["response_sha256"]
    assert diagnostic["reason_code"] == "json_object_not_found"


def test_format_repair_hint_contains_code_and_never_executes_bad_reply(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    goal = "列出 " + str(tmp_path)
    failure_reply = ('{"type":"tool_call","name":"create_directory",'
                     '"arguments":{"path":"E:\\Other"}}')
    actual_receipt = encode_result(
        "SUCCEEDED", "DIRECTORY_LISTED", tool="list_directory", executed=True,
        data={"entries": [{"name": "ok.py", "kind": "FILE"}],
              "terminal_eligible": True},
    )
    loop = FakeLoop(tmp_path, goal, [
        failure_reply, tool_json("list_directory", path=str(tmp_path)),
        '{"type":"finish","summary":"ok.py"}',
    ], actual_receipt)
    # A repaired tool call on decision #2 returns the confirmed observation;
    # no third Finish is needed or permitted for this isolated read task.
    answer = run_minimal_core(loop, goal)
    assert "ok.py" in answer
    assert loop.terminal_outcome == "SUCCESS"
    assert len(loop.expected_protocols) == 2
    assert len(loop.executed) == 1
    assert "Invalid JSON escape" in loop.prompts[1]
    assert "Last decision rejected before execution" in loop.prompts[1]
    receipts = (tmp_path / "localdata/runtime/minimal_core/TASK-MINIMAL-TEST.jsonl").read_text(encoding="utf-8")
    assert "json_invalid_escape" in receipts
    assert "json_error_column" in receipts


def test_prose_without_json_remains_failure_not_fake_finish():
    with pytest.raises(MinimalDecisionError, match="json_object_not_found"):
        parse_minimal_decision("The folder has been created, I think.")



def test_pokeclaw_single_json_with_trailing_explanation_is_accepted():
    response = '{"type":"finish","summary":"ok"} trailing note'
    decision, meta = parse_minimal_decision_with_diagnostics(response)
    assert decision.kind == "finish" and decision.message == "ok"
    assert meta["candidate_count"] == 1
    assert meta["source_kind"] == "BALANCED_JSON_EXTRACTION"


def test_legacy_v9_envelope_with_valid_json_still_rejected():
    fence = chr(96) * 3
    response = fence + "smartagent_tool\n" + tool_json("delete_path", path=r"E:\Erase") + "\n" + fence
    with pytest.raises(MinimalDecisionError, match="legacy_protocol_response"):
        parse_minimal_decision(response)



def test_runtime_auto_finishes_mkdir_after_first_action_without_model_finish(tmp_path, monkeypatch):
    """Original Live regression: actual directory creation must not be undone
    by a second model round or arbitrary number of subsequent observations."""
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    destination = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
    goal = "創立這個資料夾\n" + destination
    evidence = encode_result(
        "SUCCEEDED", "DIRECTORY_CREATED", tool="create_directory",
        executed=True, requested_path=destination, resolved_path=destination,
        data={
            "directory_created": True, "observed_exists": True,
            "observed_is_directory": True, "observation_status": "CONFIRMED",
            "goal_completion_verified": False,
        },
    )
    loop = FakeLoop(tmp_path, goal, [
        tool_json("create_directory", path=destination),
        "model should never be asked to finish this proven goal",
    ], evidence)
    summary = run_minimal_core(loop, goal)
    assert destination in summary
    assert len(loop.executed) == 1
    assert len(loop.expected_protocols) == 1
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["schema"] == "SMARTAGENT_TERMINAL_ACCEPTANCE_V1"
    assert loop.terminal_acceptance["evidence_verdict"] == "PASS"
    assert loop.terminal_acceptance["evidence_refs"] == [
        loop.executed[0]["action_id"]
    ]
    journal = (tmp_path / "localdata/runtime/minimal_core/TASK-MINIMAL-TEST.jsonl")
    events = [json.loads(row)["event"] for row in journal.read_text(encoding="utf-8").splitlines()]
    assert "runtime_goal_verified" in events
    assert "runtime_autofinish" in events
    assert "decision_rejected" not in events


def test_multi_step_goal_does_not_auto_finish_after_directory_created(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    destination = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
    goal = "建立 " + destination + " 並寫入 README.md"
    evidence = encode_result(
        "SUCCEEDED", "DIRECTORY_CREATED", tool="create_directory",
        executed=True, requested_path=destination, resolved_path=destination,
        data={"directory_created": True, "observed_exists": True,
              "observed_is_directory": True, "observation_status": "CONFIRMED"},
    )
    loop = FakeLoop(tmp_path, goal, [
        tool_json("create_directory", path=destination),
        '{"type":"finish","summary":"Done!"}',
    ], evidence)
    message = run_minimal_core(loop, goal)
    assert "尚無足夠證據" in message
    assert loop.terminal_outcome == "UNKNOWN"
    assert loop.terminal_acceptance == {}
    assert len(loop.executed) == 1



def test_live_rca_ai_gravity_joke_text_only_completes_first_round(tmp_path, monkeypatch):
    """Exact RCA 2026-11-08: valid finish is text-only SUCCESS, not PAUSED."""
    from agent_core.task_progress import set_runtime_state
    from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
    from agent_core.tri_one_task_result import make_task_result, validate_task_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    request = "說個關於ai 加上引力理論的笑話"
    joke = '物理學家問 AI：「你真的理解廣義相對論嗎？」AI 回答：「我的參數足以彎曲時空。」'
    loop = FakeLoop(tmp_path, request, [
        json.dumps({"type": "finish", "summary": joke}, ensure_ascii=False),
    ], "")
    answer = run_minimal_core(loop, request)
    assert answer == joke
    assert not loop.executed
    assert loop.terminal_outcome == "SUCCESS"
    receipt = loop.terminal_acceptance
    assert receipt["completion_kind"] == "ANSWER_ONLY"
    assert receipt["admission_reason"] == "ANSWER_ONLY_RESPONSE_ADMITTED"
    assert receipt["evidence_refs"] == []
    assert receipt["evidence_verdict"] == "UNVERIFIED"
    assert receipt["answer_sha256"]
    assert terminal_verdict_from_loop(loop).is_complete
    payload = make_task_result(
        request_id=loop.run_id, task_id=loop.task_id,
        task_epoch=loop.task_epoch, intent_digest="f" * 64,
        source_interface="TELEGRAM",
        trusted_reply_route={"transport": "TELEGRAM"},
        state="COMPLETED", outcome="SUCCESS",
        terminal_acceptance=receipt, answer_text=answer,
        evidence_refs=[], operational_action_count=0,
        operational_action_count_basis="RESULT_LEDGER", artifacts=[],
    )
    validate_task_result(payload)
    assert payload["completion_kind"] == "ANSWER_ONLY"
    assert payload["outcome"] == "SUCCESS"
    assert payload["operational_action_count"] == 0
    assert loop.expected_protocols == [None]
    journal = (tmp_path / "localdata/runtime/minimal_core/TASK-MINIMAL-TEST.jsonl")
    events = [json.loads(x)["event"] for x in journal.read_text(encoding="utf-8").splitlines()]
    assert "runtime_text_only_admitted" in events
    assert "model_finish" not in events


@pytest.mark.parametrize("goal_text", [
    "創立這個資料夾\nE:\\TestTextOnlyNotAllowed",
    "請讀取 E:\\private\\data.txt 並幫我說明內容",
    "說個笑話，然後執行 python -m pytest",
    "今天最新的引力波研究新聞是什麼？",
])
def test_non_text_only_cannot_complete_without_tool_evidence(tmp_path, monkeypatch, goal_text):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    loop = FakeLoop(tmp_path, goal_text, ['{"type":"finish","summary":"完成"}'], "")
    answer = run_minimal_core(loop, goal_text)
    assert "尚無足夠證據" in answer
    assert loop.terminal_outcome == "UNKNOWN"
    assert not loop.terminal_acceptance


def test_text_only_with_attached_material_needs_real_context_evidence(tmp_path, monkeypatch):
    from agent_core.tri_one_runtime_terminal import accept_runtime_answer_only
    request = "說個關於 AI 的笑話"
    loop = FakeLoop(tmp_path, request, [], "")
    receipt, reason = accept_runtime_answer_only(
        loop, goal=request, answer="好笑", had_attachments=True)
    assert receipt is None and reason == "ANSWER_ONLY_REQUIRES_RESOURCE_CONTEXT"


def test_finish_claims_tool_execution_without_evidence_not_answer_only(tmp_path, monkeypatch):
    from agent_core.tri_one_runtime_terminal import accept_runtime_answer_only
    goal = "說個關於 AI 的笑話"
    loop = FakeLoop(tmp_path, goal, [], "")
    record, reason = accept_runtime_answer_only(
        loop, goal=goal, answer="我已執行本地工具，現在有笑話。")
    assert record is None and reason == "ANSWER_ONLY_UNVERIFIED_EXECUTION_CLAIM"


def test_goal_driver_context_carries_canonical_gaps_and_denied_read_gate(tmp_path, monkeypatch):
    from WebAgent.minimal_goal_driver import (
        build_goal_driver_context, compact_goal_driver_context,
        gate_known_absent_directory,
    )
    path = r"E:\GoalDriver\missing"
    request = "若 " + path + " 資料夾不存在則建立，並執行測試"
    loop = FakeLoop(tmp_path, request, [], "")
    canonical = {
        "context_snapshot_id": "CTX-FAKE-1",
        "persistent_goal_gaps": {
            "active_count": 1,
            "active_gaps": [{
                "gap_id": "GAP-1", "kind": "DIRECTORY_EXISTENCE",
                "path": path, "status": "UNRESOLVED",
                "observation_status": "CONFIRMED",
                "observed_value": False, "observed_fact_ref": "MIN-OBS-1",
                "goal_link": "CONDITIONAL_REMEDIATION_CANDIDATE",
            }],
            "gaps": [],
        },
        "goal_prerequisite_resolution": {
            "next_category": "PROPOSE_GOAL_ACTION_FOR_ADMISSION",
            "source_requirement": "NOT_REQUIRED",
            "reason_code": "TARGET_ABSENT_SOURCE_NOT_REQUIRED",
        },
        "directory_decision_rule": {
            "decision": "CREATE_DIRECTORY_REQUIRED",
            "candidate": {"tool": "create_directory", "target": path,
                          "status": "UNTRIED", "strategy_id": "STRAT-FAKE"},
        },
        "strategy_candidates": {"candidates": []},
        "action_loop_decision": {"required_transition": "CONTINUE"},
    }
    monkeypatch.setattr(
        "agent_core.planner_context_snapshot.build_planner_context_snapshot",
        lambda runtime: canonical,
    )
    context = build_goal_driver_context(loop, request, [])
    assert context["snapshot_source"] == "CANONICAL_SHARED_BLACKBOARD"
    assert context["confirmed_missing_targets"][0]["evidence_ref"] == "MIN-OBS-1"
    assert context["next_category"] == "PROPOSE_GOAL_ACTION_FOR_ADMISSION"
    assert context["directory_candidate"]["tool"] == "create_directory"
    assert context["scope_authorization_granted"] is False
    assert gate_known_absent_directory(
        {"tool": "query_project", "project_root": path}, context,
    )["reason_code"] == "CONFIRMED_MISSING_PREREQUISITE_DIRECTORY"
    assert gate_known_absent_directory(
        {"tool": "inspect_directory", "paths": [path]}, context,
    ) is None
    assert gate_known_absent_directory(
        {"tool": "create_directory", "path": path}, context,
    ) is None
    assert gate_known_absent_directory(
        {"tool": "query_project", "project_root": r"E:\Other"}, context,
    ) is None
    encoded = compact_goal_driver_context(context)
    assert "MIN-OBS-1" in encoded
    assert len(encoded) <= 6500


def test_goal_driver_snapshot_failure_is_diagnostic_not_an_absence(tmp_path, monkeypatch):
    from WebAgent.minimal_goal_driver import build_goal_driver_context
    goal = "建立 E:\\new\\project 並編譯"
    loop = FakeLoop(tmp_path, goal, [], "")
    def unavailable(*args):
        raise RuntimeError("snapshot_not_ready")
    monkeypatch.setattr(
        "agent_core.planner_context_snapshot.build_planner_context_snapshot",
        unavailable,
    )
    context = build_goal_driver_context(loop, goal, [])
    assert context["snapshot_source"] == "DURABLE_GOAL_GAP_FALLBACK"
    assert context["snapshot_error_type"] == "RuntimeError"
    assert not context["confirmed_missing_targets"]
    assert context["scope_authorization_granted"] is False


def test_goal_driver_rejection_feedback_reaches_next_planner_round(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    req = "列出 " + str(tmp_path) + " 的檔案"
    result = encode_result("SUCCEEDED", "DIRECTORY_LISTED",
                           tool="list_directory", executed=True,
                           requested_path=str(tmp_path),
                           data={"entries": [], "terminal_eligible": True})
    loop = FakeLoop(tmp_path, req, [
        tool_json("list_directory", action_id="FORGED"),  # Runtime-owned field forbidden
        tool_json("list_directory", path=str(tmp_path)),
        '{"type":"finish","summary":"空資料夾"}',
    ], result)
    # A repaired ToolCall uses the second atomic decision. Its already
    # verified bounded observation is rendered without a THIRD model reply.
    observed = run_minimal_core(loop, req)
    assert "Runtime 已驗證單次觀測結果" in observed
    assert len(loop.expected_protocols) == 2
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.executor_result["outcome"] == "VERIFIED"
    assert len(loop.executed) == 1
    assert "recent_rejections" in loop.prompts[1]
    assert "runtime_owned_argument_rejected" in loop.prompts[1]
    assert "Runtime Action Admission denied" in loop.prompts[1]



@pytest.mark.parametrize("user_goal,expected", [
    ("說個關於ai 加上引力理論的笑話", "ANSWER_ONLY"),
    ("講一個關於相對論的故事", "ANSWER_ONLY"),
    ("請解釋什麼是廣義相對論", "ANSWER_ONLY"),
    ("列出 /tmp/project 底下檔案", "RESOURCE_REQUIRED"),
    ("列出 /tmp/project", "RESOURCE_REQUIRED"),
    ("列出 E:\\Project\\files", "RESOURCE_REQUIRED"),
    ("說個笑話，然後執行 python -m pytest", "RESOURCE_REQUIRED"),
])
def test_text_only_route_does_not_confuse_local_paths_with_prose(user_goal, expected):
    from agent_core.task_requirement import assess_task_requirement
    assert assess_task_requirement(user_goal)["route"] == expected



@pytest.mark.skipif(__import__("os").name != "nt",
                    reason="Windows native directory observation regression")
def test_actual_windows_inspect_absence_goal_gap_resolves_after_create(tmp_path):
    """Real ToolContext ToolResult drives Goal Gap and Prerequisite decisions."""
    from types import SimpleNamespace
    from WebAgent.tool_context import WebAgentToolContext
    from WebAgent.minimal_goal_driver import (
        build_goal_driver_context, gate_known_absent_directory,
    )
    from agent_core.tool_result_contract import decode_result

    path = str(tmp_path / "goal_driver_absent")
    goal = "如果 " + path + " 資料夾不存在就建立它"
    tools = WebAgentToolContext(tmp_path)
    tools.begin_run("RR-WIN-GOAL-GAP", goal, [path])
    inspect_action, issue = admit_minimal_action(
        MinimalDecision("tool_call", "inspect_directory", {"paths": [path]}),
        tools,
    )
    assert issue == "", issue
    observed = tools.execute(inspect_action)
    receipt = decode_result(observed)
    assert receipt["status"] == "SUCCEEDED", receipt
    assert receipt["reason"] == "DIRECTORY_ABSENCE_OBSERVED"
    assert receipt["executed"] is True
    assert receipt["data"]["observation_status"] == "CONFIRMED"

    loop = SimpleNamespace(
        run_id="RR-WIN-GOAL-GAP", task_id="TASK-WIN-GOAL-GAP",
        task_epoch="goal-epoch", task_request=goal, workspace=str(tmp_path),
        progress_ledger=SimpleNamespace(goal=goal),
        action_ledger={inspect_action["action_id"]: {
            "action": dict(inspect_action), "result": str(observed)}},
        action_result_ledger={inspect_action["action_id"]: {
            "tool": "inspect_directory", "result_status": "SUCCEEDED",
            "result_reason": receipt["reason"], "executed": True,
            "evidence_status": "ACTIVE", "task_epoch": "goal-epoch",
            "request_id": "RR-WIN-GOAL-GAP", "result": str(observed)}},
        tools=tools, authorized_paths=[path],
    )
    before = build_goal_driver_context(loop, goal, [])
    assert before["confirmed_missing_targets"], before
    assert gate_known_absent_directory(
        {"tool": "query_project", "project_root": path}, before
    )["reason_code"] == "CONFIRMED_MISSING_PREREQUISITE_DIRECTORY"

    create_action, issue = admit_minimal_action(
        MinimalDecision("tool_call", "create_directory", {"path": path}),
        tools,
    )
    assert issue == "", issue
    created = tools.execute(create_action)
    created_receipt = decode_result(created)
    assert created_receipt["status"] == "SUCCEEDED", created_receipt
    loop.action_ledger[create_action["action_id"]] = {
        "action": dict(create_action), "result": str(created)
    }
    loop.action_result_ledger[create_action["action_id"]] = {
        "tool": "create_directory", "result_status": "SUCCEEDED",
        "result_reason": created_receipt["reason"], "executed": True,
        "evidence_status": "ACTIVE", "task_epoch": "goal-epoch",
        "request_id": "RR-WIN-GOAL-GAP", "result": str(created),
    }
    after = build_goal_driver_context(loop, goal, [])
    assert not after["confirmed_missing_targets"], after
    assert gate_known_absent_directory(
        {"tool": "query_project", "project_root": path}, after
    ) is None



def test_live_rca_conditional_directory_goal_auto_terminal_after_real_like_stat(tmp_path, monkeypatch):
    """RCA TASK-02D4A8B2A612BB5E546B, without a redundant model Finish."""
    from agent_core.tool_result_contract import attach_runtime_result
    from agent_core.evidence_foundation import observation
    from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    destination = r"E:\SmartAgent_TriOne_Live_Test_B"
    goal = (f"檢查 {destination} 是否存在，如果不存在就建立，"
            "最後確認資料夾確實存在。")
    loop = FakeLoop(tmp_path, goal, [
        tool_json("observe_fact", path=destination),
        tool_json("create_directory", path=destination),
        tool_json("observe_fact", path=destination),
        '{"type":"finish","summary":"Should not need this turn"}',
    ], "")
    loop.action_ledger = {}
    def actual_tool(action):
        loop.executed.append(action)
        tool = action["tool"]
        if tool == "observe_fact":
            exists = len(loop.executed) == 3
            fact = observation(
                "file.exists", exists, source="filesystem.stat",
                scope=destination, raw_ref=action["action_id"],
            )
            payload = {"observations": [fact], "observation_status": "CONFIRMED",
                       "verification_status": "UNVERIFIED", "terminal_eligible": True}
            raw = attach_runtime_result(
                "{}", "SUCCEEDED", "FACT_OBSERVED", tool=tool, executed=True,
                requested_path=destination, resolved_path=destination,
                data=payload, action_id=action["action_id"],
                request_id=loop.run_id, task_id=loop.task_id)
        else:
            raw = attach_runtime_result(
                "{}", "SUCCEEDED", "DIRECTORY_CREATED", tool=tool, executed=True,
                requested_path=destination, resolved_path=destination,
                data={"directory_created": True, "observed_exists": True,
                      "observed_is_directory": True, "observation_status": "CONFIRMED"},
                action_id=action["action_id"], request_id=loop.run_id,
                task_id=loop.task_id)
        loop.action_ledger[action["action_id"]] = {
            "action": dict(action), "result": raw,
        }
        return raw
    loop._execute_action = actual_tool
    answer = run_minimal_core(loop, goal)
    assert destination in answer
    assert "確認" in answer
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.protocol_state == "COMPLETED"
    assert terminal_verdict_from_loop(loop).is_complete
    assert len(loop.executed) == 3
    assert len(loop.expected_protocols) == 3
    assert list(loop.action_result_ledger) == list(loop.terminal_acceptance["evidence_refs"])
    assert loop.terminal_acceptance["matched_condition_id"] == "CONDITIONAL_DIRECTORY_EXISTS"
    journal = (tmp_path / "localdata/runtime/minimal_core/TASK-MINIMAL-TEST.jsonl")
    events = [json.loads(x)["event"] for x in journal.read_text(encoding="utf-8").splitlines()]
    assert "runtime_goal_verified" in events
    assert "runtime_autofinish" in events
    assert "model_finish" not in events


@pytest.mark.skipif(__import__("os").name != "nt",
                    reason="Native Windows filesystem evidence contract")
def test_real_windows_conditional_goal_observe_create_observe_is_terminal(tmp_path):
    from types import SimpleNamespace
    from WebAgent.tool_context import WebAgentToolContext
    from agent_core.tri_one_runtime_terminal import (
        evaluate_goal_evidence, accept_runtime_terminal, terminal_verdict_from_loop,
    )
    from agent_core.goal_gap_ledger import reconstruct_goal_gaps
    from agent_core.tool_result_contract import decode_result
    import hashlib

    target = str(tmp_path / "conditional_directory_live")
    goal = (f"檢查 {target} 是否存在，如果不存在就建立，"
            "最後確認資料夾確實存在。")
    context = WebAgentToolContext(tmp_path)
    context.begin_run("RR-WIN-GOAL-TERMINAL", goal, [target])
    loop = SimpleNamespace(
        run_id=context.current_request_id, task_id=context.current_task_id,
        task_epoch=context.current_task_epoch, task_request=goal,
        turn_id=1, terminal_outcome="UNKNOWN", terminal_acceptance={},
        protocol_state="ACTIVE", progress_ledger=SimpleNamespace(goal=goal),
        action_ledger={}, action_result_ledger={},
    )
    history = []
    for turn, (name, values) in enumerate((
        ("observe_fact", {"path": target}),
        ("create_directory", {"path": target}),
        ("observe_fact", {"path": target}),
    ), 1):
        loop.turn_id = turn
        action, rejection = admit_minimal_action(
            MinimalDecision("tool_call", name, values), context)
        assert action is not None and not rejection, rejection
        raw = context.execute(action)
        receipt = decode_result(raw)
        assert receipt and receipt["status"] == "SUCCEEDED", receipt
        assert receipt["executed"] is True
        if name == "observe_fact":
            assert receipt["data"]["observations"][0]["value"] is (turn == 3)
        history.append({"action": action, "receipt": receipt})
        loop.action_ledger[action["action_id"]] = {
            "action": action, "result": raw,
        }
        loop.action_result_ledger[action["action_id"]] = {
            "request_id": loop.run_id, "task_id": loop.task_id,
            "task_epoch": loop.task_epoch, "tool": name,
            "result_status": receipt["status"],
            "result_reason": receipt["reason"], "executed": True,
            "evidence_status": "ACTIVE", "result": raw,
            "result_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
        gap = reconstruct_goal_gaps(loop)
        if turn == 1:
            assert gap["active_count"] == 1, gap
        if turn >= 2:
            assert gap["active_count"] == 0, gap
    proof = evaluate_goal_evidence(goal, history)
    assert proof.verified and len(proof.evidence_refs) == 3
    receipt = accept_runtime_terminal(loop, proof)
    assert receipt["admission_status"] == "ACCEPTED"
    assert terminal_verdict_from_loop(loop).is_complete
    assert target in str(tmp_path / "conditional_directory_live")
    assert (tmp_path / "conditional_directory_live").is_dir()


@pytest.mark.parametrize("greeting", ["你好", "早安", "Hello", "謝謝", "在嗎"])
def test_git27_requirement_gated_no_tool_greeting_finishes_one_round(tmp_path, monkeypatch, greeting):
    from agent_core.task_requirement import assess_task_requirement, no_tool_terminal_candidate
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    assert no_tool_terminal_candidate(assess_task_requirement(greeting))
    loop = FakeLoop(tmp_path, greeting, ['{"type":"finish","summary":"收到你的訊息"}'], "")
    assert run_minimal_core(loop, greeting) == "收到你的訊息"
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["completion_kind"] == "ANSWER_ONLY"
    assert len(loop.expected_protocols) == 1
    assert loop.executed == []


@pytest.mark.parametrize("goal_text", [
    "建立資料夾 E:\\NotTextOnly", "讀取 E:\\secret.txt 並分析",
    "下載 https://example.com/a.zip", "今天最新的財經新聞",
    "我的專案檔案在哪",
])
def test_git27_requirement_gated_no_tool_does_not_elevate_operations(goal_text):
    from agent_core.task_requirement import assess_task_requirement, no_tool_terminal_candidate
    from agent_core.tri_one_answer_only import classify_original_request
    assert not no_tool_terminal_candidate(assess_task_requirement(goal_text))
    assert not classify_original_request(goal_text)[0]


def test_git27_atomic_rejection_budget_max_two_model_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    goal = "列出 " + str(tmp_path) + " 的檔案"
    malformed = tool_json("list_directory", action_id="MODEL-FORGED", path=str(tmp_path))
    loop = FakeLoop(tmp_path, goal, [malformed, malformed, '{"type":"finish","summary":"not consumed"}'], "")
    answer = run_minimal_core(loop, goal)
    assert "Runtime 已阻止" in answer
    assert len(loop.expected_protocols) == 2
    assert loop.executed == []
    assert loop.executor_result["outcome"] == "BLOCKED"
    assert loop.executor_result["goal_verified"] is False


def test_git27_ungrounded_blocked_gets_one_redecision_then_typed_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    goal = "列出 " + str(tmp_path) + " 的檔案"
    blocked = '{"type":"blocked","reason":"我不知道要做什麼"}'
    loop = FakeLoop(tmp_path, goal, [blocked, blocked, blocked], "")
    answer = run_minimal_core(loop, goal)
    assert "整體目標未驗證" in answer
    assert len(loop.expected_protocols) == 2
    assert loop.executor_result["outcome"] == "UNKNOWN"
    assert loop.executor_result["reason_code"] == "PLANNER_UNCERTAINTY_NOT_VERIFIED"


def test_git27_confirmed_missing_parent_returns_prerequisite(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    path = r"E:\FutureParent\Child"
    goal = "建立資料夾 " + path
    result = encode_result("REJECTED", "DIRECTORY_PARENT_MISSING",
                           tool="create_directory", executed=False)
    loop = FakeLoop(tmp_path, goal, [
        tool_json("create_directory", path=path),
        '{"type":"blocked","reason":"父目錄不存在"}',
    ], result)
    run_minimal_core(loop, goal)
    assert loop.executor_result["outcome"] == "NEEDS_PREREQUISITE"
    assert len(loop.expected_protocols) == 2
    assert len(loop.executed) == 1


def test_git27_atomic_mkdir_contract_accepts_equivalent_word_order_only():
    from WebAgent.minimal_executor_contract import single_directory_contract_target
    path = r"E:\UnitFolder"
    assert single_directory_contract_target("在 " + path + " 建立資料夾", path)
    assert not single_directory_contract_target(
        "在 " + path + " 建立資料夾並寫入 README.md", path)


def test_git27_rca_hook_is_on_normal_paused_egress():
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "RemoteAgent" / "telegram_webagent_worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "terminal.state == 'PAUSED'"
    ]
    assert len(matches) == 1
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_auto_rca_debug_hook"
        for statement in matches[0].body for node in ast.walk(statement)
    )
    from agent_core.auto_rca_upload import COLLECTOR
    from agent_core.paths import install_root
    assert COLLECTOR == install_root() / "collect.bat"
