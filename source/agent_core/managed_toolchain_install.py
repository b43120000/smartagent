"""Git63 noninteractive managed installer Action: no Host CMD fallback."""
from __future__ import annotations
import json
import base64
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
    """Install a source validated by Runtime; no Planner command or target path.

    First-time administrator setup is a separate, explicit operation.
    Once provisioned, the installer has its own restricted AppContainer SID
    and can install without a per-task UAC or command-policy exception.
    """
    tool, version = action.get("name"), action.get("version")
    root = install_root(root)
    source = action.get("source")
    # Source is a bounded declarative archive/checksum descriptor, not a script.
    try:
        package = select_package(tool, version, root, source=source)
    except (ValueError, OSError, TypeError) as exc:
        reason = str(exc) if str(exc).startswith("GIT63_") else "GIT63_SOURCE_INVALID"
        return encode_result("REJECTED", reason,
            tool="install_toolchain", executed=False, data={"terminal_eligible":False})
    from .runtime_environment import read_environment_ledger
    ledger = read_environment_ledger(root)
    installed_source = (ledger.get("managed_sources") or {}).get(f"{tool}:{version}")
    if source is not None and installed_source is not None and package != installed_source:
        return encode_result("REJECTED", "GIT63_MANAGED_VERSION_SOURCE_CONFLICT",
            tool="install_toolchain", executed=False, data={"terminal_eligible":False})
    reusable = [row for row in managed_hints(ledger, root)
                if row["tool"] == tool and row["version"] == version]
    if reusable:
        return encode_result("SUCCEEDED", "GIT63_TOOL_ALREADY_DISCOVERABLE",
            tool="install_toolchain", executed=False,
            data={"installation_status":"RECORDED_REPROBE_REQUIRED",
                  "tool_paths":{f'{row["name"]}:{version}':row["path"]
                                for row in reusable},
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
    if source is not None:
        descriptor = {key:package[key] for key in
                      ("url", "archive_sha256", "checksum_url", "executables", "archive_type")}
        encoded = base64.b64encode(json.dumps(descriptor, separators=(",", ":")).encode("utf-8"))
        args.extend(["-SourceSpecBase64", encoded.decode("ascii")])
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
        committed = register_install_receipt(record, root=root, source=source)
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
