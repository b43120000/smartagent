#!/usr/bin/env python3
from __future__ import annotations

"""Force-stop only processes owned by this SmartAgent checkout."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .conversation_ownership import ConversationOwnershipRegistry
from .protocol_manifest import CORE_FILES, validate_protocol_manifest
from .paths import force_stop_flag_path, webgpt_submit_lock_path
from .paths import runtime_root
from .process_file_lock import exclusive_process_lock
from .remote_clean_start import remote_clean_start
from .runtime_cleanup import INTERFACES, RuntimeCleanupManager
from .webgpt_rate_governor import WebGPTRateGovernor


STOP_FLAG_NAME = "force_stop_all_agents.flag"
LAUNCHER_NAMES = frozenset({
    "launch_smart_agent.bat",
    "launch_remote_agent.bat",
    "launch_webcopilot.bat",
    "adapterui.bat",
    "launch_web_copilot.bat",
    "remoteagent.bat",
})
AGENT_COMMAND_MARKERS = (
    "-m agent_core.runtime_cleanup prepare",
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
        raise RuntimeError("agent_process_inventory_empty_refusing_cleanup")
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

    # Only Agent executables/scripts or explicit --root can establish
    # installation ownership. A foreign Agent whose --workspace refers to
    # this checkout must NOT become a kill candidate.
    rooted_arg = re.compile(
        r"(?:^|\s)--root(?:=|\s+)[\"']?"
        + re.escape(root_text)
        + r"(?=$|[\s\"'])"
    )
    rooted_scripts = (
        root_text + "\\smart_agent.py",
        root_text + "\\web_copilot.py",
        root_text + "\\source\\",
        root_text + "\\remoteagent\\",
    )
    explicit_root_arg = re.compile(
        r"(?:^|\s)--root(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|(\S+))"
    )
    seeds: set[int] = set()
    for pid, row in rows.items():
        command = str(row.get("CommandLine") or "").replace("/", "\\").lower()
        executable = str(row.get("ExecutablePath") or "").replace("/", "\\").lower()
        explicit = explicit_root_arg.search(command)
        if explicit:
            declared = next((part for part in explicit.groups() if part), "")
            # A local Python executable may be reused to run an Agent with
            # a foreign explicit --root. Respect the runtime owner, not the
            # interpreter's location.
            if declared.rstrip("\\") != root_text:
                continue
        belongs_to_checkout = (
            executable.startswith(root_text + "\\")
            or bool(rooted_arg.search(command))
            or any(script in command for script in rooted_scripts)
        )
        if belongs_to_checkout and any(marker in command for marker in AGENT_COMMAND_MARKERS):
            seeds.add(pid)

    # Never recursively select all child PIDs: a legitimate Agent may spawn
    # a program located in another checkout. Stopping it would violate the
    # local-install boundary. Only independently attested Agent PIDs qualify.
    selected = set(seeds)

    launchers: set[int] = set()
    for seed in list(seeds):
        parent = int(rows.get(seed, {}).get("ParentProcessId", 0) or 0)
        visited: set[int] = set()
        while parent in rows and parent not in visited:
            visited.add(parent)
            row = rows[parent]
            name = str(row.get("Name") or "").lower()
            command = str(row.get("CommandLine") or "").replace("/", "\\").lower()
            if (name in {"cmd.exe", "cmd"}
                    and any(root_text + "\\" + value in command
                            for value in LAUNCHER_NAMES)):
                # Kill only a CMD that names a launcher *inside this checkout*.
                # A generic shared CMD ancestor is not owned by SmartAgent.
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


def _taskkill_owned_pid(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/PID", str(int(pid)), "/F"],
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


def _verify_startup_contract(root: Path) -> dict[str, Any]:
    report = validate_protocol_manifest(root)
    if report["hash_verification"] != "PASS":
        raise RuntimeError("protocol_manifest_source_unverified")
    return {"manifest_roots": [str(root)], "hash_verification": "PASS"}


def _machine_manifest_status(root: Path) -> str:
    from .windows_security import load_machine_authorization, machine_authorization_path

    path = machine_authorization_path()
    if not path.is_file():
        return "machine_authorization_missing"
    authorization = json.loads(path.read_bytes())
    if Path(authorization["install_root"]).resolve() != root:
        return "machine_authorization_install_root_mismatch"
    try:
        if load_machine_authorization() is None:
            return "machine_authorization_missing"
    except RuntimeError as exc:
        return str(exc).split(":", 1)[0]
    return "MATCH"


def _verify_upgrade_commit(root: Path, commit: str) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)

    if git("rev-parse", "HEAD").decode().strip() != commit.lower():
        raise RuntimeError("force_stop_upgrade_head_mismatch")
    if Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip())).resolve() != root:
        raise RuntimeError("force_stop_upgrade_git_root_mismatch")
    relative = "config/protocol_manifest.json"
    manifest = root / relative
    if hashlib.sha256(manifest.read_bytes()).digest() != hashlib.sha256(
        git("show", f"{commit}:{relative}")
    ).digest():
        raise RuntimeError("force_stop_upgrade_manifest_differs_from_commit")
    status = git("status", "--porcelain", "--untracked-files=all", "--",
                 relative, *CORE_FILES)
    if status:
        raise RuntimeError("force_stop_upgrade_managed_files_modified")
    _verify_startup_contract(root)


def _apply_git_upgrade(root: Path, commit: str) -> None:
    from .security_preflight import preflight
    from .windows_security import acl_mode

    _verify_upgrade_commit(root, commit)
    profile = root / "localdata" / "secure" / "windows_security" / "security_profile.json"
    if acl_mode(profile) != "on" or not profile.is_file():
        raise RuntimeError("force_stop_upgrade_requires_existing_acl_on")
    status = _machine_manifest_status(root)
    if status == "machine_authorization_manifest_mismatch":
        if profile.with_name("turbo_os_activation.json").is_file():
            raise RuntimeError("GIT50_TURBO_BROKER_REVOKE_REQUIRED_BEFORE_ACL_ON_RECONFIGURE")
        subprocess.run(
            [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", "ACLstatus.bat", "on"],
            cwd=root, check=True,
        )
        _verify_upgrade_commit(root, commit)
        status = _machine_manifest_status(root)
    if status != "MATCH":
        raise RuntimeError(status)
    report = preflight("remote")
    if report.get("status") != "PASS":
        reasons = (report.get("attestation") or {}).get("reasons") or []
        raise RuntimeError("force_stop_upgrade_preflight_failed:" + ",".join(reasons))


def force_stop_all(
    root: str | Path, *, dry_run: bool = False, exclude_pids: set[int] | None = None
) -> dict[str, Any]:
    if dry_run:
        return _force_stop_all_locked(root, dry_run=True, exclude_pids=exclude_pids)
    with exclusive_process_lock(
        runtime_root(root) / "force_stop_cleanup.lock",
        timeout_sec=5.0, label="force-stop/startup barrier",
    ):
        return _force_stop_all_locked(root, exclude_pids=exclude_pids)


def _force_stop_all_locked(
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
    stopped: list[int] = []
    failed: list[int] = []
    for pid in discovered["root_pids"]:
        (stopped if _taskkill_owned_pid(pid) else failed).append(pid)
    time.sleep(0.5)
    # One bounded second sweep closes a launcher/worker that crossed the first
    # inventory boundary.  This is intentionally not an endless kill loop.
    second = discover_agent_pids(
        _powershell_process_inventory(), project, exclude_pids=excluded
    )
    second_stopped: list[int] = []
    second_failed: list[int] = []
    for pid in second["root_pids"]:
        (second_stopped if _taskkill_owned_pid(pid) else second_failed).append(pid)
    if second["root_pids"]:
        time.sleep(0.5)
    final_inventory = _powershell_process_inventory()
    final = discover_agent_pids(final_inventory, project, exclude_pids=excluded)
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
    # Includes explicitly excluded live Agents: their work must not be cancelled.
    from .force_stop_cleanup import cleanup_stopped_work
    result["work_cleanup"] = cleanup_stopped_work(project, final_inventory, _barrier_held=True)
    if not result["work_cleanup"].get("success"):
        result["success"] = False
        result["startup_contract"] = {"status": "SKIPPED", "reason": "work_cleanup_blocked"}
        return result
    if result["work_cleanup"].get("success"):
        try:
            # All incomplete tasks are now terminal; this only clears remaining
            # control/approval/ownership reservations, never raw metadata PIDs.
            result["remote_clean_start"] = remote_clean_start(
                project, reason="FORCE_STOP_ALL_AGENTS",
                local_checkout_only=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            result["remote_clean_start"] = {"error": f"{type(exc).__name__}: {exc}"}
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
    work_clean = result["work_cleanup"].get("success") is True
    controls_clean = "error" not in result.get("remote_clean_start", {"error": "not_run"})
    if process_clean and runtime_clean and rate_clean and lock_clean and work_clean and controls_clean:
        try:
            result["startup_contract"] = {
                "status": "PASS",
                **_verify_startup_contract(project),
                "machine_authorization": _machine_manifest_status(project),
            }
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            result["startup_contract"] = {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            }
    result["success"] = (
        process_clean
        and runtime_clean
        and rate_clean
        and lock_clean
        and work_clean
        and controls_clean
    )
    # Still inside the startup barrier: include excluded/blocked new launchers.
    last = discover_agent_pids(_powershell_process_inventory(), project, exclude_pids={os.getpid()})
    result["remaining_agent_pids"] = last["selected_pids"]
    if last["selected_pids"]:
        result["success"] = False
        result["late_start_detected"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force-stop this checkout's Agent processes")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--exclude-pid", action="append", type=int, default=[])
    parser.add_argument("--after-git-update", metavar="COMMIT")
    args = parser.parse_args(argv)
    if args.after_git_update is not None and (
        args.dry_run or not re.fullmatch(r"[0-9a-fA-F]{40}", args.after_git_update)
    ):
        parser.error("--after-git-update requires a full commit SHA and a real stop")
    try:
        exclusions = set(args.exclude_pid)
        if args.after_git_update is None:
            result = force_stop_all(args.root, dry_run=args.dry_run, exclude_pids=exclusions)
        else:
            root = Path(args.root).resolve()
            with exclusive_process_lock(
                runtime_root(root) / "force_stop_cleanup.lock",
                timeout_sec=5.0, label="force-stop/startup barrier",
            ):
                result = _force_stop_all_locked(root, exclude_pids=exclusions)
                if result.get("success"):
                    try:
                        _apply_git_upgrade(root, args.after_git_update)
                        result["git_upgrade"] = {"status": "PASS"}
                    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                        result["git_upgrade"] = {"status": "FAIL", "error": str(exc)}
                        result["success"] = False
                else:
                    result["git_upgrade"] = {"status": "SKIPPED", "reason": "force_stop_failed"}
    except Exception as exc:
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.dry_run or result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["discover_agent_pids", "force_stop_all"]
