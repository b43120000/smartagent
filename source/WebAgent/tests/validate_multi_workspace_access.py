#!/usr/bin/env python3
"""Validate that the writable registry is the runtime authorization set."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core.command_security import inspect_command
from agent_core.path_security import PathSecurityError
from agent_core.security_context import SecurityContext


class FakeAgent:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self._authorized_local_paths = []
        self.current_request_id = "TEST-REQUEST"
        self.current_task_id = "TEST-TASK"
        self.interface_name = "test"


def _policy(primary: Path, secondary: Path, read_only: Path) -> dict:
    return {
        "writable_workspaces": [str(primary), str(secondary)],
        "read_only_roots": [str(read_only)],
    }


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="smartagent-multi-workspace-") as value:
        root = Path(value).resolve()
        primary = root / "primary"
        secondary = root / "secondary"
        read_only = root / "read-only"
        outside = root / "outside"
        for path in (primary, secondary, read_only, outside):
            path.mkdir()

        policy = _policy(primary, secondary, read_only)
        agent = FakeAgent(primary)
        with patch("agent_core.workspace_access.access_snapshot", return_value=policy):
            context = SecurityContext.from_agent(agent)
            assert context.workspace_root == primary
            assert context.require_write(secondary / "write.txt") == secondary / "write.txt"
            assert context.require_read(secondary / "read.txt") == secondary / "read.txt"
            assert context.require_read(read_only / "evidence.txt") == read_only / "evidence.txt"

            try:
                context.require_write(read_only / "blocked.txt")
            except PathSecurityError as exc:
                assert exc.code == "path_outside_authorized_roots", exc
            else:
                raise AssertionError("read-only root accepted for write")

            try:
                context.require_read(outside / "blocked.txt")
            except PathSecurityError as exc:
                assert exc.code == "path_outside_authorized_roots", exc
            else:
                raise AssertionError("unlisted root accepted for read")

            for workspace in (primary, secondary):
                try:
                    context.require_write(workspace / ".agents" / "security" / "state.json")
                except PathSecurityError as exc:
                    assert "runtime_owned_security_state" in str(exc), exc
                else:
                    raise AssertionError("runtime-owned security state accepted for write")

            secondary_script = secondary / "tool.ps1"
            outside_script = outside / "tool.ps1"
            allowed = inspect_command(f'& "{secondary_script}"', workspace=primary)
            rejected = inspect_command(f'& "{outside_script}"', workspace=primary)
            assert allowed.allowed and allowed.code == "authorized_workspace_executable", allowed
            assert not rejected.allowed and rejected.code == "execution_target_outside_workspace", rejected

        print("MULTI_WORKSPACE_ACCESS_OK")
        return {
            "binding_is_default_workspace": True,
            "all_writable_roots_read_write": True,
            "read_only_root_write_rejected": True,
            "unlisted_root_rejected": True,
            "writable_root_execution_allowed": True,
            "runtime_security_state_protected": True,
        }


if __name__ == "__main__":
    run()
