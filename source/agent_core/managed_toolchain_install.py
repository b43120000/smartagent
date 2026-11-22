"""Git63 noninteractive managed installer Action: no Host CMD fallback.

Git63 Process Liveness: monitor the installer wrapper via an OS-owned handle.
A live wrapper never constitutes proof that its AppContainer child succeeded.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .managed_toolchains import select_package, managed_hints, register_install_receipt
from .paths import install_root
from .process_liveness import observe_owned_process, communicate_with_liveness
from .tool_result_contract import encode_result

_SAFE = re.compile(r"\bGIT63_[A-Z0-9_]{3,100}\b")


def install_toolchain_action(action: dict, *, root=None, telemetry: dict | None = None) -> str:
    """Install only a Runtime-validated source through the dedicated broker.

    A preprovisioned restricted AppContainer performs the download/install.
    This function never runs an arbitrary Planner shell command.
    """
    tool, version = action.get("name"), action.get("version")
    root = install_root(root)
    source = action.get("source")
    try:
        package = select_package(tool, version, root, source=source)
    except (ValueError, OSError, TypeError) as exc:
        reason = str(exc) if str(exc).startswith("GIT63_") else "GIT63_SOURCE_INVALID"
        return encode_result("REJECTED", reason, tool="install_toolchain",
                             executed=False, data={"terminal_eligible": False})

    from .runtime_environment import read_environment_ledger
    ledger = read_environment_ledger(root)
    installed_source = (ledger.get("managed_sources") or {}).get(f"{tool}:{version}")
    if source is not None and installed_source is not None and package != installed_source:
        return encode_result("REJECTED", "GIT63_MANAGED_VERSION_SOURCE_CONFLICT",
                             tool="install_toolchain", executed=False,
                             data={"terminal_eligible": False})
    reusable = [row for row in managed_hints(ledger, root)
                if row["tool"] == tool and row["version"] == version]
    if reusable:
        return encode_result("SUCCEEDED", "GIT63_TOOL_ALREADY_DISCOVERABLE",
                             tool="install_toolchain", executed=False,
                             data={"installation_status": "RECORDED_REPROBE_REQUIRED",
                                   "tool_paths": {f'{row["name"]}:{version}': row["path"]
                                                  for row in reusable},
                                   "native_execution_verified": False,
                                   "terminal_eligible": False})
    if os.name != "nt":
        return encode_result("REJECTED", "GIT63_WINDOWS_NATIVE_REQUIRED",
                             tool="install_toolchain", executed=False)

    script = root / "install_smart_agent" / "git63_toolchain_install.ps1"
    exe = Path(os.environ.get("SystemRoot") or r"C:\Windows") / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    args = [str(exe), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "RemoteSigned", "-File", str(script),
            "-ProjectRoot", str(root), "-Tool", tool, "-Version", version]
    if source is not None:
        descriptor = {key: package[key] for key in
                      ("url", "archive_sha256", "checksum_url", "executables", "archive_type")}
        encoded = base64.b64encode(json.dumps(
            descriptor, separators=(",", ":")).encode("utf-8"))
        args.extend(["-SourceSpecBase64", encoded.decode("ascii")])

    metadata = dict(telemetry or {})
    telemetry_root = metadata.get("root")
    telemetry_task = metadata.get("task_id")
    execution_id = "EXEC-" + uuid.uuid4().hex.upper()
    last_running_evidence = None
    process = None

    def write_progress(**fields):
        if telemetry_root and telemetry_task:
            try:
                from .execution_telemetry import write_snapshot
                write_snapshot(telemetry_root, telemetry_task,
                               action_id=str(metadata.get("action_id") or ""), **fields)
            except OSError:
                # Progress snapshots cannot change the outcome of a live install.
                pass

    def record_running(proof):
        nonlocal last_running_evidence
        if proof.get("liveness_verified") is not True:
            return
        last_running_evidence = proof
        write_progress(
            state="RUNNING", execution_id=execution_id, pid=proof.get("pid"),
            process_role="TOOLCHAIN_INSTALLER_WRAPPER",
            liveness_verified=True, liveness_evidence=proof,
            liveness_observed_at=proof.get("observed_at"),
        )

    write_progress(state="STARTING", execution_id=execution_id,
                   process_role="TOOLCHAIN_INSTALLER_WRAPPER",
                   liveness_verified=False, started_at=time.time())
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", shell=False,
        )
        stdout, stderr = communicate_with_liveness(
            process, 365, on_running=record_running,
            execution_id=execution_id, interval=2.0,
        )
        exit_code = process.returncode
        final_proof = observe_owned_process(process, execution_id=execution_id)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.communicate(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        write_progress(state="UNKNOWN", execution_id=execution_id,
                       process_role="TOOLCHAIN_INSTALLER_WRAPPER",
                       liveness_verified=False, error=type(exc).__name__)
        return encode_result(
            "UNKNOWN", "GIT63_INSTALL_PROCESS_OUTCOME_UNCERTAIN",
            tool="install_toolchain", executed=False,
            data={"possible_partial_execution": process is not None,
                  "execution_id": execution_id,
                  "process_state": "UNKNOWN",
                  "process_role": "TOOLCHAIN_INSTALLER_WRAPPER",
                  "last_running_evidence": last_running_evidence,
                  "failure_class": type(exc).__name__,
                  "terminal_eligible": False},
        )

    write_progress(state="EXITED", execution_id=execution_id,
                   process_role="TOOLCHAIN_INSTALLER_WRAPPER",
                   liveness_verified=False, liveness_evidence=final_proof,
                   exit_code=exit_code, ended_at=time.time())
    witness = {
        "execution_id": execution_id,
        "process_role": "TOOLCHAIN_INSTALLER_WRAPPER",
        "process_state": "EXITED" if exit_code is not None else final_proof["process_state"],
        "exit_code": exit_code,
        "liveness_evidence": final_proof,
        "last_running_evidence": last_running_evidence,
    }
    if exit_code:
        matched = _SAFE.search(stderr[-4096:])
        reason = matched.group() if matched else "GIT63_INSTALLER_REJECTED"
        return encode_result("UNKNOWN", reason, tool="install_toolchain",
                             executed=False,
                             data={"possible_partial_execution": True,
                                   **witness, "terminal_eligible": False})
    try:
        record = json.loads(stdout.strip().splitlines()[-1])
        committed = register_install_receipt(record, root=root, source=source)
    except (KeyError, ValueError, TypeError, OSError, IndexError):
        return encode_result("UNKNOWN", "GIT63_INSTALL_RECEIPT_UNVERIFIED",
                             tool="install_toolchain", executed=False,
                             data={"possible_partial_execution": True,
                                   **witness, "terminal_eligible": False})
    return encode_result(
        "SUCCEEDED", "GIT63_MANAGED_TOOL_INSTALLED",
        tool="install_toolchain", executed=True,
        data={"installation_status": "INSTALLED",
              "tool_paths": committed["tool_paths"],
              **witness, "native_execution_verified": False,
              "execution_reprobe_required": True,
              "install_scope": "SMARTAGENT_LOCALDATA_TOOLCHAINS_ONLY",
              "terminal_eligible": False},
    )
