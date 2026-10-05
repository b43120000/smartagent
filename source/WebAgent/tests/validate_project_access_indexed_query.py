#!/usr/bin/env python3
"""Offline vertical-slice validation for attachment-free project access."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.project_access import HARD_QUERY_MAX_BYTES
from agent_core.protocol_v9 import parse_v9_tool_transport
from agent_core.protocol_v8 import ProtocolV8Error, validate_model_action
from agent_core.routing import (
    SYNC_FULL_BUNDLE, SYNC_INDEX_ONLY, lazy_context_sync_prompt,
    select_context_sync,
)
from agent_core.smartagent_protocol import parse_tool_calls, validate_tool_envelope
from agent_core.tools import execute_tool


class FakeAgent:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self._authorized_local_paths = []
        self.current_request_id = "REQUEST-PROJECT-ACCESS"
        self.current_task_id = "TASK-PROJECT-ACCESS"
        self.interface_name = "remote"

    def run_project_sync_transaction(self, *_args, **_kwargs):
        raise AssertionError("INDEX_ONLY must never invoke attachment transport")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="smartagent-project-access-") as temp:
        container = Path(temp).resolve()
        project = container / "sample"
        source = project / "src"
        vendor = project / "third_party" / "library"
        source.mkdir(parents=True)
        vendor.mkdir(parents=True)
        (project / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\nadd_executable(sample src/main.cpp)\n",
            encoding="utf-8",
        )
        main = source / "main.cpp"
        main.write_text(
            "#include <iostream>\n"
            "int hdrcore_value() {\n"
            "  return 42;\n"
            "}\n"
            "int main() { return hdrcore_value(); }\n",
            encoding="utf-8",
        )
        (source / "large.cpp").write_text(
            "\n".join(f"int repeated_symbol_{index} = 0; // " + ("x" * 500) for index in range(100)),
            encoding="utf-8",
        )
        (vendor / "vendor.cpp").write_text(
            "int hdrcore_value_vendor_only = 7;\n", encoding="utf-8"
        )
        agent = FakeAgent(container)

        try:
            validate_model_action({
                "tool": "project_sync",
                "action_id": "INVALID-DIRECT-SYNC",
                "strategy": "DIRECT",
                "project_root": str(project),
            })
        except ProtocolV8Error as exc:
            assert exc.code == "PROJECT_SYNC_STRATEGY_INVALID"
            assert "DIRECT is an access mode" in exc.detail
        else:
            raise AssertionError("DIRECT must be rejected before project_sync execution")

        valid, diagnostic = validate_tool_envelope({
            "tool": "project_sync",
            "action_id": "INVALID-DIRECT-ENVELOPE",
            "strategy": "DIRECT",
            "project_root": str(project),
        })
        assert valid is False
        assert diagnostic["reason"] == "project_sync_strategy_invalid"

        policy = json.loads(
            lazy_context_sync_prompt("inspect one file", project)
            .split("\n", 1)[1].rsplit("\n", 1)[0]
        )
        assert policy["available_access_modes"] == ["NONE", "DIRECT"]
        assert policy["available_project_sync_strategies"] == [
            "INDEX_ONLY", "DELTA", "FULL_BUNDLE",
        ]
        assert "available_strategies" not in policy

        invalid_transport = "\n".join((
            "```smartagent_tool",
            json.dumps({
                "tool": "project_sync",
                "action_id": "INVALID-DIRECT-TRANSPORT",
                "strategy": "DIRECT",
                "project_root": str(project),
            }, ensure_ascii=False, separators=(",", ":")),
            "```",
            "```smartagent_tool",
            '{"tool":"turn_commit","action_count":1}',
            "```",
        ))
        calls, diagnostics = parse_v9_tool_transport(invalid_transport)
        assert calls == []
        assert diagnostics[0]["reason"] == "PROJECT_SYNC_STRATEGY_INVALID"
        assert "DIRECT is an access mode" in diagnostics[0]["detail"]

        admitted = validate_model_action({
            "tool": "query_project",
            "action_id": "QUERY-WITH-RUNTIME-OWNED-INDEX",
            "project_root": str(project),
            "queries": [{"operation": "list_tree", "path": "."}],
        })
        assert "snapshot_id" not in admitted
        normalized_sync = validate_model_action({
            "tool": "project_sync",
            "action_id": "NORMALIZED-INDEX-ONLY",
            "strategy": "index_only",
            "project_root": str(project),
        })
        assert normalized_sync["strategy"] == "INDEX_ONLY"
        parsed = parse_tool_calls(
            "```smartagent_tool\n"
            + json.dumps(admitted, ensure_ascii=False, separators=(",", ":"))
            + "\n```"
        )
        assert len(parsed) == 1
        assert parsed[0]["tool"] == "query_project"

        parent_capsule = json.loads(execute_tool({
            "tool": "project_sync",
            "strategy": "INDEX_ONLY",
            "project_root": str(container),
            "workspace": str(container),
        }, agent=agent))

        # A query may be the first project-access action. Runtime owns the
        # exact-root snapshot bootstrap and treats model identity as
        # diagnostic input only. Plain strings are deterministic search_text
        # shorthand used by the v9 narrative bridge.
        bootstrapped = json.loads(execute_tool({
            "tool": "query_project",
            "project_root": str(project),
            "workspace": str(container),
            "snapshot_id": parent_capsule["snapshot_id"],
            "project_handle": parent_capsule["project_handle"],
            "queries": ["hdrcore_value"],
        }, agent=agent))
        assert bootstrapped["status"] == "READY"
        assert bootstrapped["index_context"]["recovery_action"] == "INDEX_BOOTSTRAP"
        assert bootstrapped["index_context"]["model_identity_authoritative"] is False
        assert bootstrapped["index_context"]["requested_snapshot_id"] == parent_capsule["snapshot_id"]
        assert bootstrapped["index_context"]["effective_snapshot_id"] != parent_capsule["snapshot_id"]
        assert bootstrapped["index_context"]["effective_root"] == str(project)
        assert bootstrapped["index_context"]["normalized_query_count"] == 1
        assert bootstrapped["results"][0]["operation"] == "search_text"
        assert bootstrapped["results"][0]["matches"]

        capsule = json.loads(execute_tool({
            "tool": "project_sync",
            "strategy": "INDEX_ONLY",
            "project_root": str(project),
            "workspace": str(container),
        }, agent=agent))
        assert capsule["schema"] == "PROJECT_ACCESS_CAPSULE_V1"
        assert capsule["status"] == "READY"
        assert capsule["attachments_uploaded"] == 0
        assert capsule["transport"] == "INLINE_QUERY_ONLY"
        assert capsule["file_count"] == 4
        assert (project / ".agents" / "project_access" / "current.json").is_file()

        result = json.loads(execute_tool({
            "tool": "query_project",
            "project_root": str(project),
            "workspace": str(container),
            "project_handle": capsule["project_handle"],
            "snapshot_id": capsule["snapshot_id"],
            "queries": [
                {"operation": "list_tree", "path": "src", "depth": 2},
                {"operation": "search_text", "query": "hdrcore_value"},
                {"operation": "read_symbol", "symbol": "hdrcore_value", "path": "src"},
                {"operation": "read_range", "path": "src/main.cpp", "start_line": 1, "end_line": 4},
                {"operation": "get_build_configuration"},
            ],
        }, agent=agent))
        assert result["schema"] == "PROJECT_ACCESS_QUERY_RESULT_V1"
        assert result["status"] == "READY"
        assert result["attachments_uploaded"] == 0
        assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= HARD_QUERY_MAX_BYTES
        search = result["results"][1]
        assert search["status"] == "OK"
        assert search["matches"]
        assert all("third_party" not in item["path"] for item in search["matches"])
        assert "return 42" in result["results"][2]["content"]
        assert "src/main.cpp" in result["results"][3]["path"]
        assert result["results"][4]["items"][0]["path"] == "CMakeLists.txt"

        bounded = json.loads(execute_tool({
            "tool": "query_project",
            "project_root": str(project),
            "workspace": str(container),
            "project_handle": capsule["project_handle"],
            "snapshot_id": capsule["snapshot_id"],
            "max_bytes": 4096,
            "queries": [{"operation": "search_text", "query": "repeated_symbol", "limit": 100}],
        }, agent=agent))
        bounded_search = bounded["results"][0]
        assert bounded["response_truncated"] is True
        assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= 4096
        assert bounded_search["next_cursor"] == len(bounded_search["matches"])

        # Every source slice remains bound to the indexed file hash. Runtime
        # performs one exact-root rebuild rather than terminating the task.
        main.write_text("int changed_after_snapshot = 1;\n", encoding="utf-8")
        stale = json.loads(execute_tool({
            "tool": "query_project",
            "project_root": str(project),
            "workspace": str(container),
            "project_handle": capsule["project_handle"],
            "snapshot_id": capsule["snapshot_id"],
            "queries": [{"operation": "read_range", "path": "src/main.cpp"}],
        }, agent=agent))
        assert stale["status"] == "READY"
        assert stale["index_context"]["recovery_action"] == "INDEX_REBUILT_ONCE"
        assert stale["index_context"]["effective_snapshot_id"] != capsule["snapshot_id"]
        assert "changed_after_snapshot" in stale["results"][0]["content"]

        invalid = json.loads(execute_tool({
            "tool": "query_project",
            "project_root": str(project),
            "workspace": str(container),
            "queries": [{"operation": "invented_operation"}],
        }, agent=agent))
        assert invalid["status"] == "REJECTED"
        assert invalid["error"] == "project_query_operation_unsupported:invented_operation"

        assert select_context_sync("/project-sync").selected_strategy == SYNC_INDEX_ONLY
        assert select_context_sync("/bundle").selected_strategy == SYNC_FULL_BUNDLE

    print("PROJECT_ACCESS_INDEXED_QUERY_OK")


if __name__ == "__main__":
    run()
