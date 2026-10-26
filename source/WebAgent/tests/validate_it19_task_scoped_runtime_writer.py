#!/usr/bin/env python3
"""IT19 native Runtime tool dispatch regression in an isolated TemporaryDirectory.

This is NOT a Live task and must never read or construct a manager on an
installed SmartAgent writer state root. The fixture supplies Runtime-owned
task identities to the real execute_tool implementation.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_core.tools import _execute_tool_impl, _task_scoped_chunked_write_manager
from agent_core.security_context import SecurityContext
from agent_core.chunked_write import ChunkedWriteError


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


def response(raw: str) -> dict:
    assert raw.startswith(("[CHUNKED_WRITE_SUCCESS] ", "[CHUNKED_WRITE_FAILED] ")), raw
    return json.loads(raw.split("] ", 1)[1])


def agent_for(workspace: Path, name: str, *, epoch: str = "EPOCH-1"):
    return SimpleNamespace(
        workspace_root=str(workspace),
        current_task_id="TASK-" + name,
        current_task_epoch=epoch,
        current_request_id="RR-" + name,
        current_run_id="RR-" + name,
        interface_name="telegram",
    )


def begin(agent, destination: Path, prefix: str) -> tuple[str, bytes]:
    chunk = (prefix + ": staged only; not committed").encode("utf-8")
    write_id = prefix + "-OPEN-WRITE"
    started = response(_execute_tool_impl({
        "tool": "begin_file_write",
        "action_id": prefix + "-BEGIN",
        "write_id": write_id,
        "path": str(destination),
        "expected_size": len(chunk),
        "expected_sha256": digest(chunk),
        "chunk_count": 1,
    }, agent=agent))
    assert started["state"] == "OPEN", started
    staged = response(_execute_tool_impl({
        "tool": "write_file_chunk",
        "action_id": prefix + "-CHUNK",
        "write_id": write_id,
        "chunk_index": 0,
        "offset": 0,
        "content": chunk.decode("utf-8"),
        "chunk_size": len(chunk),
        "chunk_sha256": digest(chunk),
    }, agent=agent))
    assert staged["state"] == "OPEN" and staged["received_bytes"] == len(chunk), staged
    assert not destination.exists(), "staged bytes are not a committed output"
    return write_id, chunk


def main():
    with tempfile.TemporaryDirectory(prefix="sa-it19-runtime-task-isolation-") as tmp:
        workspace = Path(tmp).resolve() / "test-workspace"
        workspace.mkdir()
        a, b = agent_for(workspace, "A"), agent_for(workspace, "B")
        original = workspace / ".agents" / "chunked_writes"
        original_manifest = original / "manifests" / "unrelated.json"
        original_manifest.parent.mkdir(parents=True)
        original_manifest.write_text(json.dumps({
            "write_id": "OLD-FOREIGN", "state": "OPEN",
            "request_id": "RR-FOREIGN", "updated_at": 0,
        }), encoding="utf-8")
        original_before = inventory(original)

        # A and B share one workspace, but MUST NOT share the same writer root.
        a_dest = workspace / "A-staged.txt"
        a_id, a_bytes = begin(a, a_dest, "A")
        a_manager = a._chunked_write_manager
        a_root = a_manager.state_root
        assert a_root != original
        assert a_root.parent.name == "tasks"
        assert a_root.is_relative_to(workspace)
        a_stage = Path(a_manager._load_manifest(a_id)["staging_path"])
        assert a_stage.read_bytes() == a_bytes

        b_context = SecurityContext.from_agent(b)
        b_manager = _task_scoped_chunked_write_manager(b, b_context)
        assert b_manager.state_root != a_root
        assert not b_manager.open_sessions(request_id="RR-A", run_id="RR-A")
        # The foreign write ID is not accessible via any canonical Runtime tool.
        denied = response(_execute_tool_impl({
            "tool": "abort_file_write", "action_id": "B-FOREIGN-ABORT", "write_id": a_id,
        }, agent=b))
        assert denied["status"] == "FAILED" and "write_session_not_found" in denied["error_code"], denied
        assert a_manager._load_manifest(a_id)["state"] == "OPEN"

        b_dest = workspace / "B-staged.txt"
        b_id, b_bytes = begin(b, b_dest, "B")
        b_stage = Path(b_manager._load_manifest(b_id)["staging_path"])
        assert b_stage.read_bytes() == b_bytes
        a_snapshot = inventory(a_root)
        # B cleanup/abort can affect only B, despite shared workspace.
        assert b_manager.abort_open_sessions(reason="sandbox_task_cancel")["aborted_sessions"] == 1
        assert b_manager._load_manifest(b_id)["state"] == "ABORTED"
        assert inventory(a_root) == a_snapshot
        assert a_stage.read_bytes() == a_bytes and not a_dest.exists()
        assert not b_dest.exists()

        # Reusing the agent object for a new Task cannot resurrect cached A.
        a.current_task_id = "TASK-C"
        a.current_request_id = "RR-C"
        a.current_run_id = "RR-C"
        c_manager = _task_scoped_chunked_write_manager(a, SecurityContext.from_agent(a))
        assert c_manager.state_root not in {a_root, b_manager.state_root}
        assert a._chunked_write_manager is c_manager
        assert inventory(a_root) == a_snapshot
        # Same Task & Request but different Epoch cannot reuse the previous store.
        a.current_task_epoch = "EPOCH-2"
        new_epoch = _task_scoped_chunked_write_manager(a, SecurityContext.from_agent(a))
        assert new_epoch.state_root != c_manager.state_root
        assert inventory(a_root) == a_snapshot

        # Missing Runtime identity is fail-closed before any manager construction.
        invalid = agent_for(workspace, "INVALID")
        invalid.current_task_epoch = ""
        rejected = response(_execute_tool_impl({
            "tool": "begin_file_write",
            "action_id": "INVALID-BEGIN",
            "write_id": "INVALID-WRITE",
            "path": str(workspace / "should-not-exist.txt"),
        }, agent=invalid))
        assert rejected["status"] == "REJECTED"
        assert rejected["error_code"] == "task_scoped_writer_identity_required"
        assert not hasattr(invalid, "_chunked_write_manager")
        assert not (workspace / "should-not-exist.txt").exists()

        # No cleanup/recovery/commit on the historical workspace-wide root.
        assert {k:v for k,v in inventory(original).items()
                if not k.replace(chr(92), "/").startswith("tasks/")} == original_before
        assert a_manager._load_manifest(a_id)["state"] == "OPEN"
        assert a_stage.read_bytes() == a_bytes and not a_dest.exists()

    print("IT19_TASK_SCOPED_RUNTIME_WRITER_NATIVE_PASS")


if __name__ == "__main__":
    main()
