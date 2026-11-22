"""Git50 Windows-only end-to-end disposable Broker fixture.

This test injects ONLY fixture-specific installation paths and ACL state.
It does NOT forge a native OS attestation, token, Job Object, process receipt,
or successful execution. Every tested command runs via the real C# native
AppContainer backend in admin-protected test installation files.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agent_core import (
    turbo_sandbox, turbo_os_broker_client, turbo_broker_admission,
)
from agent_core.bounded_process import run_bounded_process
from agent_core.tools import execute_tool


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--turbo-root", required=True)
    args = p.parse_args()
    project = Path(args.project_root).resolve(strict=True)
    workspace = Path(args.workspace).resolve(strict=True)
    root = Path(args.turbo_root).resolve(strict=True)
    policy_file = project / "localdata" / "secure" / "windows_security" / "workspace_access_policy.json"
    mode_file = policy_file.with_name("acl_mode.json")
    policy = json.loads(policy_file.read_text(encoding="utf-8-sig"))
    if policy.get("turbo_requested") is not True or policy.get("revision") != 77:
        raise AssertionError("GIT50_TEST_FIXTURE_POLICY_MISMATCH")
    access = {
        "policy": policy,
        "turbo_roots": (str(root),),
        "writable_workspaces": (str(workspace),),
        "read_only_roots": (),
    }
    # Only change the locations of a disposable CI installation, not any OS
    # authority. The protected real broker still checks policy/mode/SID.
    turbo_sandbox.access_snapshot = lambda: access
    turbo_sandbox.acl_mode = lambda: json.loads(
        mode_file.read_text(encoding="utf-8-sig")
    )["mode"]
    turbo_os_broker_client.access_snapshot = lambda: access
    turbo_os_broker_client.workspace_access_policy_path = lambda: policy_file
    turbo_os_broker_client._install_root = lambda: project

    gate = turbo_broker_admission.assess(workspace, tool="run_command")
    assert gate["decision"] == "BROKER_REQUIRED", gate["reason_code"]
    command = (
        "[IO.File]::WriteAllText('runtime_created.txt','GIT50_RUNTIME_NATIVE'); "
        "'GIT50_RUNTIME_STDOUT'"
    )
    result = run_bounded_process(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=workspace, security_command=command,
        timeout=35, max_stream_bytes=65536,
        telemetry={"task_id": "TASK-GIT50-RUNTIME", "action_id": "ACT-GIT50-RUNTIME"},
    )
    assert result["exit_code"] == 0, result
    assert result["security_rejected"] is False, result
    assert result["process_started"] is True, result
    assert "GIT50_RUNTIME_STDOUT" in result["stdout"], result
    assert (root / "runtime_created.txt").read_text(encoding="utf-8") == "GIT50_RUNTIME_NATIVE"
    evidence = result["sandbox_evidence"]
    assert evidence["sandbox_backend"] == "WIN32_APPCONTAINER", evidence
    assert evidence["sandbox_identity_verified"] is True, evidence
    assert evidence["kernel_outside_read_denied"] is True, evidence
    assert evidence["child_process_policy"] == "JOB_KILL_ON_CLOSE", evidence
    assert evidence["action_id"] == "ACT-GIT50-RUNTIME", evidence
    assert evidence["task_id"] == "TASK-GIT50-RUNTIME", evidence
    print("GIT50_PYTHON_RUNTIME_NATIVE_BROKER_INTEGRATION_PASS", flush=True)

    # Git50 P4: regular typed Runtime directory and bounded UTF-8 file
    # mutations must reach the same real native AppContainer token, not host.
    agent = SimpleNamespace(workspace_root=workspace, workspace=str(workspace))
    typed_dir = root / "typed_created_directory"
    made = execute_tool(
        {"tool": "create_directory", "path": str(typed_dir),
         "action_id": "ACT-GIT50-MKDIR"}, agent,
    )
    assert typed_dir.is_dir(), made
    assert "DIRECTORY_CREATED" in made, made
    assert "WIN32_APPCONTAINER" in made, made
    print("GIT50_TYPED_DIRECTORY_NATIVE_BROKER_PASS", flush=True)

    typed_target = typed_dir / "typed_write_via_os_broker.txt"
    written = execute_tool(
        {"tool": "write_file", "path": str(typed_target),
         "content": "GIT50_TYPED_OS_BROKER", "action_id": "ACT-GIT50-TYPED"},
        agent,
    )
    assert typed_target.read_text(encoding="utf-8") == "GIT50_TYPED_OS_BROKER", written
    assert "FILE_WRITTEN_VERIFIED" in written, written
    assert "WIN32_APPCONTAINER" in written, written
    print("GIT50_TYPED_WRITE_NATIVE_BROKER_PASS", flush=True)

    # Even when the ordinary Workspace write scope includes its sibling,
    # the OS Turbo authorization must be limited to the *nested* Turbo root.
    outside_file = workspace / "typed_os_broker_outside_must_not_write.txt"
    outside_write = execute_tool(
        {"tool": "write_file", "path": str(outside_file),
         "content": "SHOULD_NOT_EXIST", "action_id": "ACT-GIT50-OUTSIDE"},
        agent,
    )
    assert not outside_file.exists(), outside_write
    assert "TURBO_TYPED_WRITE_REJECTED" in outside_write, outside_write
    outside_dir = workspace / "typed_os_broker_outside_must_not_mkdir"
    outside_mkdir = execute_tool(
        {"tool": "create_directory", "path": str(outside_dir),
         "action_id": "ACT-GIT50-OUTSIDE-MKDIR"},
        agent,
    )
    assert not outside_dir.exists(), outside_mkdir
    assert "TURBO_TYPED_CREATE_REJECTED" in outside_mkdir, outside_mkdir
    print("GIT50_TYPED_OPERATIONS_OUTSIDE_ROOT_DENIED_PASS", flush=True)

    # Recursive deletion is unimplemented and must never call a host
    # shutil.rmtree. A single file and an EMPTY directory may be removed
    # only by the native AppContainer process.
    recursive = execute_tool(
        {"tool": "delete_path", "path": str(typed_dir),
         "recursive": True, "action_id": "ACT-GIT50-RECURSIVE"}, agent,
    )
    assert typed_target.exists(), recursive
    assert "TURBO_RECURSIVE_DELETE_OS_ADAPTER_NOT_ATTESTED" in recursive, recursive
    print("GIT50_RECURSIVE_DELETE_FAIL_CLOSED_PASS", flush=True)

    deleted_file = execute_tool(
        {"tool": "delete_path", "path": str(typed_target),
         "recursive": False, "action_id": "ACT-GIT50-DELETE-FILE"}, agent,
    )
    assert not typed_target.exists(), deleted_file
    assert "DELETED" in deleted_file and "WIN32_APPCONTAINER" in deleted_file, deleted_file
    deleted_empty_dir = execute_tool(
        {"tool": "delete_path", "path": str(typed_dir),
         "recursive": False, "action_id": "ACT-GIT50-DELETE-DIR"}, agent,
    )
    assert not typed_dir.exists(), deleted_empty_dir
    assert "DELETED" in deleted_empty_dir and "WIN32_APPCONTAINER" in deleted_empty_dir, deleted_empty_dir
    print("GIT50_TYPED_FILE_AND_EMPTY_DIRECTORY_OS_DELETE_PASS", flush=True)

    # Direct subprocess injection cannot cross the one-shot broker boundary.
    unsupported = run_bounded_process(
        ["python", "-c", "print('NO_HOST_FALLBACK')"],
        cwd=workspace, security_command="python -c print('NO_HOST_FALLBACK')",
    )
    assert unsupported["security_rejected"] is True, unsupported
    assert unsupported["process_started"] is False, unsupported
    assert unsupported["exit_code"] is None, unsupported
    print("GIT50_DIRECT_SUBPROCESS_HOST_FALLBACK_DENIED_PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
