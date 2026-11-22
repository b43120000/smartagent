"""Git69 open-mode bypass IT: real Windows Runtime dispatch, no Planner/Host CMD fallback.

This is a fixed, fixture-scoped diagnostic and is NOT a permission bypass.
The --live CLI requires the installed effective ACL OFF, Turbo OFF and
Auto Approve ON policy. CI injects policy *only in its test process* and
labels that policy evidence CI_SIMULATED; process execution is real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

SCHEMA = "SMARTAGENT_GIT69_OPEN_FIDELITY_IT_V1"
_GUARD = {"acl": "OFF", "turbo_requested": "OFF", "turbo_effective": "OFF",
          "auto_approve_effective": "ON", "executor_backend": "WINDOWS_EXECUTOR"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _action(name: str, mode: str, command: str, cwd: Path, *,
            argv: list[str] | None = None, timeout: int = 20,
            operation: str = "GENERAL") -> dict:
    action = {"tool": "run_command", "operation": operation,
              "action_id": "ACT-G69-" + uuid.uuid4().hex.upper(),
              "shell_mode": mode, "command": command,
              "working_directory": str(cwd), "timeout": timeout}
    if argv is not None:
        action["argv"] = argv
    return action


def _cases(workspace: Path, fixture: Path) -> list[tuple[str, dict, dict]]:
    exe = str(Path(sys.executable).resolve())
    bat = fixture / "exit_37.bat"
    bat.write_bytes(b"@echo off\r\necho F69_BAT_STDERR 1>&2\r\nexit /b 37\r\n")
    script = ("import sys; sys.stdout.buffer.write(b' F69_DIRECT_OUT  \\r\\n'); "
              "sys.stderr.buffer.write(b'F69_DIRECT_ERR\\n'); sys.exit(9)")
    portable_git = Path(__file__).resolve().parents[2] / ".venv" / "git" / "cmd" / "git.exe"
    git_candidate = str(portable_git) if portable_git.is_file() else shutil.which("git.exe")
    if not git_candidate or not Path(git_candidate).is_file():
        raise RuntimeError("GIT69_OPEN_IT_GIT_EXECUTABLE_UNAVAILABLE")
    git_exe = str(Path(git_candidate).resolve())
    missing_postcondition = str(fixture / "never_created_verify_result.txt")
    verify = _action("VERIFY_EXPECTED_FAIL", "POWERSHELL",
                     "Write-Output 'F69_VERIFY_ZERO'", workspace, operation="TEST")
    verify.update({"success_criteria": "intentional negative postcondition probe",
                   "verify": [{"action": "file_exists",
                               "path": missing_postcondition, "expect": True}],
                   "expected_verification": {"status": "FAIL", "failed_checks": [1]}})
    return [
        ("PS_SUCCESS", _action("PS_SUCCESS", "POWERSHELL",
             "Write-Output 'F69_PS_OK'", workspace), {
             "exit_code": 0, "stdout": "F69_PS_OK", "shell_mode": "POWERSHELL"}),
        ("PS_THROW", _action("PS_THROW", "POWERSHELL",
             "throw 'F69_PS_THROW'", workspace), {
             "exit_code": 1, "stderr": "F69_PS_THROW", "failure_origin": "CHILD_PROCESS"}),
        ("PS_NONTERMINATING", _action("PS_NONTERMINATING", "POWERSHELL",
             "Write-Error 'F69_PS_ERR'; Write-Output 'F69_PS_AFTER'", workspace), {
             "exit_code": 0, "stderr": "F69_PS_ERR", "stdout": "F69_PS_AFTER"}),
        ("CMD_EXIT_7", _action("CMD_EXIT_7", "CMD",
             "echo F69_CMD_OK & echo F69_CMD_ERR 1>&2 & exit /b 7", workspace), {
             "exit_code": 7, "stdout": "F69_CMD_OK", "stderr": "F69_CMD_ERR",
             "failure_origin": "CHILD_PROCESS", "shell_mode": "CMD"}),
        ("CMD_BAT_37", _action("CMD_BAT_37", "CMD",
             'call "' + str(bat) + '"', workspace), {
             "exit_code": 37, "stderr": "F69_BAT_STDERR", "shell_mode": "CMD"}),
        ("DIRECT_EXIT_9", _action("DIRECT_EXIT_9", "DIRECT", exe, workspace,
             argv=["-c", script]), {
             "exit_code": 9, "stdout": "F69_DIRECT_OUT",
             "stderr": "F69_DIRECT_ERR", "failure_origin": "CHILD_PROCESS",
             "shell_mode": "DIRECT"}),
        ("DIRECT_EXIT_0", _action("DIRECT_EXIT_0", "DIRECT", exe, workspace,
             argv=["-c", "import sys; sys.stdout.buffer.write(b'F69_DIRECT_ZERO\\r\\n')"]), {
             "exit_code": 0, "stdout": "F69_DIRECT_ZERO", "shell_mode": "DIRECT"}),
        ("DIRECT_GIT_VERSION", _action("DIRECT_GIT_VERSION", "DIRECT",
             git_exe, workspace, argv=["--version"], operation="GIT_INSPECT"), {
             "exit_code": 0, "stdout": "git version", "shell_mode": "DIRECT"}),
        ("DIRECT_GIT_128", _action("DIRECT_GIT_128", "DIRECT",
             git_exe, workspace, argv=["-C", str(fixture), "rev-parse", "HEAD"],
             operation="GIT_INSPECT"), {
             "exit_code": 128, "stderr": "fatal", "failure_origin": "CHILD_PROCESS"}),
        ("DIRECT_UTF8", _action("DIRECT_UTF8", "DIRECT", exe, workspace,
             argv=["-c", "import sys; sys.stdout.buffer.write('F69_影像品質\\r\\n'.encode('utf-8'))"]), {
             "exit_code": 0, "stdout": "F69_影像品質", "shell_mode": "DIRECT"}),
        ("VERIFY_EXPECTED_FAIL", verify, {
             "exit_code": 0, "verification_status": "FAIL",
             "failure_origin": "VERIFICATION", "stdout": "F69_VERIFY_ZERO"}),
        ("DIRECT_MISSING", _action("DIRECT_MISSING", "DIRECT",
             str(fixture / "not_existing_git69.exe"), workspace, argv=["--version"]), {
             "exit_code": None, "process_started": False,
             "failure_origin": "PROCESS_START"}),
        ("DIRECT_TIMEOUT", _action("DIRECT_TIMEOUT", "DIRECT", exe, workspace,
             argv=["-c", "import time; time.sleep(4)"], timeout=1), {
             "exit_code": None, "timed_out": True, "possible_partial_execution": True}),
        ("DIRECT_TRUNCATED", _action("DIRECT_TRUNCATED", "DIRECT", exe, workspace,
             argv=["-c", "import sys; sys.stdout.buffer.write(b'Z'*12000000)"]), {
             "exit_code": 0, "stdout_truncated": True, "output_complete": False,
             "capture_integrity": "TRUNCATED"}),
    ]


def run_open_it(workspace: str | Path, *, policy_source: str = "INSTALLED_HOST") -> dict:
    """Same execute_tool, approval, bounded_process and evidence-store as Runtime."""
    from .auto_approve_mode import auto_approve_snapshot
    from .turbo_broker_admission import admit_action
    from .tools import execute_tool
    from .tool_result_contract import decode_result
    from .runtime_evidence_store import (
        persist_runtime_evidence, read_runtime_evidence, RuntimeEvidenceError,
    )
    from .shell_execution import execution_contract
    if os.name != "nt":
        raise RuntimeError("GIT69_OPEN_IT_WINDOWS_HOST_REQUIRED")
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("GIT69_OPEN_IT_WORKSPACE_UNAVAILABLE")
    state = auto_approve_snapshot(root)
    for key, expected in _GUARD.items():
        if state.get(key) != expected:
            raise RuntimeError("GIT69_OPEN_IT_POLICY_NOT_VERIFIED:" + key)
    install = Path(__file__).resolve().parents[2]
    task_id = "TASK-G69IT-" + uuid.uuid4().hex.upper()
    request_id = "RR-G69IT-" + uuid.uuid4().hex.upper()
    epoch = "EPOCH-G69IT-" + uuid.uuid4().hex.upper()
    agent = SimpleNamespace(
        workspace_root=root, workspace=root, current_task_id=task_id,
        current_request_id=request_id, current_task_epoch=epoch,
        interface_name="runtime_open_fidelity_it", _models_registry={},
        last_verification_status="UNVERIFIED",
    )
    report = {
        "schema": SCHEMA, "status": "RUNNING",
        "policy_source": policy_source,
        "policy_effective": {key: state[key] for key in _GUARD},
        "planner_invoked": False, "runtime_dispatch_shared": True,
        "process_backend_mocked": False,
        "task_id": task_id, "request_id": request_id, "task_epoch": epoch,
        "cases": [],
    }
    report_dir = install / "localdata" / "runtime" / "open_execution_fidelity_it"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / (task_id + ".json")

    def checkpoint():
        report["status"] = (
            "PASS" if report["cases"] and all(row["status"] == "PASS" for row in report["cases"])
            else "FAIL" if report["cases"] else "RUNNING")
        tmp = report_path.with_name(report_path.name + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, report_path)

    checkpoint()
    with tempfile.TemporaryDirectory(prefix="git69_open_it_", dir=root) as td:
        fixture = Path(td)
        for name, action, expect in _cases(root, fixture):
            row = {"case_id": name, "action_id": action["action_id"],
                   "shell_mode": action["shell_mode"], "status": "UNKNOWN"}
            try:
                if any(state.get(key) != value for key, value in _GUARD.items()):
                    raise RuntimeError("GIT69_OPEN_IT_STATE_CHANGED")
                fresh = auto_approve_snapshot(root)
                if any(fresh.get(key) != value for key, value in _GUARD.items()):
                    raise RuntimeError("GIT69_OPEN_IT_EFFECTIVE_POLICY_CHANGED")
                gate = admit_action(str(root), action)
                if gate.get("decision") != "NORMAL":
                    raise RuntimeError("GIT69_OPEN_IT_ADMISSION_NOT_NORMAL")
                shell = execution_contract(action)
                raw = execute_tool(action, agent=agent)
                receipt = decode_result(raw)
                if not isinstance(receipt, dict) or not isinstance(receipt.get("data"), dict):
                    raise RuntimeError("GIT69_OPEN_IT_TOOL_RESULT_UNPARSEABLE")
                data = receipt["data"]
                raw_hash = _digest(raw.encode("utf-8"))
                ref = "MIN-EV-" + _digest((action["action_id"] + "\0" + raw_hash).encode())[:24]
                pointer = persist_runtime_evidence(
                    install, task_id=task_id, request_id=request_id, task_epoch=epoch,
                    action_id=action["action_id"], evidence_ref=ref,
                    raw_result=raw, receipt=receipt)
                observation = read_runtime_evidence(
                    install, task_id=task_id, request_id=request_id,
                    task_epoch=epoch, evidence_ref=ref, section="metadata")
                checks = {
                    "evidence_persisted": pointer["store_status"] == "PERSISTED_AND_READBACK_VERIFIED",
                    "evidence_readback": observation["task_bound"]
                        and observation["source_result_sha256"] == raw_hash,
                    "launch_form": data.get("launch_form") == shell["launch_form"],
                    "shell_mode": data.get("shell_mode") == action["shell_mode"],
                    "not_native": not (data.get("sandbox_evidence") or {}).get(
                        "sandbox_identity_verified", False),
                }
                for key, expected in expect.items():
                    if key in {"stdout", "stderr"}:
                        checks[key + "_marker"] = expected in str(data.get(key) or "")
                    else:
                        checks[key] = type(data.get(key)) is type(expected) and data.get(key) == expected
                for stream in ("stdout", "stderr"):
                    digest = data.get(stream + "_raw_sha256")
                    value = data.get(stream)
                    if data.get(stream + "_truncated"):
                        local = ((data.get(stream + "_or_local_ref") or {}).get("local_ref")
                                 if isinstance(data.get(stream + "_or_local_ref"), dict) else "")
                        checks[stream + "_raw_digest"] = (
                            bool(local) and Path(local).is_file()
                            and _digest(Path(local).read_bytes()) == digest)
                    elif digest is not None and isinstance(value, str):
                        checks[stream + "_raw_digest"] = _digest(value.encode("utf-8")) == digest
                if name == "DIRECT_EXIT_9":
                    checks["auto_approval"] = (
                        (data.get("auto_approval") or {}).get("decision") == "AUTO_APPROVED")
                if name == "DIRECT_TIMEOUT":
                    checks["execution_unknown"] = receipt["status"] == "UNKNOWN"
                if name == "DIRECT_MISSING":
                    checks["not_executed"] = receipt.get("executed") is False
                row.update({
                    "status": "PASS" if all(checks.values()) else "FAIL",
                    "receipt_status": receipt.get("status"),
                    "execution_status": data.get("execution_status"),
                    "failure_origin": data.get("failure_origin"),
                    "process_state": data.get("process_state"),
                    "exit_code": data.get("exit_code"),
                    "stdout_bytes": data.get("stdout_bytes"),
                    "stderr_bytes": data.get("stderr_bytes"),
                    "stdout_raw_sha256": data.get("stdout_raw_sha256"),
                    "stderr_raw_sha256": data.get("stderr_raw_sha256"),
                    "stdout_truncated": data.get("stdout_truncated"),
                    "stderr_truncated": data.get("stderr_truncated"),
                    "output_complete": data.get("output_complete"),
                    "capture_integrity": data.get("capture_integrity"),
                    "launch_form": data.get("launch_form"),
                    "evidence_ref": ref,
                    "raw_result_sha256": raw_hash,
                    "checks": checks,
                })
            except Exception as exc:
                row.update({"status": "FAIL", "error_class": type(exc).__name__,
                            "error_code": str(exc)[:120] if re.fullmatch(
                                r"[A-Z0-9_:-]{1,120}", str(exc)) else "REDACTED_EXCEPTION"})
                report["cases"].append(row)
                checkpoint()
                # Fail closed on unexpected preflight/dispatch errors.
                break
            report["cases"].append(row)
            checkpoint()
    report["case_count"] = len(report["cases"])
    report["passed"] = sum(r["status"] == "PASS" for r in report["cases"])
    report["failed"] = sum(r["status"] == "FAIL" for r in report["cases"])
    checkpoint()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Git69 Open-mode real Runtime IT")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"schema": SCHEMA, "status": "BLOCKED",
                          "reason": "EXPLICIT_LIVE_OPT_IN_REQUIRED"}))
        return 2
    try:
        report = run_open_it(args.workspace)
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"schema": SCHEMA, "status": "BLOCKED",
                          "reason": str(exc) if re.fullmatch(
                              r"[A-Z0-9_:-]{1,140}", str(exc))
                              else type(exc).__name__}))
        return 2
    print(json.dumps({
        "schema": SCHEMA, "status": report["status"],
        "task_id": report["task_id"], "request_id": report["request_id"],
        "passed": report["passed"], "failed": report["failed"],
        "cases": [{"case_id": x["case_id"], "status": x["status"]}
                  for x in report["cases"]],
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
