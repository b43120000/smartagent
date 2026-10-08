#!/usr/bin/env python3
"""Offline vertical-slice validation for Project Ledger provenance."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.attachment_policy import AttachmentPolicy
from agent_core.project_access import build_project_capsule
from agent_core.project_ledger import (
    append_project_event,
    inspect_project_ledger,
    project_ledger_index_path,
    project_ledger_path,
    query_project_history,
)
from agent_core.project_sync import inspect_project_scope
from agent_core.project_sync_message import build_atomic_project_sync
from agent_core.project_sync_runner import run_project_sync_transaction
from agent_core.project_sync_transaction import build_project_sync_plan
from agent_core.semantic_map import update_semantic_map
from agent_core.smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
from agent_core.tool_capabilities import get_allowed_tools
from agent_core.tools import execute_tool


class FakeAgent:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self._authorized_local_paths = []
        self.current_request_id = "REQUEST-LEDGER"
        self.current_task_id = "TASK-LEDGER"
        self.interface_name = "remote"


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="smartagent-project-ledger-") as temp:
        project = Path(temp) / "project"
        project.mkdir()
        source = project / "main.py"
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")

        first = append_project_event(
            project,
            "project_initialized",
            operation_result="READY",
            event_key="project_initialized:test",
        )
        duplicate = append_project_event(
            project,
            "project_initialized",
            operation_result="READY",
            event_key="project_initialized:test",
        )
        assert duplicate["event_id"] == first["event_id"]
        assert duplicate["deduplicated"] is True

        capsule = build_project_capsule(project)
        assert capsule["strategy"] == "INDEX_ONLY"
        indexed = query_project_history(project, event_types=["sync_completed"])
        assert indexed["match_count"] == 1
        assert indexed["events"][0]["snapshot_id"] == capsule["snapshot_id"]
        assert indexed["events"][0]["details"]["attachments_uploaded"] == 0

        prepared = build_atomic_project_sync(
            project,
            "FULL_BUNDLE",
            policy=AttachmentPolicy(
                max_files_per_bundle=10,
                max_bytes_per_bundle=100_000,
                max_attachments_per_message=7,
                max_batches=4,
            ),
        )
        assert prepared["status"] == "READY"
        prepared_history = query_project_history(project, event_types=["sync_prepared"])
        assert prepared_history["match_count"] == 1
        assert prepared_history["events"][0]["sync_id"] == prepared["transaction"]["sync_id"]

        snapshot = inspect_project_scope(project)
        main_record = next(row for row in snapshot["files"] if row["path"] == "main.py")
        semantic = update_semantic_map(project, {
            "base_snapshot_id": snapshot["snapshot_id"],
            "project_summary": "Small ledger validation project.",
            "flows": ["answer"],
            "files": [{
                "path": "main.py",
                "source_sha256": main_record["sha256"],
                "responsibility": "Return the test answer.",
                "public_symbols": ["answer"],
                "dependencies": [],
                "flows": ["answer"],
                "invariants": ["answer returns 42"],
                "tests": ["validate_project_ledger"],
            }],
        })
        assert semantic["status"] == "UPDATED"
        assert semantic["ledger_event_id"].startswith("PLE-")

        no_change_plan = build_project_sync_plan(
            snapshot["snapshot_id"], [], AttachmentPolicy()
        )

        def unexpected_transport(_prompt: str, _paths: list[Path]):
            raise AssertionError("zero-batch sync must not invoke attachment transport")

        runtime = run_project_sync_transaction(
            project,
            no_change_plan,
            unexpected_transport,
            interface_name="remote",
            conversation_id="conversation-ledger",
            session_id="session-ledger",
            request_id="request-ledger",
        )
        assert runtime["status"] == "PROJECT_SYNC_READY"

        inspection = inspect_project_ledger(project, limit=100)
        assert inspection["status"] == "READY"
        assert inspection["event_count"] >= 6
        assert inspection["latest_semantic_map_revision"] == semantic["semantic_map_revision"]
        assert project_ledger_path(project).is_file()
        assert project_ledger_index_path(project).is_file()

        history = query_project_history(
            project,
            event_types=["semantic_map_updated"],
            path_contains="main.py",
        )
        assert history["match_count"] == 1
        assert history["events"][0]["changed_files"] == ["main.py"]

        agent = FakeAgent(project)
        tool_inspection = json.loads(execute_tool(
            {"tool": "inspect_project_ledger", "workspace": str(project), "limit": 5},
            agent=agent,
        ))
        assert tool_inspection["status"] == "READY"
        tool_history = json.loads(execute_tool({
            "tool": "query_project_history",
            "workspace": str(project),
            "event_types": ["sync_completed"],
            "limit": 20,
        }, agent=agent))
        assert tool_history["match_count"] >= 2

        for tool in ("inspect_project_ledger", "query_project_history"):
            assert tool in TOOL_ENVELOPE_SCHEMAS
            assert tool in get_allowed_tools("web_direct")

    print("PROJECT_LEDGER_OK")


if __name__ == "__main__":
    run()
