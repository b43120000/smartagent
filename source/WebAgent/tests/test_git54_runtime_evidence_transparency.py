"""Git54: Runtime source evidence stays readable under every execution mode.

Mocks verify data contracts, NOT a Windows kernel AppContainer attestation.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.command_operation import build_operation_result
from agent_core.executor_run_blackboard import (
    append_executor_event, begin_executor_run, projection,
    read_executor_event, read_executor_run,
)
from agent_core.runtime_blackboard_reader import read_runtime_blackboard
from agent_core.runtime_evidence_store import (
    RuntimeEvidenceError, persist_runtime_evidence, read_runtime_evidence,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_capabilities import get_allowed_tools
from agent_core.tool_result_contract import decode_result
from agent_core import tools
from WebAgent.pokeclaw_executor_loop import _tool_result


TASK = "TASK-GIT54-TEST"
REQUEST = "RR-GIT54-TEST"
EPOCH = "EPOCH-GIT54-TEST"
ACTION = "MIN-GIT54-TEST"
REF = "MIN-EV-" + "c" * 24


def _persist(root, output, **kwargs):
    data = {"stdout": output, "stderr": "", "exit_code": 0,
            "stdout_bytes": len(output.encode("utf-8")),
            "stderr_bytes": 0, "output_complete": True}
    receipt = {"status": "SUCCEEDED", "executed": True, "reason": "COMMAND_EXITED",
               "data": data}
    raw = "[COMMAND_RESULT]\nstdout:\n" + output + "\n[RUNTIME_TOOL_RESULT] {}"
    return persist_runtime_evidence(
        root, task_id=kwargs.get("task_id", TASK),
        request_id=REQUEST, task_epoch=EPOCH,
        action_id=ACTION, evidence_ref=REF,
        raw_result=raw, receipt=receipt,
    )


def test_small_stdout_is_directly_typed_and_stderr_preserved():
    result = build_operation_result(
        "INSPECT",
        command=r"Test-Path -LiteralPath 'C:\smartagent\.venv\Scripts\python.exe'",
        execution_status="SUCCEEDED", verification_status="PASS",
        stdout="True", stderr="", stdout_bytes=4, stderr_bytes=0,
    )
    assert result["stdout"] == "True"
    assert result["observed_boolean"] is True
    assert result["observation_value_confirmed"] is True
    assert result["stdout_inline_complete"] is True
    assert result["stdout_bytes"] == 4
    assert result["output_complete"] is True
    false = build_operation_result(
        "INSPECT", command="Test-Path -LiteralPath 'missing'",
        execution_status="SUCCEEDED", verification_status="PASS",
        stdout="False", stderr="unrelated diagnostic",
    )
    assert false["observed_boolean"] is False
    assert false["stderr"] == "unrelated diagnostic"


def test_non_boolean_output_is_not_turned_into_an_observation():
    result = build_operation_result(
        "INSPECT", command="Get-Command git, python -ErrorAction SilentlyContinue",
        execution_status="SUCCEEDED", verification_status="UNVERIFIED",
        stdout="", stderr="",
    )
    assert "observed_boolean" not in result
    assert result["stdout"] == ""


def test_complete_raw_result_persisted_and_paged_without_data_loss(tmp_path):
    payload = "Window-Environment: " + "A" * 13500
    pointer = _persist(tmp_path, payload)
    assert pointer["store_status"] == "PERSISTED_AND_READBACK_VERIFIED"
    parts, offset = [], 0
    while True:
        row = read_runtime_evidence(
            tmp_path, task_id=TASK, request_id=REQUEST, task_epoch=EPOCH,
            evidence_ref=REF, section="stdout", offset=offset, max_chars=2400)
        parts.append(row["content"])
        if row["complete"]:
            break
        assert row["next_offset"] > offset
        offset = row["next_offset"]
    assert "".join(parts) == payload
    meta = read_runtime_evidence(
        tmp_path, task_id=TASK, request_id=REQUEST, task_epoch=EPOCH,
        evidence_ref=REF, section="metadata")
    assert meta["task_bound"] is True
    assert meta["source_sha256"]
    text = read_runtime_evidence(
        tmp_path, task_id=TASK, request_id=REQUEST, task_epoch=EPOCH,
        evidence_ref=REF, section="raw_result")
    assert text["content"].startswith("[COMMAND_RESULT]")


def test_evidence_immutable_and_cross_request_refused(tmp_path):
    pointer = _persist(tmp_path, "True")
    assert _persist(tmp_path, "True") == pointer
    with pytest.raises(RuntimeEvidenceError, match="IMMUTABLE_CONFLICT"):
        _persist(tmp_path, "False")
    with pytest.raises(RuntimeEvidenceError, match="REQUEST_BINDING"):
        read_runtime_evidence(tmp_path, task_id=TASK, request_id="RR-OTHER",
                              task_epoch=EPOCH, evidence_ref=REF)
    with pytest.raises(RuntimeEvidenceError):
        read_runtime_evidence(tmp_path, task_id="../other", request_id=REQUEST,
                              task_epoch=EPOCH, evidence_ref=REF)
    with pytest.raises(RuntimeEvidenceError):
        read_runtime_evidence(tmp_path, task_id=TASK, request_id=REQUEST,
                              task_epoch=EPOCH, evidence_ref="../secret")


def test_planner_read_action_is_task_bound_and_never_executes_host(tmp_path, monkeypatch):
    _persist(tmp_path, "True")
    agent = SimpleNamespace(
        _runtime_evidence_root=tmp_path, current_task_id=TASK,
        current_request_id=REQUEST, current_task_epoch=EPOCH,
    )
    monkeypatch.setattr(tools, "_run_powershell_capture",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("read_tool_must_not_execute_command")))
    result = tools.execute_tool({
        "tool": "read_runtime_evidence", "evidence_ref": REF,
        "section": "stdout", "offset": 0, "max_chars": 150,
    }, agent)
    outcome = decode_result(result)
    assert outcome["status"] == "SUCCEEDED"
    assert outcome["data"]["content"] == "True"
    assert outcome["data"]["authorization_granted"] is False
    agent.current_request_id = "RR-OTHER"
    outcome = decode_result(tools.execute_tool({
        "tool": "read_runtime_evidence", "evidence_ref": REF,
        "section": "stdout",
    }, agent))
    assert outcome["status"] == "REJECTED"


@pytest.mark.parametrize("tool", ("read_runtime_evidence", "read_runtime_blackboard"))
def test_read_tools_registered_in_all_interfaces_and_never_native_mutators(tool):
    from agent_core.turbo_broker_admission import assess, needs_broker
    assert tool in get_allowed_tools("remote")
    assert tool in get_allowed_tools("local")
    assert not needs_broker(tool)
    assert assess(tool=tool)["decision"] == "NORMAL"


def test_read_envelope_rejects_unbounded_offset_and_cross_path_fields():
    accepted, error = validate_tool_envelope({
        "tool": "read_runtime_evidence", "evidence_ref": REF,
        "offset": 0, "max_chars": 2400, "section": "stdout",
    })
    assert accepted, error
    invalid = [
        {"tool": "read_runtime_evidence", "evidence_ref": "../../secret"},
        {"tool": "read_runtime_evidence", "evidence_ref": REF,
         "offset": -1},
        {"tool": "read_runtime_evidence", "evidence_ref": REF,
         "max_chars": 100000},
        {"tool": "read_runtime_evidence", "evidence_ref": REF,
         "path": "C:\\private"},
        {"tool": "read_runtime_blackboard", "section": "event",
         "sequence": -1},
    ]
    for action in invalid:
        ok, _ = validate_tool_envelope(action)
        assert not ok, action


def test_truncated_planner_projection_retains_readable_source_pointer():
    payload = {"stdout": "A" * 18000, "stderr": "", "exit_code": 0}
    pointer = {"store_status": "PERSISTED_AND_READBACK_VERIFIED", "evidence_ref": REF}
    result = _tool_result("SUCCEEDED", "COMMAND_EXITED", payload,
                          evidence_pointer=pointer)
    assert result["isSuccess"]
    assert '"truncated":true' in result["data"]
    assert REF in result["data"]
    assert '"complete_result_available_in_evidence_store":true' in result["data"]


@pytest.mark.parametrize("acl,turbo,backend", [
    ("off", "off", "LEGACY"),
    ("off", "on", "LEGACY"),
    ("on", "off", "RESTRICTED"),
    ("on", "on", "WIN32_APPCONTAINER"),
])
def test_four_mode_results_share_same_stdout_stderr_contract(
    tmp_path, monkeypatch, acl, turbo, backend,
):
    monkeypatch.setattr(tools, "resolve_command_working_directory",
                        lambda *a, **kw: tmp_path)
    monkeypatch.setattr(tools, "_run_powershell_capture",
                        lambda *a, **kw: {
                            "stdout": "True", "stderr": "diagnostic",
                            "stdout_bytes": 4, "stderr_bytes": 10,
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "exit_code": 0,
                            "sandbox_evidence": {"sandbox_backend": backend},
                        })
    result = tools.tool_run_command(
        r"Test-Path -LiteralPath 'C:\smartagent\.venv\Scripts\python.exe'",
        "INSPECT", working_directory=tmp_path,
    )
    receipt = decode_result(result)
    assert receipt["status"] == "SUCCEEDED", (acl, turbo, result)
    data = receipt["data"]
    assert data["stdout"] == "True"
    assert data["stderr"] == "diagnostic"
    assert data["observed_boolean"] is True
    assert data["stdout_bytes"] == 4
    assert data["output_complete"] is True
    assert data["sandbox_evidence"]["sandbox_backend"] == backend
    assert data["terminal_eligible"] is True
    # A command exit is not proof that the entire requested user goal passed.
    assert data["verification_status"] == "UNVERIFIED"


def test_failed_evidence_persist_never_claims_readable_pointer():
    pointer = {"store_status": "PERSIST_FAILED", "evidence_ref": REF}
    successful = _tool_result(
        "SUCCEEDED", "COMMAND_EXITED", {"stdout": "A" * 12000},
        evidence_pointer=pointer,
    )
    assert '"complete_result_available_in_evidence_store":false' in successful["data"]
    assert '"evidence_delivery_status":"PERSIST_FAILED"' in successful["data"]
    assert '"read_tool":null' in successful["data"]
    failed = _tool_result(
        "FAILED", "COMMAND_EXITED", {"stdout": "", "stderr": "ACCESS_DENIED", "exit_code": 1},
        evidence_pointer=pointer,
    )
    assert "evidence_delivery_status=PERSIST_FAILED" in failed["error"]
    assert "read_tool=read_runtime_evidence" not in failed["error"]


def test_canonical_protocol_and_executor_use_same_action_evidence_reference():
    # A regression guard against the literal backslash-zero instead of
    # the NUL byte used in the historical MIN-EV digest contract.
    root = Path(__file__).resolve().parents[2]
    executor = (root / "WebAgent" / "pokeclaw_executor_loop.py").read_text(encoding="utf-8")
    common = (root / "WebAgent" / "protocol_loop.py").read_text(encoding="utf-8")
    assert 'action["action_id"] + "\\0" + digest' in executor
    assert 'action_id + "\\0" + _result_digest' in common
    assert 'action_id + "\\\\0" + _result_digest' not in common


def test_blackboard_history_archives_without_loss_and_supports_paged_read(tmp_path):
    loop = SimpleNamespace(task_id=TASK, run_id=REQUEST, task_epoch=EPOCH)
    begin_executor_run(tmp_path, loop, "check source evidence")
    for index in range(1, 151):
        append_executor_event(tmp_path, loop, {
            "round": index, "kind": "TOOL_RESULT",
            "message": "history " + str(index),
        })
    board = read_executor_run(
        tmp_path, task_id=TASK, request_id=REQUEST, task_epoch=EPOCH)
    assert board["event_count_total"] == 150
    assert len(board["events"]) < 128
    assert len(board["archive_chunks"]) > 0
    assert read_executor_event(
        tmp_path, task_id=TASK, request_id=REQUEST,
        task_epoch=EPOCH, sequence=1)["message"] == "history 1"
    assert read_executor_event(
        tmp_path, task_id=TASK, request_id=REQUEST,
        task_epoch=EPOCH, sequence=150)["message"] == "history 150"
    assert projection(board)["event_count"] == 150
    index = read_runtime_blackboard(
        tmp_path, task_id=TASK, request_id=REQUEST,
        task_epoch=EPOCH, section="events_index", max_chars=2400)
    assert '"event_count_total":150' in index["content"]
    row = read_runtime_blackboard(
        tmp_path, task_id=TASK, request_id=REQUEST,
        task_epoch=EPOCH, section="event", sequence=1, max_chars=2400)
    assert '"message":"history 1"' in row["content"]


def test_blackboard_reader_rejects_wrong_request(tmp_path):
    loop = SimpleNamespace(task_id=TASK, run_id=REQUEST, task_epoch=EPOCH)
    begin_executor_run(tmp_path, loop, "check result")
    with pytest.raises(ValueError, match="REQUEST_BINDING"):
        read_runtime_blackboard(tmp_path, task_id=TASK, request_id="RR-OTHER",
                                task_epoch=EPOCH, section="metadata")


def test_native_wrapper_source_separates_streams_and_snapshots_status():
    root = Path(__file__).resolve().parents[3]
    source = (root / "install_smart_agent" /
              "turbo_appcontainer_probe.cs").read_text(encoding="utf-8")
    assert "$commandSucceeded=$?;" in source
    assert source.index("$commandSucceeded=$?;") < source.index(
        "$text=[string]::Join([Environment]::NewLine,$outList.ToArray());")
    assert "System.Management.Automation.ErrorRecord" in source
    assert "string wrapped =" in source
    assert "} finally { \" + putOut + putErr + \" }" in source
