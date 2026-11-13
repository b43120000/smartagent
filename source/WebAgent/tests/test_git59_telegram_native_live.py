"""Git59 Telegram one-message native diagnostic must remain read-only."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.transport_message import NormalizedInboundMessage
from agent_core.git59_telegram_native_live import (
    COMMAND, prepare_message, build_requirement, MARKER,
)
from agent_core.requirement_goal_state import (
    _step, compile_requirement_goal_state,
)


def _message(text=COMMAND, **kwargs):
    fields = {
        "transport": "TELEGRAM", "endpoint": "api.telegram.org",
        "conversation_key": "telegram://chat/42", "sender_id": "42",
        "source_message_id": "10", "text": text,
        "received_at": 1.0, "idempotency_key": "tg:42:10",
        "reply_context": {"chat_id": 42, "message_id": 10},
    }
    fields.update(kwargs)
    return NormalizedInboundMessage(**fields)


def test_non_command_telegram_text_never_arms_diagnostic():
    assert prepare_message(_message("普通自然語言工作"), update_id=14,
                           project_root=Path.cwd()) is None


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_exact_command_builds_frozen_diagnostic_not_host_cmd():
    project = Path(r"C:\smartagent")
    message = prepare_message(
        _message(), update_id=20, project_root=project,
    )
    assert COMMAND not in message.text
    assert message.text.count("run_command") >= 1
    assert message.metadata[MARKER]["request_sha256"]
    assert message.metadata[MARKER]["action_authorization_granted"] is False
    goal = compile_requirement_goal_state(message.text)
    assert goal["status"] == "COMPILED", goal
    assert goal["goal_kind"] == "DIAGNOSTIC_EXECUTABLE_RESOLUTION"
    assert goal["authorizes_operation"] is False
    import re
    matched = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^[1-6]\.\s*(.+)$", message.text)
    ]
    assert len(matched) == 6
    assert [_step(command, goal["executable_targets"])
            for command in matched] == [
        "path", "git_exists", "python_exists", "resolve",
        "git_version", "python_version",
    ]


@pytest.mark.parametrize("change", [
    {"transport": "WEBGPT_COPILOT"},
    {"sender_id": "not-numeric"},
])
@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_untrusted_transport_cannot_use_control(change):
    with pytest.raises(ValueError, match="GIT59_AUTHENTICATED_TELEGRAM_REQUIRED"):
        prepare_message(_message(**change), update_id=20,
                        project_root=Path(r"C:\smartagent"))


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_attachments_disallowed():
    from agent_core.transport_message import InboundAttachment
    with pytest.raises(ValueError, match="GIT59_ATTACHMENTS_FORBIDDEN"):
        prepare_message(_message(
            attachments=(InboundAttachment(kind="document", transport_id="abc"),)
        ), update_id=20, project_root=Path(r"C:\smartagent"))



def test_git59_separate_native_child_verdict_never_inferred_from_green_goal():
    import hashlib
    from types import SimpleNamespace
    from agent_core.git59_telegram_native_live import assess_native_child_live
    from WebAgent.tests.test_git55_requirement_goal_state_contract import REQUEST
    goal = compile_requirement_goal_state(REQUEST)
    marker = {
        "schema": "SMARTAGENT_GIT59_NATIVE_EXEC_LIVE_SELECTOR_V1",
        "original_command": COMMAND,
        "request_sha256": hashlib.sha256(REQUEST.encode("utf-8")).hexdigest(),
        "source_message_id": "10",
        "action_authorization_granted": False,
    }
    task = SimpleNamespace(
        task_id="TASK-GIT59-FAKE", request_id="RR-GIT59-FAKE",
        request=REQUEST,
        metadata={"source_message_id": "10",
                  "ingress_metadata": {MARKER: marker}},
    )
    rows = []
    for key in ("D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09"):
        row = {"condition_id": key, "verdict": "PASS",
               "reason_code": "OTHER", "actual": {}, "evidence_refs": []}
        if key in {"D07", "D08"}:
            name = "git" if key == "D07" else "python"
            row.update({
                "reason_code": "NATIVE_CHILD_EXECUTION_VERIFIED",
                "actual": {
                    "native_executable_identity_verified": True,
                    "native_executable_probe_name": name,
                    "native_executable_probe_pid": 250,
                    "native_executable_probe_exit_code": 0,
                    "native_executable_direct_launch_count": 1,
                    "native_executable_probe_is_second_process": True,
                    "exit_code": 0, "stdout": name + " version 1",
                },
                "evidence_refs": ["MIN-EV-" + ("a" * 24 if name == "git"
                                              else "b" * 24)],
            })
        rows.append(row)
    terminal = {
        "final_verdict": "PASS",
        "goal_contract_sha256": goal["contract_sha256"],
        "comparison": {"reason_code": "FROZEN_GOAL_STATE_SATISFIED"},
        "goal_state_table": rows,
    }
    loop = SimpleNamespace(terminal_acceptance=terminal)
    positive = assess_native_child_live(task, loop, source_transport="TELEGRAM")
    assert positive["verdict"] == "GIT59_NATIVE_CHILD_EXECUTION_PASS"
    terminal["goal_state_table"][7]["actual"]["native_executable_identity_verified"] = False
    # A green diagnostic is NOT positive executable evidence.
    negative = assess_native_child_live(task, loop, source_transport="TELEGRAM")
    assert negative["diagnostic_goal_verdict"] == "PASS"
    assert negative["verdict"] == "GIT59_NATIVE_CHILD_EXECUTION_UNVERIFIED"
    assert negative["python_direct_child_verified"] is False
    with pytest.raises(ValueError, match="GIT59_DIAGNOSTIC_TASK_BOUNDARY_INVALID"):
        assess_native_child_live(task, loop, source_transport="WEBGPT_COPILOT")
