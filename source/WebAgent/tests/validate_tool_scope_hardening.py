#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from WebAgent.tool_context import WebAgentToolContext
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.semantic_capability_resolution import SCHEMA


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

        tools.bind_runtime_capability_resolution(
            {"schema": SCHEMA, "capabilities": ["directory.list"]},
            request_id="RUN-LIST", task_epoch=tools.current_task_epoch,
        )
        wrong_tool = tools.preview_tool_scope({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-2",
            "workspace": str(requested),
        })
        assert not wrong_tool["allowed"], wrong_tool
        assert wrong_tool["error"] == "semantic_capability_not_selected:inspect_project_scope", wrong_tool
        rejected = tools.execute({
            "tool": "inspect_project_scope",
            "action_id": "BAD-SCOPE-2",
            "workspace": str(requested),
        })
        assert rejected.startswith("[TOOL_SCOPE_REJECTED]"), rejected
        assert "semantic_capability_not_selected:inspect_project_scope" in rejected, rejected

        wrong_directory_tool = tools.preview_tool_scope({
            "tool": "inspect_directory",
            "action_id": "BAD-SCOPE-2B",
            "paths": [str(requested)],
            "recursive": False,
        })
        assert not wrong_directory_tool["allowed"], wrong_directory_tool
        assert wrong_directory_tool["error"] == "semantic_capability_not_selected:inspect_directory", wrong_directory_tool

        # A goal may genuinely require both listing and independent metadata
        # inspection. A word such as "列出" elsewhere in the same request
        # cannot cancel a separately requested read capability.
        tools.begin_run(
            "RUN-MIXED-OBSERVATIONS",
            f"看看這個路徑有哪些東西，順便整理資料夾的統計資訊，"
            f"禁止任何寫入。\\n{requested}",
            [str(requested)],
        )
        tools.bind_runtime_capability_resolution(
            {"schema": SCHEMA, "capabilities": ["directory.list", "directory.inspect_metadata"]},
            request_id="RUN-MIXED-OBSERVATIONS", task_epoch=tools.current_task_epoch,
        )
        metadata = tools.preview_tool_scope({
            "tool": "inspect_directory",
            "action_id": "GOOD-MIXED-METADATA",
            "paths": [str(requested)],
            "recursive": False,
        })
        assert metadata["allowed"], metadata
        inspected = tools.execute({
            "tool": "inspect_directory",
            "action_id": "GOOD-MIXED-METADATA",
            "paths": [str(requested)],
            "recursive": False,
        })
        assert '"file_count":1' in inspected, inspected
        outside = tools.preview_tool_scope({
            "tool": "inspect_directory",
            "action_id": "BAD-MIXED-OUTSIDE",
            "paths": [str(workspace)],
            "recursive": False,
        })
        assert not outside["allowed"], outside
        tools.begin_run(
            "RUN-MIXED-NEGATIVE",
            f"列出檔案；禁止使用 inspect_directory。\\n{requested}",
            [str(requested)],
        )
        tools.bind_runtime_capability_resolution(
            {"schema": SCHEMA, "capabilities": ["directory.list"]},
            request_id="RUN-MIXED-NEGATIVE", task_epoch=tools.current_task_epoch,
        )
        negated = tools.preview_tool_scope({
            "tool": "inspect_directory", "action_id": "BAD-NEGATED-METADATA",
            "paths": [str(requested)], "recursive": False,
        })
        assert not negated["allowed"], negated
        assert negated["error"] == "semantic_capability_not_selected:inspect_directory", negated
        tools.begin_run("RUN-LIST", f"先列出這個路徑下有哪些檔案\\n{requested}",
                        [str(requested)])

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
