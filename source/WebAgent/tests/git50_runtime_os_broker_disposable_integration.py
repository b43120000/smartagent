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

    # The typed file write adapter is still intentionally unimplemented.
    # Its rejection must be decisive, not a privileged Python fallback.
    blocked_target = root / "typed_write_must_not_run.txt"
    blocked = execute_tool(
        {"tool": "write_file", "path": str(blocked_target),
         "content": "UNSAFE", "action_id": "ACT-GIT50-TYPED"},
        SimpleNamespace(workspace=str(workspace)),
    )
    assert not blocked_target.exists(), blocked
    assert "TURBO_OS_BROKER_BLOCKED" in blocked, blocked
    assert "NOT_EXECUTED" in blocked, blocked
    print("GIT50_UNADAPTED_TYPED_MUTATOR_FAIL_CLOSED_PASS", flush=True)

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
