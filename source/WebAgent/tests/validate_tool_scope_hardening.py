#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from WebAgent.tool_context import WebAgentToolContext
from agent_core.smartagent_protocol import validate_tool_envelope


def main() -> int:
    valid, diagnostic = validate_tool_envelope({
        "tool": "inspect_project_scope",
        "action_id": "BAD-SCOPE-1",
        "project_root": r"C:\work",
        "path": r"C:\work\child",
        "depth": 1,
        "include_files": True,
    })
    assert not valid
    assert diagnostic and diagnostic.get("reason") == "unexpected_field", diagnostic
    assert "path" in str(diagnostic.get("detail", "")), diagnostic

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp).resolve()
        requested = workspace / "requested"
        requested.mkdir()
        (requested / "one.txt").write_text("one", encoding="utf-8")

        tools = WebAgentToolContext(workspace)
        tools.begin_run(
            "RUN-LIST",
            f"先列出這個路徑下有哪些檔案\n{requested}",
            [str(requested)],
        )

        wrong_tool = tools.preview_tool_scope({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-2",
            "workspace": str(requested),
        })
        assert not wrong_tool["allowed"], wrong_tool
        assert wrong_tool["error"] == "directory_listing_requires_list_directory", wrong_tool
        rejected = tools.execute({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-2",
            "workspace": str(requested),
        })
        assert rejected.startswith("[TOOL_SCOPE_REJECTED]"), rejected
        assert "directory_listing_requires_list_directory" in rejected, rejected

        wrong_directory_tool = tools.preview_tool_scope({
            "tool": "inspect_directory",
            "action_id": "BAD-SCOPE-2B",
            "paths": [str(requested)],
            "recursive": False,
        })
        assert not wrong_directory_tool["allowed"], wrong_directory_tool
        assert wrong_directory_tool["error"] == "directory_listing_requires_list_directory", wrong_directory_tool

        missing_path = tools.preview_tool_scope({
            "tool": "list_directory",
            "action_id": "BAD-SCOPE-3",
        })
        assert not missing_path["allowed"], missing_path
        assert missing_path["error"] == "explicit_path_scope_requires_path", missing_path

        exact = tools.preview_tool_scope({
            "tool": "list_directory",
            "action_id": "GOOD-SCOPE-1",
            "path": str(requested),
        })
        assert exact["allowed"], exact
        assert exact["resolved_paths"] == [str(requested)], exact
        listing = tools.execute({
            "tool": "list_directory",
            "action_id": "GOOD-SCOPE-1",
            "path": str(requested),
        })
        assert "one.txt" in listing, listing

        tools.begin_run(
            "RUN-SNAPSHOT",
            f"分析這個專案的結構\n{requested}",
            [str(requested)],
        )
        fallback = tools.preview_tool_scope({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-4",
        })
        assert not fallback["allowed"], fallback
        assert fallback["error"] == "explicit_path_scope_requires_workspace", fallback

        expanded = tools.preview_tool_scope({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-5",
            "workspace": str(workspace),
        })
        assert not expanded["allowed"], expanded
        assert expanded["error"].startswith("resolved_scope_expands_beyond_explicit_path:"), expanded

        exact_snapshot = tools.preview_tool_scope({
            "tool": "inspect_project_scope",
            "action_id": "GOOD-SCOPE-2",
            "workspace": str(requested),
        })
        assert exact_snapshot["allowed"], exact_snapshot
        assert exact_snapshot["resolved_paths"] == [str(requested)], exact_snapshot

    print("TOOL_SCOPE_HARDENING_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
