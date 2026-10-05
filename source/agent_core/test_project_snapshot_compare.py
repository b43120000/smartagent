from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .project_sync import compare_project_snapshot, inspect_project_scope, save_project_snapshot
from .tools import execute_tool


class ProjectSnapshotCompareRegressionTests(unittest.TestCase):
    def test_snapshot_id_is_resolved_before_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "sample.txt").write_text("same", encoding="utf-8")
            snapshot = inspect_project_scope(workspace)
            save_project_snapshot(snapshot)
            agent = SimpleNamespace(workspace_root=workspace, _models_registry={}, _authorized_local_paths=(str(workspace),), current_request_text="")
            result = json.loads(execute_tool({"tool":"compare_project_snapshot","workspace":str(workspace),"known_snapshot":snapshot["snapshot_id"]}, agent=agent))
            self.assertEqual(result["freshness"], "FRESH")
            self.assertEqual(result["known_snapshot_id"], snapshot["snapshot_id"])

    def test_compare_boundary_rejects_string(self):
        current = {"snapshot_id":"current","file_count":0,"files":[]}
        with self.assertRaisesRegex(TypeError, "known snapshot must be dict or None"):
            compare_project_snapshot(current, "snapshot-id")


if __name__ == "__main__":
    unittest.main()
