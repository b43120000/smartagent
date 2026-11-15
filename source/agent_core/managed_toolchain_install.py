"""Git63 noninteractive managed installer Action: no Host CMD fallback."""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from .managed_toolchains import (
    select_package, managed_hints, register_install_receipt,
)
from .paths import install_root
from .tool_result_contract import encode_result

_SAFE = re.compile(r"\bGIT63_[A-Z0-9_]{3,100}\b")


def install_toolchain_action(action: dict, *, root=None) -> str:
    """Only a catalog tool/version, never Planner URL, path, script or command.

    First-time administrator setup is a separate, explicit operation.
    Once provisioned, the installer has its own restricted AppContainer SID
    and can install without a per-task UAC or command-policy exception.
    """
    tool, version = action.get("name"), action.get("version")
    root = install_root(root)
    try:
        select_package(tool, version, root)
    except (ValueError, OSError, TypeError):
        return encode_result("REJECTED", "GIT63_TOOL_NOT_IN_TRUSTED_CATALOG",
            tool="install_toolchain", executed=False, data={"terminal_eligible":False})
    from .runtime_environment import read_environment_ledger
    for row in managed_hints(read_environment_ledger(root), root):
        if row["name"] == tool and row["version"] == version:
            return encode_result("SUCCEEDED", "GIT63_TOOL_ALREADY_DISCOVERABLE",
                tool="install_toolchain", executed=False,
                data={"installation_status":"RECORDED_REPROBE_REQUIRED",
                      "tool_paths":{tool:row["path"]},
                      "native_execution_verified":False,
                      "terminal_eligible":False})
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
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=365, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return encode_result("UNKNOWN", "GIT63_INSTALL_PROCESS_OUTCOME_UNCERTAIN",
            tool="install_toolchain", executed=False,
            data={"possible_partial_execution":True,"terminal_eligible":False})
    if result.returncode:
        matched = _SAFE.search(result.stderr[-4096:])
        reason = matched.group() if matched else "GIT63_INSTALLER_REJECTED"
        return encode_result("UNKNOWN", reason, tool="install_toolchain",
            executed=False, data={"possible_partial_execution":True,
                                  "terminal_eligible":False})
    try:
        record = json.loads(result.stdout.strip().splitlines()[-1])
        committed = register_install_receipt(record, root=root)
    except (KeyError, ValueError, TypeError, OSError, IndexError) as exc:
        return encode_result("UNKNOWN", "GIT63_INSTALL_RECEIPT_UNVERIFIED",
            tool="install_toolchain", executed=False,
            data={"possible_partial_execution":True, "terminal_eligible":False})
    return encode_result("SUCCEEDED", "GIT63_MANAGED_TOOL_INSTALLED",
        tool="install_toolchain", executed=True,
        data={"installation_status":"INSTALLED",
              "tool_paths":committed["tool_paths"],
              "native_execution_verified":False,
              "execution_reprobe_required":True,
              "install_scope":"SMARTAGENT_LOCALDATA_TOOLCHAINS_ONLY",
              "terminal_eligible":False})
