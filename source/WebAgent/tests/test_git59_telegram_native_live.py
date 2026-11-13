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
