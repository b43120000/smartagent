#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from .paths import (
    force_stop_flag_path,
    remote_binding_path,
    remote_restart_log_path,
    startup_preferences_path,
    webgpt_submit_lock_path,
    workspace_manager_launcher_log_path,
)

Resolver = Callable[[str | Path | None], Path]

RESOLVERS: dict[str, Resolver] = {
    "force_stop_flag": force_stop_flag_path,
    "remote_binding": remote_binding_path,
    "remote_restart_log": remote_restart_log_path,
    "startup_preferences": startup_preferences_path,
    "webgpt_submit_lock": webgpt_submit_lock_path,
    "workspace_manager_launcher_log": workspace_manager_launcher_log_path,
}


def resolve_named_path(name: str, root: str | Path | None = None) -> Path:
    try:
        resolver = RESOLVERS[str(name)]
    except KeyError as exc:
        raise KeyError(f"unknown_smartagent_path_name:{name}") from exc
    return resolver(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve one SmartAgent semantic path")
    parser.add_argument("name", choices=sorted(RESOLVERS))
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)
    print(resolve_named_path(args.name, args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RESOLVERS", "resolve_named_path"]
