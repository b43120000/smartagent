#!/usr/bin/env python3
"""IT19 offline sandbox: OPEN staging and writer/store isolation.

Must never run against a real workspace or instantiate ChunkedWriteManager on
an active SmartAgent transaction store. All writer state is under TemporaryDirectory.
This test proves the offline isolation fixture only, NOT native Live readiness.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from agent_core.chunked_write import ChunkedWriteManager


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(folder: Path) -> dict[str, str]:
    if not folder.exists():
        return {}
    return {
        str(path.relative_to(folder)): sha(path.read_bytes())
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def write_open(manager: ChunkedWriteManager, destination: Path, prefix: str) -> tuple[Path, bytes]:
    payload = (prefix + "-staged-without-commit").encode("utf-8")
    write_id = prefix + "-WRITE"
    began = manager.begin({
        "tool": "begin_file_write",
        "action_id": prefix + "-BEGIN",
        "write_id": write_id,
        "path": str(destination),
        "expected_size": len(payload),
        "expected_sha256": sha(payload),
        "chunk_count": 1,
        "overwrite": False,
    }, request_id=prefix + "-REQUEST", run_id=prefix + "-REQUEST", turn_id=1)
    assert began["state"] == "OPEN", began
    chunked = manager.write_chunk({
        "tool": "write_file_chunk",
        "action_id": prefix + "-CHUNK",
        "write_id": write_id,
        "chunk_index": 0,
        "offset": 0,
        "content": payload.decode("utf-8"),
        "chunk_size": len(payload),
        "chunk_sha256": sha(payload),
    })
    assert chunked["state"] == "OPEN" and chunked["received_bytes"] == len(payload), chunked
    assert not destination.exists(), "staged bytes must not be represented as committed"
    manifest = manager._load_manifest(write_id)
    staging = Path(manifest["staging_path"])
    assert staging.is_file() and staging.read_bytes() == payload
    assert manifest["state"] == "OPEN" and manifest["next_offset"] == len(payload)
    assert manager.open_sessions(request_id=prefix + "-REQUEST", run_id=prefix + "-REQUEST")
    assert not manager.open_sessions(request_id=prefix + "-FOREIGN", run_id=prefix + "-FOREIGN")
    return staging, payload


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sa-it19-isolated-offline-") as tmp:
        root = Path(tmp).resolve()
        workspaces = [root / label for label in ("task_a", "task_b", "task_c")]
        for workspace in workspaces:
            workspace.mkdir()
        roots = [workspace / ".agents" / "chunked_writes" for workspace in workspaces]
        assert len({str(path.resolve()) for path in roots}) == 3
        assert not any(path.exists() for path in roots), "test must start with no OPEN writer state"

        # A legacy shared-state decoy must not be scanned, cleaned or migrated.
        legacy = root / "legacy_shared"
        (legacy / "manifests").mkdir(parents=True)
        legacy_file = legacy / "manifests" / "foreign_OPEN.json"
        legacy_file.write_text(json.dumps({"write_id": "FOREIGN", "state": "OPEN"}), encoding="utf-8")
        legacy_before = snapshot(legacy)

        other = ChunkedWriteManager(roots[1], [workspaces[1]])
        b_target = workspaces[1] / "open_transaction.txt"
        b_stage, b_bytes = write_open(other, b_target, "B")
        b_before = snapshot(roots[1])

        candidate = ChunkedWriteManager(roots[0], [workspaces[0]])
        a_target = workspaces[0] / "open_transaction.txt"
        a_stage, a_bytes = write_open(candidate, a_target, "A")
        assert snapshot(roots[1]) == b_before, "A begin/chunk modified B store"
        assert snapshot(legacy) == legacy_before, "A begin/chunk modified old shared state"

        # A new same-workspace manager may perform its own startup recovery,
        # but it must not traverse the separately bound foreign store.
        restarted_a = ChunkedWriteManager(roots[0], [workspaces[0]])
        assert restarted_a._load_manifest("A-WRITE")["state"] == "OPEN"
        assert a_stage.read_bytes() == a_bytes and not a_target.exists()
        assert snapshot(roots[1]) == b_before, "A restart modified B store"

        # Cooperative abort for a third isolated writer cannot affect A or B.
        third = ChunkedWriteManager(roots[2], [workspaces[2]])
        c_target = workspaces[2] / "open_transaction.txt"
        write_open(third, c_target, "C")
        result = third.abort_open_sessions(reason="sandbox_cancel")
        assert result["aborted_sessions"] == 1, result
        assert third._load_manifest("C-WRITE")["state"] == "ABORTED"
        assert not c_target.exists()

        assert candidate._load_manifest("A-WRITE")["state"] == "OPEN"
        assert other._load_manifest("B-WRITE")["state"] == "OPEN"
        assert a_stage.read_bytes() == a_bytes and b_stage.read_bytes() == b_bytes
        assert not a_target.exists() and not b_target.exists()
        assert snapshot(roots[1]) == b_before
        assert snapshot(legacy) == legacy_before
        # Expire only C. A and B are still OPEN and byte-identical.
        third.cleanup_expired(now=10**12)
        assert candidate._load_manifest("A-WRITE")["state"] == "OPEN"
        assert other._load_manifest("B-WRITE")["state"] == "OPEN"
        assert snapshot(roots[1]) == b_before
        assert snapshot(legacy) == legacy_before

    print("IT19_OPEN_WRITE_ISOLATION_OFFLINE_PASS")


if __name__ == "__main__":
    main()
