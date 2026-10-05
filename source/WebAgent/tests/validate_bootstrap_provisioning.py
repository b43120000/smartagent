#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from WebAgent import bootstrap_provisioning
from WebAgent.tool_context import WebAgentToolContext


def main() -> int:
    root = APP_ROOT.resolve()
    manifest = root / "install_smart_agent" / "POST_BOOTSTRAP_SETUP.md"
    saved: dict[str, object] = {}

    def capture(_path: Path, payload: dict) -> None:
        saved.update(payload)

    initial = {
        "schema": "SMARTAGENT_PROVISIONING_STATE_V1",
        "status": "BOOTSTRAP_READY",
        "manifest": str(manifest),
    }
    with (
        patch.object(bootstrap_provisioning, "_load", return_value=initial),
        patch.object(bootstrap_provisioning, "_save", side_effect=capture),
    ):
        assert bootstrap_provisioning.bootstrap_provisioning_needed(root) is True
        request = bootstrap_provisioning.prepare_bootstrap_request(root)

    assert "M0 bootstrap and browser launch verification already passed" in request
    assert "remaining M1-M5" in request
    assert saved["status"] == "PROVISIONING_RUNNING"
    assert saved["attempt"] == 1

    tools = WebAgentToolContext(APP_ROOT)
    tools.enable_bootstrap_install_policy(APP_ROOT)
    assert tools._bootstrap_command_argv("InstallCheckList.bat --json")[-1] == "--json"
    install_argv = tools._bootstrap_command_argv(
        "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone M3 -NonInteractive"
    )
    assert install_argv is not None
    assert "M3" in install_argv
    assert str(root / "install_smart_agent" / "install_milestones.ps1") in install_argv
    assert tools._bootstrap_command_argv("dir") == []
    rejected = tools.execute({"tool": "inspect_project_scope"})
    assert "SMARTAGENT_BOOTSTRAP_TOOL_REJECTED" in rejected

    print("BOOTSTRAP_PROVISIONING_HANDOFF_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
