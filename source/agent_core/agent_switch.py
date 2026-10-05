"""Exclusive launcher guard: one agent interface owns the runtime at a time."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .paths import runtime_root

STATE_FILES = {
    "local": ("agent_host_state.json",),
    "remote": ("remote_supervisor_state.json", "remote_runtime_state.json", "telegram_listener_state.json"),
    "webdirect": ("webdirect_runtime_state.json",),
}


def stop_other_agents(root: str | Path, keep: str) -> list[int]:
    base = runtime_root(root)
    stopped: list[int] = []
    seen: set[int] = set()
    for interface, names in STATE_FILES.items():
        if interface == keep:
            continue
        for name in names:
            path = base / name
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            pid = int(value.get("pid", value.get("runtime_pid", 0)) or 0)
            if pid <= 0 or pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except ProcessLookupError:
                continue
            except OSError:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
                    stopped.append(pid)
                except OSError:
                    pass
    time.sleep(0.2)
    return stopped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--keep", choices=sorted(STATE_FILES), required=True)
    args = ap.parse_args()
    print(json.dumps({"stopped_pids": stop_other_agents(args.root, args.keep)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
