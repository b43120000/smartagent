"""Git75 P0: real-file Project Sync INDEX_ONLY/query/staleness E2E.

This is a sidecar Tool API test, NOT a Telegram/Browser Planner Live pass.
It uses the production index and query implementations on a fresh temporary
project, without permitting writes outside that project. It publishes
explicit machine-readable evidence for GitHub CI and local diagnostic review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .tools import execute_tool
from .unified_preflight import _pointer


SCHEMA = "SMARTAGENT_GIT75_PROJECT_SYNC_P0_IT_V1"


class FixtureAgent:
    interface_name = "webdirect"

    def __init__(self, workspace_root: Path, project: Path):
        self.workspace_root = workspace_root
        self._authorized_local_paths = [str(project)]
        self.current_request_id = "RR-G75-PROJECT-SYNC-P0"
        self.current_task_id = "TASK-G75-PROJECT-SYNC-P0"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _invoke(agent: FixtureAgent, action: dict) -> dict:
    response = json.loads(execute_tool(action, agent=agent))
    if not isinstance(response, dict):
        raise AssertionError("project_tool_result_not_object")
    return response


def run(workspace_root: str | Path | None = None) -> dict:
    start = time.monotonic()
    if workspace_root is None:
        temporary_base = tempfile.TemporaryDirectory(prefix="g75-projectsync-p0-")
        root = Path(temporary_base.name).resolve()
    else:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("workspace_root_must_exist")
        temporary_base = None
    try:
        # Never reuse or remove an existing user Project, and never broaden
        # authorization from this test fixture to the enclosing Workspace.
        with tempfile.TemporaryDirectory(prefix="SA_G75_PS_P0_", dir=root) as fixture:
            project = Path(fixture).resolve() / "project"
            source = project / "src"
            source.mkdir(parents=True)
            target = source / "probe.py"
            old_bytes = b"SYNC_TEST_VALUE = 'A0'\n"
            new_bytes = b"SYNC_TEST_VALUE = 'B1'\n"
            assert len(old_bytes) == len(new_bytes)
            target.write_bytes(old_bytes)
            agent = FixtureAgent(root, project)
            project_root = str(project)
            sync = _invoke(agent, {
                "tool": "project_sync", "strategy": "INDEX_ONLY",
                "project_root": project_root,
            })
            assert sync.get("status") == "READY", sync
            old_id = sync["snapshot_id"]
            assert sync.get("attachments_uploaded") == 0
            assert _pointer(project)["snapshot_id"] == old_id

            def query(queries):
                return _invoke(agent, {
                    "tool": "query_project", "project_root": project_root,
                    "queries": queries,
                })

            original = query([{
                "operation": "read_range", "path": "src/probe.py",
                "start_line": 1, "end_line": 2,
            }])
            assert original["status"] == "READY", original
            assert original["results"][0]["status"] == "OK", original
            assert "A0" in original["results"][0]["content"]
            assert original["results"][0]["file_sha256"] == _sha(old_bytes)
            assert original["snapshot_id"] == old_id

            metadata = query([{
                "operation": "get_file_metadata", "path": "src/probe.py",
            }])
            ref = metadata["results"][0].get("context_ref")
            assert isinstance(ref, str) and ref

            # Exact original size and mtime: a timestamp-only cache must NOT
            # be mistaken for current-byte proof after mutation.
            st = target.stat()
            target.write_bytes(new_bytes)
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            assert target.read_bytes() == new_bytes
            assert _sha(old_bytes) != _sha(new_bytes)
            # Pinned content references may not silently switch snapshots.
            pinned = query([{
                "operation": "read_context_ref", "context_ref": ref,
            }])
            assert pinned["index_context"]["recovery_action"] == (
                "PINNED_SNAPSHOT_NO_REBUILD"
            ), pinned
            assert pinned["results"][0]["status"] == "ERROR", pinned
            assert pinned["results"][0]["error"].startswith(
                "project_snapshot_stale:"
            ), pinned
            assert _pointer(project)["snapshot_id"] == old_id

            # Unpinned queries can re-index exactly ONCE and must read
            # verified new bytes, not the prior cached source.
            refreshed = query([{
                "operation": "read_range", "path": "src/probe.py",
                "start_line": 1, "end_line": 2,
            }])
            assert refreshed["status"] == "READY", refreshed
            assert refreshed["index_context"]["recovery_action"] == (
                "INDEX_REBUILT_ONCE"
            ), refreshed
            assert refreshed["results"][0]["status"] == "OK", refreshed
            assert "B1" in refreshed["results"][0]["content"]
            assert "A0" not in refreshed["results"][0]["content"]
            assert refreshed["results"][0]["file_sha256"] == _sha(new_bytes)
            new_id = refreshed["snapshot_id"]
            assert old_id != new_id
            assert _pointer(project)["snapshot_id"] == new_id

            resync = _invoke(agent, {
                "tool": "project_sync", "strategy": "INDEX_ONLY",
                "project_root": project_root,
            })
            assert resync["status"] == "READY", resync
            assert resync["snapshot_id"] == new_id, resync
            assert resync.get("attachments_uploaded") == 0
            stable = query([{
                "operation": "read_range", "path": "src/probe.py",
                "start_line": 1, "end_line": 2,
            }])
            assert stable["index_context"]["recovery_action"] == (
                "REUSED_CURRENT_INDEX"
            ), stable
            assert stable["results"][0]["file_sha256"] == _sha(new_bytes)

            # Scope is enforced by the existing tool policy; no write or
            # index creation of an unrelated path is allowed.
            outside = root / "unrelated-source-not-authorized"
            forbidden = _invoke(agent, {
                "tool": "project_sync", "strategy": "INDEX_ONLY",
                "project_root": str(outside),
            })
            assert forbidden["status"] == "REJECTED", forbidden
            assert not outside.exists()

            return {
                "schema": SCHEMA, "status": "PASS",
                "tool_api": "agent_core.tools.execute_tool",
                "strategy": "INDEX_ONLY",
                "snapshot_initial": old_id, "snapshot_refreshed": new_id,
                "source_sha256_old": _sha(old_bytes),
                "source_sha256_new": _sha(new_bytes),
                "snapshot_changed": old_id != new_id,
                "initial_query_actual_bytes_verified": True,
                "mtime_and_size_unchanged_on_mutation": True,
                "pinned_context_ref_rejected_on_stale_bytes": True,
                "unpinned_query_rebuilt_index_once": True,
                "new_query_actual_bytes_verified": True,
                "previous_bytes_absent_after_refresh": True,
                "explicit_index_only_resync": "READY",
                "stable_query_reused_current_index": True,
                "outside_scope_rejected": True,
                "attachments_uploaded": 0,
                "source_snapshot_is_current_byte_evidence": False,
                "runtime_source_byte_verification_per_query": True,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                "telegram_live_verified": False,
            }
    finally:
        if temporary_base is not None:
            temporary_base.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        result = run(args.workspace_root or None)
    except Exception as exc:
        result = {
            "schema": SCHEMA, "status": "FAIL",
            "failure_class": type(exc).__name__,
            "reason_code": str(exc)[:500],
            "telegram_live_verified": False,
        }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
