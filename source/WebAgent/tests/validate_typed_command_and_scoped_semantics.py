#!/usr/bin/env python3
"""Regression checks for typed commands and bounded semantic-map completion."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from agent_core.action_loop_state import build_action_loop_state, phase_for_action
from agent_core.command_operation import (
    build_operation_result,
    operation_mismatch_detail,
    operation_result_contract,
    operation_terminal_eligible,
    result_schema_for_operation,
)
from agent_core.project_sync import (
    inspect_project_scope, inspect_project_working_set, save_project_snapshot,
)
from agent_core.semantic_map import inspect_semantic_map, update_semantic_map
from agent_core.smartagent_protocol import validate_tool_envelope


def _semantic_row(path: str, digest: str) -> dict:
    return {
        "path": path,
        "source_sha256": digest,
        "responsibility": "test source",
        "public_symbols": [], "dependencies": [], "flows": [],
        "invariants": [], "tests": [],
    }


def main() -> None:
    valid = {
        "tool": "run_command", "operation": "BUILD",
        "command": ".\\gradlew.bat assembleDebug",
    }
    assert validate_tool_envelope(valid)[0]
    expected_failure = {
        **valid,
        "expected_failure": {"exit_codes": [1], "stderr_regex": "not a directory"},
    }
    assert validate_tool_envelope(expected_failure)[0]
    forbidden_expected_failure = {
        "tool": "run_command", "operation": "MUTATE", "command": "Write-Output changed",
        "expected_failure": {"exit_codes": [1]},
    }
    ok, diagnostic = validate_tool_envelope(forbidden_expected_failure)
    assert not ok and diagnostic["reason"] == "run_command_expected_failure_operation_forbidden"
    invalid_expected_failure = {
        **valid, "expected_failure": {"exit_codes": [0]},
    }
    ok, diagnostic = validate_tool_envelope(invalid_expected_failure)
    assert not ok and diagnostic["reason"] == "run_command_expected_failure_invalid"
    missing = {"tool": "run_command", "command": "git status --short"}
    ok, diagnostic = validate_tool_envelope(missing)
    assert not ok and diagnostic["reason"] == "missing_required_field"
    assert phase_for_action(valid) == "EXECUTE"
    assert phase_for_action({
        "tool": "run_command", "operation": "GIT_INSPECT",
        "command": "git status --short",
    }) == "DISCOVERY"
    assert phase_for_action({
        "tool": "run_command", "operation": "VERIFY",
        "command": "git rev-parse HEAD", "verifies_action_id": "A-COMMIT",
    }) == "VERIFY"
    assert operation_mismatch_detail({
        "operation": "GIT_INSPECT", "command": "git commit -m checkpoint",
    })
    assert not operation_terminal_eligible("GENERAL", "PASS")
    assert result_schema_for_operation("BUILD") == "SMARTAGENT_COMMAND_RESULT_BUILD_V1"
    assert "build_artifact_evidence" in operation_result_contract("BUILD")
    typed = build_operation_result(
        "VERIFY", command="git rev-parse HEAD", execution_status="SUCCEEDED",
        verification_status="PASS", verifies_action_id="A-COMMIT",
        verification_evidence=[{"passed": True}],
    )
    assert typed["schema"] == "SMARTAGENT_COMMAND_RESULT_VERIFY_V1"
    assert typed["verifies_action_id"] == "A-COMMIT"
    state = build_action_loop_state(
        transport_state="ACTIVE", progress={"decision": "CONTINUE"},
        action=valid, execution_status="SUCCEEDED", verification_status="PASS",
        action_records=[{
            "action": valid, "execution_status": "SUCCEEDED",
            "verification_status": "PASS", "result": "ok",
        }],
    )
    assert state["state_layers"]["transport"]["state"] == "ACTIVE"
    assert state["state_layers"]["action"]["state"] == "VERIFIED_PASS"
    assert state["state_layers"]["task"]["state"] == "RUNNING"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "a.py").write_text("A = 1\n", encoding="utf-8")
        (root / "b.py").write_text("B = 2\n", encoding="utf-8")
        snapshot = inspect_project_scope(root)
        save_project_snapshot(snapshot)
        repeated = inspect_project_scope(root)
        assert repeated["inventory_mode"] == "FILESYSTEM_INCREMENTAL"
        assert repeated["reused_hash_count"] == 2
        assert repeated["hashed_file_count"] == 0
        working_set = inspect_project_working_set(root, ["a.py"])
        assert working_set["scope"] == "TASK_WORKING_SET"
        assert working_set["requested_paths"] == ["a.py"]
        assert [row["path"] for row in working_set["files"]] == ["a.py"]
        assert working_set["attachments_uploaded"] == 0

        digest = hashlib.sha256((root / "a.py").read_bytes()).hexdigest()
        updated = update_semantic_map(root, {
            "base_snapshot_id": repeated["snapshot_id"],
            "required_paths": ["a.py"],
            "project_summary": "bounded test",
            "flows": [],
            "files": [_semantic_row("a.py", digest)],
        })
        assert updated["status"] == "UPDATED", json.dumps(updated, indent=2)
        assert updated["scope"] == "PLAN"
        assert updated["target_file_count"] == 1
        assert inspect_semantic_map(root)["status"] != "FRESH"
        absolute = inspect_semantic_map(root, [str(root / "a.py")])
        assert absolute["status"] == "FRESH" and absolute["required_paths"] == ["a.py"]
        try:
            inspect_semantic_map(root, [str(root.parent / "outside.py")])
        except ValueError as exc:
            assert "semantic_map_path_outside_workspace" in str(exc)
        else:
            raise AssertionError("outside semantic path must be rejected")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "tracked.py"], check=True)
        subprocess.run([
            "git", "-C", str(root), "-c", "user.name=SmartAgent Test",
            "-c", "user.email=smartagent@example.invalid", "commit", "-m", "baseline",
        ], check=True, capture_output=True)
        first = inspect_project_scope(root)
        save_project_snapshot(first)
        second = inspect_project_scope(root)
        assert second["inventory_mode"] == "GIT_FIRST_HYBRID"
        assert second["reused_hash_count"] == 1
        assert second["hashed_file_count"] == 0
        assert second["files"][0]["identity_source"] == "GIT_BLOB"

    print("validate_typed_command_and_scoped_semantics: PASS")


if __name__ == "__main__":
    main()
