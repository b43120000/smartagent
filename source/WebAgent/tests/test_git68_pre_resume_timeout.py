"""Git68: pre-resume auto approval only after delivered UI + 600s."""
from __future__ import annotations
from unittest.mock import patch
from agent_core.resume_command_approval import ResumeCommandGate
from WebAgent.tests.test_git40_software_resume_approval import setup_source


def _begin(tmp_path):
    root = tmp_path
    workspace = root / "work"
    workspace.mkdir()
    owner = "telegram:123:456"
    source, _, _ = setup_source(root, owner, workspace)
    gate = ResumeCommandGate(root)
    with patch("agent_core.resume_command_approval.time.time", return_value=1000.0):
        choice = gate.begin(
            request=f"接續工作 {source}", owner=owner,
            workspace=str(workspace), channel_id="456",
            transport="TELEGRAM", incoming={"text": "resume"},
        )
    return gate, workspace, choice


def test_delivered_timeout_claimed_once(tmp_path):
    gate, workspace, choice = _begin(tmp_path)
    with patch("agent_core.resume_command_approval.time.time", return_value=1001.0):
        gate.mark_notified(choice["approval_id"])
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1599.0
    ):
        assert gate.take_timed_out_telegram(workspace=str(workspace)) == []
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        rows = gate.take_timed_out_telegram(workspace=str(workspace))
        assert len(rows) == 1
        assert rows[0]["approval_id"] == choice["approval_id"]
        assert gate.take_timed_out_telegram(workspace=str(workspace)) == []


def test_unsent_or_policy_off_cannot_timeout_approve(tmp_path):
    gate, workspace, choice = _begin(tmp_path)
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_telegram(workspace=str(workspace)) == []
    gate.mark_notified(choice["approval_id"])
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": False}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_telegram(workspace=str(workspace)) == []


def test_webcopilot_timeout_requires_shown_ui_and_same_owner(tmp_path):
    workspace = tmp_path / "web-work"
    workspace.mkdir()
    owner = "webcopilot:1234abc"
    source, _, _ = setup_source(tmp_path, owner, workspace)
    gate = ResumeCommandGate(tmp_path)
    with patch("agent_core.resume_command_approval.time.time", return_value=1000.0):
        challenge = gate.begin(
            request=f"接續工作 {source}", owner=owner,
            workspace=str(workspace), channel_id="web-conversation",
            transport="WEBGPT_COPILOT",
            incoming={"raw_request": f"接續工作 {source}",
                      "conversation_url": "https://chatgpt.com/c/test",
                      "turn_index": 1},
        )
    approval_id = challenge["approval_id"]
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_webcopilot(
            approval_id, owner=owner, workspace=str(workspace)) is None
    with patch("agent_core.resume_command_approval.time.time", return_value=1001.0):
        gate.mark_notified(approval_id)
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1599.0
    ):
        assert gate.take_timed_out_webcopilot(
            approval_id, owner=owner, workspace=str(workspace)) is None
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": False}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_webcopilot(
            approval_id, owner=owner, workspace=str(workspace)) is None
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_webcopilot(
            approval_id, owner="webcopilot:wrong", workspace=str(workspace)) is None
        decided = gate.take_timed_out_webcopilot(
            approval_id, owner=owner, workspace=str(workspace))
        assert decided is not None
        assert decided["state"] == "APPROVED"
        assert gate.take_timed_out_webcopilot(
            approval_id, owner=owner, workspace=str(workspace)) is None


def test_webcopilot_explicit_rejection_wins(tmp_path):
    workspace = tmp_path / "other-web-work"
    workspace.mkdir()
    owner = "webcopilot:otheruser"
    source, _, _ = setup_source(tmp_path, owner, workspace)
    gate = ResumeCommandGate(tmp_path)
    with patch("agent_core.resume_command_approval.time.time", return_value=1000.0):
        challenge = gate.begin(
            request=f"接續工作 {source}", owner=owner,
            workspace=str(workspace), channel_id="web-conversation2",
            transport="WEBGPT_COPILOT", incoming={"raw_request": "resume"},
        )
        gate.mark_notified(challenge["approval_id"])
    with patch("agent_core.resume_command_approval.time.time", return_value=1002.0):
        rejected = gate.decide(
            challenge["approval_id"], allowed=False,
            owner=owner, channel_id="web-conversation2",
        )
        assert rejected["state"] == "REJECTED"
    with patch("agent_core.auto_approve_mode.evaluate_auto_approve",
               return_value={"approved": True}), patch(
        "agent_core.resume_command_approval.time.time", return_value=1601.0
    ):
        assert gate.take_timed_out_webcopilot(
            challenge["approval_id"], owner=owner, workspace=str(workspace)) is None
