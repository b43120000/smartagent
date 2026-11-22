"""Git51: non-OS tasks have identical behavior under ACL/Turbo ON/OFF.

This is a routing contract, not a test that turns host-side file mutation into
a permitted AppContainer operation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core import turbo_broker_admission, turbo_sandbox


NON_OS_TOOLS = (
    "web_search", "ask_executor", "read_file", "list_directory",
    "inspect_directory", "find_file", "query_project", "observe_fact",
    "save_session_summary",
)
OS_MUTATIONS = (
    "run_command", "create_directory", "write_file", "delete_path",
    "write_file_chunk", "apply_edit_plan", "project_sync",
)


@pytest.mark.parametrize("acl_on,turbo_on", [
    (False, False), (False, True), (True, False), (True, True),
])
@pytest.mark.parametrize("tool", NON_OS_TOOLS)
def test_non_command_admission_does_not_ask_for_os_sandbox(
    monkeypatch, tool, acl_on, turbo_on,
):
    # If no OS action was requested, neither ACL policy nor availability
    # of a native AppContainer may block normal dialogue or web research.
    def no_sandbox(*_args, **_kwargs):
        raise AssertionError("NON_COMMAND_MUST_NOT_QUERY_TURBO_SANDBOX")

    monkeypatch.setattr(turbo_broker_admission, "resolve_turbo_state", no_sandbox)
    assert turbo_broker_admission.needs_broker(tool) is False
    admitted = turbo_broker_admission.assess(None, tool=tool, action_id="ACT-NO-CMD")
    assert admitted["decision"] == "NORMAL"
    assert admitted["reason_code"] == "NON_COMMAND_OPERATION"
    assert admitted["evidence"]["executed"] is False
    assert admitted["evidence"]["authorization_granted"] is False


@pytest.mark.parametrize("acl_on,turbo_on", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_news_lookup_tool_cannot_dispatch_restricted_executor(
    monkeypatch, acl_on, turbo_on,
):
    from agent_core import tools, restricted_executor_client, bounded_process
    calls = []

    def deny_executor(*_args, **_kwargs):
        raise AssertionError("NON_COMMAND_MUST_NOT_EXECUTE_OS_COMMAND")

    def fake_news_search(query):
        calls.append(query)
        return "NEWS_WEB_SEARCH_ONLY_NO_SHELL"

    monkeypatch.setattr(tools, "tool_web_search", fake_news_search)
    monkeypatch.setattr(restricted_executor_client, "execute_restricted", deny_executor)
    monkeypatch.setattr(bounded_process, "run_bounded_process", deny_executor)
    monkeypatch.setattr(turbo_broker_admission, "assess", deny_executor)
    result = tools.execute_tool({
        "tool": "web_search",
        "query": "Jensen Huang New York Times English news verification",
    })
    assert calls == ["Jensen Huang New York Times English news verification"]
    assert "NEWS_WEB_SEARCH_ONLY_NO_SHELL" in str(result)
    assert "TURBO_SANDBOX_BLOCKED" not in str(result)
    assert "TURBO_OS_BROKER_BLOCKED" not in str(result)


def test_internal_session_summary_not_counted_as_untrusted_host_mutation():
    assert not turbo_broker_admission.needs_broker("save_session_summary")
    for tool in OS_MUTATIONS:
        assert turbo_broker_admission.needs_broker(tool)


def test_both_executor_loops_have_action_scoped_not_global_turbo_gate():
    # Regression for the exact Telegram 'Executor BLOCKED' bug:
    # previously BOTH loops rejected even a web_search whenever Turbo ON.
    from pathlib import Path
    source_root = Path(__file__).resolve().parents[1]
    for name in ("pokeclaw_executor_loop.py", "minimal_core_loop.py"):
        source = (source_root / name).read_text(encoding="utf-8")
        assert "broker_gate = admit_action(loop.workspace, action)" in source
        assert "if broker_gate[\"decision\"] == \"BLOCKED\":" in source
        assert "require_turbo_backend(turbo_state)" not in source


@pytest.mark.parametrize("tool", NON_OS_TOOLS + ("return_artifact", "upload_file", "google_drive_upload"))
def test_loop_admits_all_non_command_actions_without_broker(monkeypatch, tool):
    def forbid_gate(*_args, **_kwargs):
        raise AssertionError("NON_COMMAND_ACL_GATE_MUST_NOT_BE_CALLED")

    monkeypatch.setattr(turbo_broker_admission, "resolve_turbo_state", forbid_gate)
    result = turbo_broker_admission.admit_action(
        "/ordinary/workspace", {"tool": tool, "action_id": "ACT-RESEARCH"},
    )
    assert result["decision"] == "NORMAL"
    assert result["reason_code"] == "NON_COMMAND_OPERATION"


def test_invalid_before_admission_must_not_query_sandbox(monkeypatch):
    def forbid_gate(*_args, **_kwargs):
        raise AssertionError("INVALID_ACTION_CANNOT_REACH_OS_GATE")

    monkeypatch.setattr(turbo_broker_admission, "resolve_turbo_state", forbid_gate)
    result = turbo_broker_admission.admit_action("/ordinary/workspace", None)
    assert result["decision"] == "NOT_APPLICABLE"
    assert result["evidence"] == {}


@pytest.mark.parametrize("mode,requested", [
    ("off", False), ("off", True), ("on", False), ("on", True),
])
def test_actual_command_never_becomes_noncommand_due_to_user_language(
    tmp_path, monkeypatch, mode, requested,
):
    workspace = tmp_path / "ws"
    root = workspace / "root"
    root.mkdir(parents=True)
    policy = {"revision": 15, "turbo_requested": requested,
              "turbo_roots": [str(root)], "denied_write_roots": []}
    access = {"policy": policy, "turbo_roots": tuple(policy["turbo_roots"]),
              "writable_workspaces": (str(workspace),), "read_only_roots": ()}
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: mode)
    monkeypatch.setattr(
        "agent_core.turbo_os_broker_client.active_broker_available",
        lambda *_args: False,
    )
    verdict = turbo_broker_admission.assess(workspace, tool="run_command")
    assert verdict["decision"] == ("BLOCKED" if mode == "on" and requested
                                   else "NORMAL")
    assert turbo_broker_admission.needs_broker("write_file") is True
