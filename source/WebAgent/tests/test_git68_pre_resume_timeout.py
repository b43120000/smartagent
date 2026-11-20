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
