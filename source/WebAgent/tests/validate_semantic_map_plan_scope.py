#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.project_sync import inspect_project_scope
from agent_core.semantic_map import inspect_semantic_map, update_semantic_map
from agent_core.task_plan import validate_task_plan


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="smartagent-semantic-scope-") as temp:
        root = Path(temp)
        (root / "source").mkdir()
        (root / "source" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (root / "source" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)

        before = inspect_project_scope(root)
        (root / ".agents" / "runtime").mkdir(parents=True)
        (root / ".agents" / "runtime" / "ledger.json").write_text("{}", encoding="utf-8")
        (root / ".update_regression" / "source").mkdir(parents=True)
        (root / ".update_regression" / "source" / "copy.py").write_text(
            "def copied(): pass\n", encoding="utf-8"
        )
        after = inspect_project_scope(root)
        assert after["snapshot_id"] == before["snapshot_id"]
        assert all(not row["path"].startswith(".update_") for row in after["files"])

        source_a = next(row for row in after["files"] if row["path"] == "source/a.py")
        updated = update_semantic_map(root, {
            "base_snapshot_id": after["snapshot_id"],
            "project_summary": "test project",
            "flows": [],
            "files": [{
                "path": "source/a.py",
                "source_sha256": source_a["sha256"],
                "responsibility": "Provides function a.",
                "public_symbols": ["a"],
                "dependencies": [], "flows": [], "invariants": [], "tests": [],
            }],
        })
        assert updated["status"] == "PARTIAL"
        placeholder = update_semantic_map(root, {
            "base_snapshot_id": after["snapshot_id"],
            "project_summary": "<provide summary>",
            "flows": [],
            "files": [],
        })
        assert placeholder == {
            "status": "INVALID", "reason": "semantic_map_placeholder_project_summary",
        }
        scoped = inspect_semantic_map(root, ["source/a.py"])
        assert scoped["status"] == "FRESH"
        assert scoped["scope"] == "PLAN"
        assert scoped["required_paths"] == ["source/a.py"]
        full = inspect_semantic_map(root)
        assert full["status"] == "STALE"
        assert full["needs_analysis"] == ["source/b.py"]

        plan = {
            "schema": "TASK_PLAN_V1",
            "base_snapshot_id": after["snapshot_id"],
            "semantic_map_revision": scoped["semantic_map_revision"],
            "goal": "Change function a without requiring unrelated b semantics.",
            "affected_flows": ["a flow"],
            "files_to_read": ["source/a.py"],
            "edit_plan": {
                "files_to_modify": [{
                    "path": "source/a.py",
                    "base_sha256": source_a["sha256"],
                    "modification_intent": "Return a different constant.",
                    "mode": "exact_replace",
                    "old": "return 1",
                    "new": "return 3",
                }],
                "expected_observable_result": "Function a returns 3.",
            },
            "verification_commands": ["python -m py_compile source/a.py"],
            "acceptance_criteria": ["source/a.py compiles"],
            "rollback_condition": "Compilation fails.",
            "post_change_semantic": [{
                "path": "source/a.py",
                "responsibility": "Provides function a returning 3.",
                "public_symbols": ["a"],
                "dependencies": [], "flows": ["a flow"],
                "invariants": [], "tests": [],
            }],
        }
        validated = validate_task_plan(root, plan)
        assert validated["status"] == "VALID", validated

    print("SEMANTIC_MAP_PLAN_SCOPE_OK")


if __name__ == "__main__":
    run()
