#!/usr/bin/env python3
"""Visible monitor for shared Tri-One runtime events."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .runtime_cleanup import INTERFACES, RuntimeCleanupManager, _atomic_json
from .process_file_lock import _pid_alive


def run_monitor(
    root: str | Path,
    interface: str,
    generation_id: str,
    *,
    poll_sec: float = 0.25,
) -> int:
    manager = RuntimeCleanupManager(root, interface)
    state = manager.read_state()
    if str(state.get("generation_id", "")) != str(generation_id):
        raise RuntimeError("monitor_generation_mismatch")
    monitor_state = {
        "runtime_protocol": "TRI_ONE_RUNTIME_MONITOR_V1",
        "interface": interface,
        "generation_id": generation_id,
        "status": "RUNNING",
        "pid": os.getpid(),
        "heartbeat_at": time.time(),
    }
    _atomic_json(manager.monitor_state_path, monitor_state)
    print(f"[{interface}][{generation_id}] MONITOR_READY", flush=True)
    event_path = manager.events.path
    offset = 0
    owner_seen = False
    owner_lost_at = 0.0
    startup_deadline = time.monotonic() + 15.0
    try:
        while True:
            if event_path.is_file():
                with event_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if (
                            event.get("interface") == interface
                            and event.get("generation_id") == generation_id
                        ):
                            request = str(event.get("request_id", "") or "")
                            prefix = f"[{interface}][{request or generation_id}]"
                            message = str(event.get("message", "") or "")
                            print(
                                f"{prefix} {event.get('state', '')}"
                                + (f" - {message}" if message else ""),
                                flush=True,
                            )
                    offset = handle.tell()
            current = manager.read_state()
            if (
                str(current.get("generation_id", "")) != generation_id
                or str(current.get("status", "")).upper() == "STOPPED"
            ):
                return 0
            owner_pid = int(current.get("pid", 0) or 0)
            owner_status = str(current.get("status", "") or "").upper()
            if owner_pid > 0 and owner_status in {
                "RUNNING", "READY", "FAILED", "STOPPING"
            }:
                owner_seen = True
            if not owner_seen and time.monotonic() >= startup_deadline:
                manager.finalize_abandoned_runtime(
                    generation_id,
                    reason="runtime_startup_abandoned",
                )
                return 0
            if owner_seen:
                heartbeat_age = max(
                    0.0,
                    time.time() - float(current.get("heartbeat_at", 0.0) or 0.0),
                )
                owner_alive = owner_pid > 0 and _pid_alive(owner_pid)
                if not owner_alive:
                    if not owner_lost_at:
                        owner_lost_at = time.monotonic()
                    if time.monotonic() - owner_lost_at >= 0.5:
                        finalized = manager.finalize_abandoned_runtime(
                            generation_id,
                            reason="runtime_owner_lost",
                        )
                        if finalized:
                            return 0
                elif heartbeat_age > 10.0:
                    # A live but unresponsive owner may still be able to run
                    # its normal finally blocks. Request cancellation and keep
                    # monitoring; never label a live PID STOPPED.
                    manager.request_shutdown(
                        generation_id,
                        reason="runtime_heartbeat_lost",
                    )
                else:
                    owner_lost_at = 0.0
            monitor_state["heartbeat_at"] = time.time()
            _atomic_json(manager.monitor_state_path, monitor_state)
            time.sleep(max(0.05, float(poll_sec)))
    except KeyboardInterrupt:
        manager.request_shutdown(generation_id, reason="monitor_interrupted")
        return 0
    finally:
        current = manager.read_monitor_state()
        if (
            int(current.get("pid", 0) or 0) == os.getpid()
            and str(current.get("generation_id", "")) == generation_id
        ):
            monitor_state.update(status="STOPPED", heartbeat_at=time.time())
            _atomic_json(manager.monitor_state_path, monitor_state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tri-One runtime event monitor")
    parser.add_argument("--interface", choices=sorted(INTERFACES), required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        print("TRI_ONE_RUNTIME_MONITOR_PARSE_OK")
        return 0
    return run_monitor(args.root, args.interface, args.generation)


if __name__ == "__main__":
    raise SystemExit(main())
