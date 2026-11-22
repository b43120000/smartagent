"""Git52: natural language is data; only actual Actions reach ACL/OS gates.

Cross-platform offline tests. Windows-only explicit drive directives retain
the exact Win32 path and must still pass Edit Workspace authorization.
"""
from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import task_workspace_resolver as resolver
from agent_core.path_security import PathSecurityError
from agent_core.security_context import SecurityContext
from agent_core.semantic_path_resolution import analyze_request_paths
from agent_core.runtime_finish_status_repair import diagnose_finish_status_mismatch


class Store:
    def process_lock(self):
        return nullcontext()
    def load(self):
        return []
    def all(self):
        return []


def select(text: str) -> dict:
    return resolver.resolve_task_workspace(
        text, conversation_key="telegram://chat/200",
        transport="TELEGRAM", sender_id="100", store=Store(),
    )


def test_chat_message_paths_are_not_workspace_authority(monkeypatch, tmp_path):
    workspace = tmp_path / "authorized"
    workspace.mkdir()
    monkeypatch.setattr(resolver, "_policy_roots", lambda: ((workspace,), ()))
    mentioned = r"C:\smartagent\.venv\git\cmd"
    selection = select("你好，檢查 " + mentioned + " 是否存在")
    assert selection["workspace"] == str(workspace)
    assert selection["source"] == "WRITABLE_PRIMARY_DEFAULT"
    assert selection["explicit_paths"] == []
    assert mentioned in selection["mentioned_paths"]
    # No ACL bypass: pure text is accepted, while concrete Action still
    # requires a separately policy-authorized path.


@pytest.mark.skipif(os.name != "nt", reason="Windows-drive selection requires Win32 Path")
def test_only_explicit_workspace_directive_can_select(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    monkeypatch.setattr(resolver, "_policy_roots", lambda: ((allowed,), ()))
    explicit = select("Workspace: " + str(allowed))
    assert explicit["source"] == "EXPLICIT_WORKSPACE_DIRECTIVE"
    assert explicit["workspace"] == str(allowed)
    with pytest.raises(ValueError, match="request_path_outside_authorized_roots"):
        select("Workspace: " + str(other))
    with pytest.raises(ValueError, match="request_explicit_workspace_ambiguous"):
        select("Workspace: " + str(allowed) + "\nWorkspace: " + str(other))
    # A path included in a normal sentence is never an explicit override.
    assert select("請檢查 " + str(other))["workspace"] == str(allowed)


def test_request_text_never_grants_extra_read_roots(tmp_path, monkeypatch):
    root, outside = tmp_path / "workspace", tmp_path / "private"
    root.mkdir()
    outside.mkdir()
    from agent_core import workspace_access
    monkeypatch.setattr(workspace_access, "access_snapshot",
                        lambda: {"writable_workspaces": (str(root),),
                                 "read_only_roots": ()})
    agent = SimpleNamespace(
        workspace_root=root,
        _authorized_local_paths=(str(outside),),
        current_task_id="TASK-READ-NO", current_request_id="RR-READ-NO",
        interface_name="telegram",
    )
    context = SecurityContext.from_agent(agent)
    assert context.require_read(root / "okay.txt") == root / "okay.txt"
    with pytest.raises(PathSecurityError):
        context.require_read(outside / "private.txt")


def test_planner_prose_does_not_stat_path_outside_edit_workspace(
    tmp_path, monkeypatch,
):
    writable, outsider = tmp_path / "workspace", tmp_path / "private"
    writable.mkdir()
    outsider.mkdir()
    from agent_core import workspace_access
    monkeypatch.setattr(workspace_access, "access_snapshot",
                        lambda: {"writable_workspaces": (str(writable),),
                                 "read_only_roots": ()})
    original_exists = Path.exists
    def safe_exists(self):
        if self == outsider or outsider in self.parents:
            raise AssertionError("UNAUTHORIZED_PATH_PROBED_BY_PLANNER")
        return original_exists(self)
    monkeypatch.setattr(Path, "exists", safe_exists)
    raw = str(outsider)
    goal = "請確認 " + raw + "是否存在"
    result = analyze_request_paths(
        goal, [raw], allow_readonly_narrowing=True,
    )
    assert result["grants_authorization"] is False


def test_finish_repair_maps_unverified_without_faking_pass():
    comparison = {
        "reason_code": "MODEL_RUNTIME_MISMATCH",
        "mismatches": [{
            "evidence_ref": "MIN-EV-VERIFY123",
            "field": "data.verification_status",
            "expected": "PASS", "actual": "UNVERIFIED",
        }],
    }
    repair = diagnose_finish_status_mismatch(comparison)
    assert repair["repair_only"] is True
    assert repair["evidence_unmodified"] is True
    assert repair["corrections"][0]["runtime_actual_status"] == "UNVERIFIED"
    assert repair["corrections"][0]["missing_postrun_evidence"] is True
    assert diagnose_finish_status_mismatch({
        "reason_code": "GOAL_CHECK_RUNTIME_MISMATCH",
        "mismatches": comparison["mismatches"],
    }) is None


def test_actual_proven_read_and_write_security_still_apply(tmp_path, monkeypatch):
    root, outside = tmp_path / "allowed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    from agent_core import workspace_access
    monkeypatch.setattr(workspace_access, "access_snapshot",
                        lambda: {"writable_workspaces": (str(root),),
                                 "read_only_roots": ()})
    agent = SimpleNamespace(workspace_root=root, _authorized_local_paths=())
    context = SecurityContext.from_agent(agent)
    assert context.require_write(root / "test.txt") == root / "test.txt"
    with pytest.raises(PathSecurityError):
        context.require_write(outside / "test.txt")


def test_failed_supervisor_security_preflight_cannot_block_chat_ingress():
    # The disabled marker remains set for later Runtime actions. Telegram
    # receives/queues normal text even while the OS executor is unavailable.
    from agent_core.host_supervisor import HostSupervisor
    supervisor = object.__new__(HostSupervisor)
    supervisor.security_mutation_disabled = True
    supervisor.security_preflight_report = {
        "status": "FAIL",
        "attestation": {"reasons": ["NATIVE_BROKER_NOT_READY"]},
    }
    admitted, reason = supervisor._remote_task_security_admission(None)
    assert admitted is True
    assert reason == "INGRESS_ONLY_ACTION_GATED_SECURITY"
    assert supervisor.security_mutation_disabled is True


def test_native_pinned_tool_resolution_keeps_host_fallback_forbidden():
    launcher = (Path(__file__).resolve().parents[3] /
                "install_smart_agent" / "turbo_appcontainer_probe.cs").read_text(
                    encoding="utf-8")
    assert "string trustedAliases" in launcher
    assert "GIT52_NATIVE_EXECUTABLE_NOT_VISIBLE_" in launcher
    assert "Set-Alias -Name " in launcher
    assert "LaunchPowerShell(moniker, wrapped, root" in launcher
    assert 'Environment.GetEnvironmentVariable("PATH")' not in launcher


def test_goal_check_unverified_requests_new_independent_action():
    from agent_core.runtime_finish_status_repair import (
        diagnose_missing_postrun_evidence,
    )
    result = diagnose_missing_postrun_evidence({
        "reason_code": "GOAL_CHECK_RUNTIME_MISMATCH",
        "mismatches": [{
            "criterion": "confirm test file absent",
            "evidence_ref": "MIN-EV-ROUND14",
            "field": "data.verification_status",
            "operator": "eq", "expected": "PASS",
            "actual": "UNVERIFIED",
        }],
    })
    assert result["repair_class"] == "ACTION_POSTRUN_VERIFICATION_REQUIRED"
    assert result["new_tool_result_required"] is True
    assert result["repair_only"] is False
    assert result["evidence_unmodified"] is True
    assert diagnose_missing_postrun_evidence({
        "reason_code": "GOAL_CHECK_RUNTIME_MISMATCH",
        "mismatches": [{
            "evidence_ref": "MIN-EV-ROUND14",
            "field": "data.verification_status",
            "expected": "FAIL", "actual": "UNVERIFIED",
        }],
    }) is None
