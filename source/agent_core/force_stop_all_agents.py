#!/usr/bin/env python3
from __future__ import annotations

"""Force-stop only processes owned by this SmartAgent checkout."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .conversation_ownership import ConversationOwnershipRegistry
from .protocol_manifest import validate_protocol_manifest, write_protocol_manifest
from .paths import force_stop_flag_path, webgpt_submit_lock_path
from .remote_clean_start import remote_clean_start
from .runtime_cleanup import INTERFACES, RuntimeCleanupManager
from .webgpt_rate_governor import WebGPTRateGovernor


STOP_FLAG_NAME = "force_stop_all_agents.flag"
LAUNCHER_NAMES = frozenset({
    "launch_smart_agent.bat",
    "launch_remote_agent.bat",
    "launch_webcopilot_chatgpt.bat",
    "adapterui.bat",
    "launch_web_copilot.bat",
    "remoteagent.bat",
})
AGENT_COMMAND_MARKERS = (
    "-m agent_core.host_supervisor",
    "-m agent_core.runtime_monitor",
    "-m remoteagent.telegram_webagent_worker",
    "\\remoteagent\\hidden_supervisor.py",
    "\\remoteagent\\remote_agent.py",
    "-m webagent.controller",
    "-m agent_core.ui_calibration",
    "\\smart_agent.py",
    "\\web_copilot.py",
)


def _powershell_process_inventory() -> list[dict[str, Any]]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "agent_process_inventory_failed:" + (result.stderr.strip() or "unknown")
        )
    raw = str(result.stdout or "").lstrip("\ufeff").strip()
    if not raw:
        return []
    decoded = json.loads(raw)
    rows = decoded if isinstance(decoded, list) else [decoded]
    return [dict(row) for row in rows if isinstance(row, dict)]


def discover_agent_pids(
    processes: list[dict[str, Any]], root: str | Path, *, exclude_pid: int = 0,
    exclude_pids: set[int] | None = None,
) -> dict[str, Any]:
    """Find verified Agent roots, descendants, and launcher CMD ancestors."""
    root_text = str(Path(root).resolve()).replace("/", "\\").rstrip("\\").lower()
    rows: dict[int, dict[str, Any]] = {}
    children: dict[int, set[int]] = {}
    for raw in processes:
        try:
            pid = int(raw.get("ProcessId", 0) or 0)
            ppid = int(raw.get("ParentProcessId", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        row = dict(raw)
        row["ProcessId"] = pid
        row["ParentProcessId"] = ppid
        rows[pid] = row
        children.setdefault(ppid, set()).add(pid)

    seeds: set[int] = set()
    for pid, row in rows.items():
        command = str(row.get("CommandLine") or "").replace("/", "\\").lower()
        executable = str(row.get("ExecutablePath") or "").replace("/", "\\").lower()
        belongs_to_checkout = root_text in command or executable.startswith(root_text + "\\")
        if belongs_to_checkout and any(marker in command for marker in AGENT_COMMAND_MARKERS):
            seeds.add(pid)

    selected = set(seeds)
    frontier = list(seeds)
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, set()):
            if child not in selected:
                selected.add(child)
                frontier.append(child)

    launchers: set[int] = set()
    for seed in list(seeds):
        parent = int(rows.get(seed, {}).get("ParentProcessId", 0) or 0)
        visited: set[int] = set()
        while parent in rows and parent not in visited:
            visited.add(parent)
            row = rows[parent]
            name = str(row.get("Name") or "").lower()
            command = str(row.get("CommandLine") or "").replace("/", "\\").lower()
            if name in {"cmd.exe", "cmd"} and any(value in command for value in LAUNCHER_NAMES):
                launchers.add(parent)
                selected.add(parent)
                break
            parent = int(row.get("ParentProcessId", 0) or 0)

    excluded = {
        int(pid) for pid in {int(exclude_pid or 0), *(exclude_pids or set())}
        if int(pid) > 0
    }
    selected.difference_update(excluded)
    roots = sorted(
        pid for pid in selected
        if int(rows.get(pid, {}).get("ParentProcessId", 0) or 0) not in selected
    )
    return {
        "seed_pids": sorted(seeds),
        "launcher_pids": sorted(launchers),
        "selected_pids": sorted(selected),
        "root_pids": roots,
    }


def _request_orderly_shutdown(root: Path) -> dict[str, bool]:
    requested: dict[str, bool] = {}
    for interface in sorted(INTERFACES):
        manager = RuntimeCleanupManager(root, interface)
        generation = str(manager.read_state().get("generation_id", "") or "")
        if not generation:
            requested[interface] = False
            continue
        try:
            requested[interface] = manager.request_shutdown(
                generation, reason="FORCE_STOP_ALL_AGENTS"
            )
        except (OSError, RuntimeError, ValueError):
            requested[interface] = False
    return requested


def _taskkill_tree(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def _finalize_runtime_states(root: Path) -> dict[str, bool]:
    finalized: dict[str, bool] = {}
    for interface in sorted(INTERFACES):
        manager = RuntimeCleanupManager(root, interface)
        state = manager.read_state()
        generation = str(state.get("generation_id", "") or "")
        if not generation:
            finalized[interface] = True
            continue
        try:
            finalized[interface] = manager.finalize_abandoned_runtime(
                generation, reason="FORCE_STOP_ALL_AGENTS"
            ) or str(manager.read_state().get("status", "")).upper() == "STOPPED"
        except (OSError, RuntimeError, ValueError):
            finalized[interface] = False
    return finalized


def _sync_startup_contract(root: Path) -> dict[str, Any]:
    """Rebuild and validate manifests only after all Agent writers are stopped."""
    import compileall

    checked: list[str] = []
    for tree in (root, root / "release"):
        if tree == root / "release" and not tree.is_dir():
            continue
        write_protocol_manifest(tree)
        validate_protocol_manifest(tree)
        checked.append(str(tree))
    compile_ok = all(
        compileall.compile_dir(str(root / folder), quiet=1, force=True)
        for folder in ("agent_core", "RemoteAgent", "WebAgent")
        if (root / folder).is_dir()
    )
    if not compile_ok:
        raise RuntimeError("startup_contract_python_compile_failed")
    return {"manifest_roots": checked, "python_compile": True}


def force_stop_all(
    root: str | Path, *, dry_run: bool = False, exclude_pids: set[int] | None = None
) -> dict[str, Any]:
    project = Path(root).resolve()
    excluded = {os.getpid(), *(int(pid) for pid in (exclude_pids or set()) if int(pid) > 0)}
    inventory = _powershell_process_inventory()
    discovered = discover_agent_pids(inventory, project, exclude_pids=excluded)
    result: dict[str, Any] = {
        "dry_run": bool(dry_run), "excluded_pids": sorted(excluded), **discovered
    }
    if dry_run:
        return result

    stop_flag = force_stop_flag_path(project)
    stop_flag.parent.mkdir(parents=True, exist_ok=True)
    stop_flag.write_text(
        json.dumps({"reason": "FORCE_STOP_ALL_AGENTS", "created_at": time.time()}),
        encoding="utf-8",
    )
    result["shutdown_requested"] = _request_orderly_shutdown(project)
    try:
        result["remote_clean_start"] = remote_clean_start(
            project, reason="FORCE_STOP_ALL_AGENTS"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        result["remote_clean_start"] = {"error": f"{type(exc).__name__}: {exc}"}

    stopped: list[int] = []
    failed: list[int] = []
    for pid in discovered["root_pids"]:
        (stopped if _taskkill_tree(pid) else failed).append(pid)
    time.sleep(0.5)
    # One bounded second sweep closes a launcher/worker that crossed the first
    # inventory boundary.  This is intentionally not an endless kill loop.
    second = discover_agent_pids(
        _powershell_process_inventory(), project, exclude_pids=excluded
    )
    second_stopped: list[int] = []
    second_failed: list[int] = []
    for pid in second["root_pids"]:
        (second_stopped if _taskkill_tree(pid) else second_failed).append(pid)
    if second["root_pids"]:
        time.sleep(0.5)
    final = discover_agent_pids(
        _powershell_process_inventory(), project, exclude_pids=excluded
    )
    remaining = set(final["selected_pids"])
    exited_during_stop = sorted(
        pid for pid in {*failed, *second_failed} if pid not in remaining
    )
    result["stopped_root_pids"] = stopped
    result["failed_root_pids"] = [pid for pid in failed if pid in remaining]
    result["second_sweep_root_pids"] = second["root_pids"]
    result["second_sweep_stopped_pids"] = second_stopped
    result["second_sweep_failed_pids"] = [
        pid for pid in second_failed if pid in remaining
    ]
    result["exited_during_stop_pids"] = exited_during_stop
    result["remaining_agent_pids"] = final["selected_pids"]
    result["runtime_states_finalized"] = _finalize_runtime_states(project)
    submit_lock = webgpt_submit_lock_path(project)
    result["submit_lock_recovered"] = RuntimeCleanupManager.recover_stale_owned_file(
        submit_lock
    )
    result["submit_lock_remaining"] = submit_lock.exists()
    try:
        WebGPTRateGovernor(project).reset_for_force_stop()
        result["webgpt_rate_state_reset"] = True
    except (OSError, RuntimeError, ValueError) as exc:
        result["webgpt_rate_state_reset"] = {
            "error": f"{type(exc).__name__}: {exc}"
        }
    result["conversation_owners_remaining"] = (
        ConversationOwnershipRegistry(project).prune_dead_owners()
    )
    runtime_clean = all(result["runtime_states_finalized"].values())
    rate_clean = result.get("webgpt_rate_state_reset") is True
    process_clean = not final["selected_pids"]
    lock_clean = not result["submit_lock_remaining"]
    result["startup_contract"] = {"status": "SKIPPED"}
    if process_clean and runtime_clean and rate_clean and lock_clean:
        try:
            result["startup_contract"] = {
                "status": "PASS",
                **_sync_startup_contract(project),
            }
        except (OSError, RuntimeError, ValueError) as exc:
            result["startup_contract"] = {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            }
    result["success"] = (
        process_clean
        and runtime_clean
        and rate_clean
        and lock_clean
        and result["startup_contract"].get("status") == "PASS"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force-stop this checkout's Agent processes")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--exclude-pid", action="append", type=int, default=[])
    args = parser.parse_args(argv)
    try:
        result = force_stop_all(
            args.root, dry_run=args.dry_run, exclude_pids=set(args.exclude_pid)
        )
    except Exception as exc:
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.dry_run or result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["discover_agent_pids", "force_stop_all"]
