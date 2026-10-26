"""Offline-only: bound-cwd planner guidance, no command execution or live sender."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import WebAgent.protocol_loop as protocol_module
from agent_core.source_attestation import _compared_code_identity
from agent_core.task_progress import initialize_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_context_acquisition import interrupted
from WebAgent.tests.validate_flexible_progress_admission import initial


def main():
    source = Path(protocol_module.__file__).resolve()
    for name in (
        "WebAgentProtocolLoop._bound_cwd_command_contract",
        "WebAgentProtocolLoop._bound_cwd_command_guidance",
        "WebAgentProtocolLoop._accept_v8_ack",
    ):
        item = _compared_code_identity(protocol_module, source, name)
        assert item["code_objects_equal"] and item["filename_verified"]
        assert item["first_line_equal"] and item["qualname_equal"], (name, item)

    with TemporaryDirectory(prefix="it20-planner-workspace-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        fixture = root / "historical_fixture"
        workspace.mkdir()
        fixture.mkdir()
        loop = WebAgentProtocolLoop(workspace, lambda *a: "", progress_root=workspace)
        loop.run_id = loop.task_id = loop.task_epoch = "R018-TEST"
        loop.intent_digest = "b" * 64
        loop.authorized_paths = [str(fixture)]
        loop.tools._authorized_local_paths = [str(fixture)]
        loop.task_request = (
            "Please use run_command to perform a brief wait; "
            "唯一 fixture 路徑 " + str(fixture) + "，短暫等待命令"
        )
        loop.progress_ledger = initialize_progress(
            "R018-TEST", request_id="R018-TEST",
            goal=loop.task_request, root=workspace,
        )
        command = "Start-Sleep -Seconds 4"
        bad = {
            "tool": "run_command", "action_id": "WAIT-REJECT",
            "operation": "PROCESS", "command": command,
            "working_directory": str(fixture), "timeout": 30,
            "success_criteria": "目錄資料已讀取",
            "verify": [{"action": "run_command", "command": "(Get-Location).Path",
                        "expect_exit_code": 0}],
        }
        allowed = {**bad, "action_id": "WAIT-BOUND",
                   "working_directory": str(workspace)}
        # The synthetic policy permits both roots at software level; the
        # runtime's bound-cwd guard must still reject explicit foreign cwd.
        with patch("agent_core.security_context._configured_access_roots",
                   return_value=((fixture,), ())):
            contract = loop._bound_cwd_command_contract(command)
            assert contract["bound_workspace"] == str(workspace)
            assert contract["bound_workspace_admitted"] is True, contract
            assert contract["separate_original_file_scope"] == [str(fixture)]
            assert contract["not_an_action_or_completed_evidence"] is True
            assert loop.tools.preview_tool_scope(allowed)["allowed"]
            bad_preview = loop.tools.preview_tool_scope(bad)
            assert not bad_preview["allowed"]
            assert "path_independent_command_cwd_outside_bound_workspace" in bad_preview["error"]
            prompt = loop._with_commit("native test prompt", {
                "run_id": "R018-TEST", "turn_id": 1, "ack_web_ack_id": ""
            })
            # Prompt contains JSON, where Windows path separators are escaped.
            # Decode the sole bounded-workspace contract; do not look for raw
            # str(Path) substrings in its transport-encoded representation.
            begin = "[RUNTIME_BOUND_COMMAND_WORKSPACE]" + chr(10)
            end = chr(10) + "[/RUNTIME_BOUND_COMMAND_WORKSPACE]"
            assert prompt.count(begin) == 1 and prompt.count(end) == 1, prompt
            embedded = prompt.split(begin, 1)[1].split(end, 1)[0]
            prompt_contract = json.loads(embedded)
            assert prompt_contract == contract, (prompt_contract, contract)
            assert prompt_contract["schema"] == "SMARTAGENT_BOUND_COMMAND_WORKSPACE_V1"
            assert prompt_contract["command"] == command
            assert prompt_contract["bound_workspace"] == str(workspace)
            assert prompt_contract["separate_original_file_scope"] == [str(fixture)]
            assert prompt_contract["bound_workspace_admitted"] is True
            assert prompt_contract["bound_workspace_error"] == ""
            assert prompt_contract["not_an_action_or_completed_evidence"] is True
            # The declared-next-action guard must remain authoritative:
            # a Progress planning list_directory cannot admit run_command.
            mismatch = [initial(), bad, {"tool": "turn_commit", "action_count": 2}]
            accepted, diagnostics = loop._accept_ack(mismatch, {"ack_web_ack_id": ""})
            assert not accepted and any(
                row.get("reason") == "declared_next_action_mismatch"
                and row.get("declared_next_tools") == ["list_directory"]
                and row.get("actual_operational_tools") == ["run_command"]
                for row in diagnostics
            ), diagnostics
            assert not loop.action_ledger and not loop.action_result_ledger
            # Now genuinely declare the action that the same turn submits.
            # This does not bypass the guard; it exercises the *next* gate,
            # which must reject the C fixture cwd before running a command.
            progress = initial()
            progress["next_action"] = "run_command"
            progress["current_focus"] = "Execute a nonwriting wait at the bound workspace"
            progress["base_evaluation"] = "Independent wait PROCESS test without fixture access"
            progress["steps"] = [
                {"step": 1, "desc": "Run command and collect process evidence", "status": "IN_PROGRESS"}
            ]
            progress["completion_contract"] = {
                "success": ["Waiting PROCESS exited with exit_code=0"],
                "failure": ["Waiting PROCESS exited with nonzero status"],
                "in_progress": ["No native process completion yet"],
                "interrupted": ["Runtime bound-workspace scope conflict"],
            }
            progress["matched_condition"] = "No native process completion yet"
            bad["success_criteria"] = progress["completion_contract"]["success"][0]
            allowed["success_criteria"] = bad["success_criteria"]
            response = [progress, bad, {"tool": "turn_commit", "action_count": 2}]
            accepted, diagnostics = loop._accept_ack(response, {"ack_web_ack_id": ""})
            assert not accepted and diagnostics, diagnostics
            assert any(row.get("reason") == "run_command_working_directory_rejected"
                       and str(workspace) in str(row.get("suggestion", ""))
                       for row in diagnostics), diagnostics
            repair = loop._pending_bound_cwd_repair
            assert repair["action_id"] == "WAIT-REJECT", repair
            assert not loop.action_ledger and not loop.action_result_ledger
            accepted, diagnostics = loop._accept_ack(interrupted({
                "kind": "BLOCKED", "detail": "the rejected command was not started",
                "blocking_action_id": "WAIT-REJECT"
            }), {"ack_web_ack_id": ""})
            assert not accepted and any(
                row.get("reason") == "path_independent_command_bound_workspace_already_available"
                and str(workspace) in str(row.get("suggestion", ""))
                for row in diagnostics
            ), diagnostics
            assert not loop.action_ledger and not loop.action_result_ledger
            # No action is dispatched by this test or by scope preview.
            assert loop.tools.preview_tool_scope(allowed)["allowed"]
    print("IT20_BOUND_WORKSPACE_PLANNER_REPAIR_OFFLINE_PASS")


if __name__ == "__main__":
    main()
