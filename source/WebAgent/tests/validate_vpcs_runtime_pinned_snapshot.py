"""VPCS integration regression: pinned references must never rebuild the index.

Uses the same query_project tool dispatcher as SmartAgent, not only the
lower-level query_project() API. Does not modify any user workspace.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.project_access import build_project_capsule
from agent_core.project_sync import load_current_project_snapshot
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result
from WebAgent.tests.validate_project_sync_root_resolution import FakeAgent


def _tool(agent, root: Path, queries: list[dict], snapshot: str = "", handle: str = "") -> dict:
    response = execute_tool({
        "tool": "query_project",
        "project_root": str(root),
        "snapshot_id": snapshot,
        "project_handle": handle,
        "queries": queries,
    }, agent)
    return decode_result(response)


def _result(payload: dict) -> dict:
    assert payload["status"] == "READY", payload
    assert len(payload["results"]) == 1, payload
    return payload["results"][0]


def run():
    with tempfile.TemporaryDirectory(prefix="smartagent-vpcs-runtime-") as directory:
        root = Path(directory).resolve()
        source = root / "sample.py"
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")
        agent = FakeAgent(root)
        initial = build_project_capsule(root)
        snapshot_a = initial["snapshot_id"]
        handle = initial["project_handle"]

        metadata = _result(_tool(agent, root, [
            {"operation": "get_file_metadata", "path": "sample.py"}
        ], snapshot_a, handle))
        ref_a = metadata["context_ref"]
        first = _result(_tool(agent, root, [
            {"operation": "read_context_ref", "context_ref": ref_a}
        ], snapshot_a, handle))
        assert first["status"] == "OK" and "return 42" in first["content"], first

        source.write_text("def answer():\n    return 99\n", encoding="utf-8")
        stale_payload = _tool(agent, root, [
            {"operation": "read_context_ref", "context_ref": ref_a}
        ], snapshot_a, handle)
        stale = _result(stale_payload)
        assert stale["status"] == "ERROR", stale
        assert stale["error"] == "project_snapshot_stale:sample.py", stale
        assert stale_payload["index_context"]["recovery_action"] == "PINNED_SNAPSHOT_NO_REBUILD", stale_payload
        assert stale_payload["index_context"]["snapshot_policy"] == "PINNED", stale_payload
        assert load_current_project_snapshot(root)["snapshot_id"] == snapshot_a, "Pinned read implicitly rebuilt index"

        # Mixed batches containing a pinned reference must not quietly change
        # the snapshot for any query in the same response.
        mixed_payload = _tool(agent, root, [
            {"operation": "read_context_ref", "context_ref": ref_a},
            {"operation": "get_file_metadata", "path": "sample.py"},
        ], snapshot_a, handle)
        assert mixed_payload["results"][0]["error"] == "project_snapshot_stale:sample.py", mixed_payload
        assert mixed_payload["index_context"]["recovery_action"] == "PINNED_SNAPSHOT_NO_REBUILD", mixed_payload
        assert load_current_project_snapshot(root)["snapshot_id"] == snapshot_a

        # Non-pinned reads retain the pre-existing automatic recovery behavior.
        ordinary = _tool(agent, root, [
            {"operation": "read_range", "path": "sample.py"}
        ], snapshot_a, handle)
        assert _result(ordinary)["status"] == "OK" and "return 99" in ordinary["results"][0]["content"], ordinary
        assert ordinary["index_context"]["recovery_action"] == "INDEX_REBUILT_ONCE", ordinary
        snapshot_b = ordinary["snapshot_id"]
        assert snapshot_b != snapshot_a
        assert load_current_project_snapshot(root)["snapshot_id"] == snapshot_b

        # Previous immutable references cannot cross the new snapshot.
        invalid = _result(_tool(agent, root, [
            {"operation": "read_context_ref", "context_ref": ref_a}
        ], snapshot_b, handle))
        assert invalid["status"] == "ERROR" and invalid["error"] == "context_ref_snapshot_mismatch", invalid

        fresh = _result(_tool(agent, root, [
            {"operation": "get_file_metadata", "path": "sample.py"}
        ], snapshot_b, handle))
        assert fresh["context_ref"] != ref_a
        resolved = _result(_tool(agent, root, [
            {"operation": "read_context_ref", "context_ref": fresh["context_ref"]}
        ], snapshot_b, handle))
        assert resolved["status"] == "OK" and "return 99" in resolved["content"], resolved
        assert resolved["file_sha256"] == fresh["file"]["sha256"], resolved

    print("VPCS_RUNTIME_PINNED_SNAPSHOT_PASS")


if __name__ == "__main__":
    run()
