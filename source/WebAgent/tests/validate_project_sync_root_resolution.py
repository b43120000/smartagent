#!/usr/bin/env python3
"""Regression checks for project_sync target/container root semantics."""
from __future__ import annotations

import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_core.project_sync import inspect_project_scope
from agent_core.tools import _project_sync_workspace, execute_tool


class FakeAgent:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self._authorized_local_paths = []
        self.current_request_id = "TEST-REQUEST"
        self.current_task_id = "TEST-TASK"
        self.interface_name = "test"


def _expect_conflict(tool_call: dict, agent: FakeAgent, reason: str) -> None:
    try:
        _project_sync_workspace(tool_call, agent)
    except ValueError as exc:
        assert str(exc) == reason, exc
    else:
        raise AssertionError(f"expected conflict: {tool_call}")


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="smartagent-project-sync-root-") as value:
        authorized = Path(value).resolve()
        container = authorized / "remoteagent"
        project = container / "release" / "SmartAgentv1"
        sibling = authorized / "other-project"
        project.mkdir(parents=True)
        sibling.mkdir()
        agent = FakeAgent(authorized)

        resolved = _project_sync_workspace(
            {"project_root": str(project), "workspace": str(container)}, agent
        )
        assert Path(resolved) == project

        assert Path(_project_sync_workspace({"workspace": str(project)}, agent)) == project
        assert Path(
            _project_sync_workspace(
                {"project_root": str(project), "path": str(project)}, agent
            )
        ) == project
        assert Path(
            _project_sync_workspace(
                {"path": str(project), "workspace": str(container)}, agent
            )
        ) == project

        with patch("agent_core.tools.build_atomic_project_sync") as build:
            build.return_value = {"status": "INCOMPLETE"}
            payload = execute_tool(
                {
                    "tool": "project_sync",
                    "strategy": "FULL_BUNDLE",
                    "project_root": str(project),
                    "workspace": str(container),
                },
                agent=agent,
            )
            assert "INCOMPLETE" in payload
            assert Path(build.call_args.args[0]) == project

        _expect_conflict(
            {"project_root": str(project), "path": str(sibling)},
            agent,
            "project_sync_root_conflict:project_root_and_path",
        )
        _expect_conflict(
            {"project_root": str(sibling), "workspace": str(container)},
            agent,
            "project_sync_root_conflict:target_outside_workspace",
        )

        with tempfile.TemporaryDirectory(prefix="smartagent-project-sync-outside-") as outside_value:
            outside = Path(outside_value).resolve()
            for tool_call in (
                {"project_root": str(outside), "workspace": str(container)},
                {"project_root": str(project), "workspace": str(outside)},
            ):
                try:
                    _project_sync_workspace(tool_call, agent)
                except ValueError as exc:
                    assert str(exc).startswith("workspace_not_authorized:"), exc
                else:
                    raise AssertionError(f"outside authorization root accepted: {tool_call}")

            external_file = outside / "must-not-sync.txt"
            external_file.write_text("outside authorized project", encoding="utf-8")
            junction = project / "external-junction"
            command = f'mklink /J "{junction}" "{outside}"'
            created = subprocess.run(
                f'cmd.exe /d /c {command}',
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if created.returncode != 0 or not junction.exists():
                raise AssertionError(f"junction regression setup failed: {created.stderr or created.stdout}")
            try:
                snapshot = inspect_project_scope(project)
                assert not any(
                    str(item.get("path", "")).startswith("external-junction/")
                    for item in snapshot.get("files", [])
                ), snapshot.get("files", [])
            finally:
                junction.rmdir()

    result = {
        "project_inside_workspace_container": True,
        "workspace_only_compatibility": True,
        "project_path_alias_match": True,
        "legacy_path_inside_workspace_container": True,
        "dispatch_uses_project_root": True,
        "outside_authorization_rejected": True,
        "junction_traversal_rejected": True,
        "unrelated_roots_fail_closed": True,
    }
    print("PROJECT_SYNC_ROOT_RESOLUTION_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
