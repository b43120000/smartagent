#!/usr/bin/env python3
"""Probe and expose SmartAgent's relocatable Python and Git runtimes.

Availability is derived only from executing the application-relative binary.
Payload manifests are packaging evidence and are deliberately not consulted here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from .metadata import DEFAULT_APP_ROOT
from .paths import environment_ledger_path

ENVIRONMENT_LEDGER_SCHEMA = "SMARTAGENT_ENVIRONMENT_LEDGER_V1"
_TOOL_PATHS = {
    "python": Path(".venv") / "Scripts" / "python.exe",
    "git": Path(".venv") / "git" / "cmd" / "git.exe",
}
_VERSION_ARGS = {"python": ("--version",), "git": ("--version",)}
ProbeRunner = Callable[..., subprocess.CompletedProcess[str]]


def _app_root(root: str | Path | None = None) -> Path:
    return Path(root or DEFAULT_APP_ROOT).expanduser().resolve()


def runtime_tool_paths(root: str | Path | None = None) -> dict[str, Path]:
    base = _app_root(root)
    return {name: (base / relative).resolve() for name, relative in _TOOL_PATHS.items()}


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def build_runtime_environment(
    root: str | Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child-only environment with validated bundled locations first."""
    env = dict(os.environ if base_env is None else base_env)
    paths = runtime_tool_paths(root)
    preferred: list[str] = []
    python_dir = paths["python"].parent
    git_cmd = paths["git"].parent
    git_bin = git_cmd.parent / "bin"
    for directory, binary in (
        (python_dir, paths["python"]),
        (git_cmd, paths["git"]),
        (git_bin, paths["git"]),
    ):
        if binary.is_file() and directory.is_dir():
            preferred.append(str(directory))
    inherited = [part for part in str(env.get("PATH", "")).split(os.pathsep) if part]
    ordered: list[str] = []
    seen: set[str] = set()
    for part in preferred + inherited:
        key = _path_key(part)
        if key not in seen:
            seen.add(key)
            ordered.append(part)
    env["PATH"] = os.pathsep.join(ordered)
    return env


def _probe_tool(name: str, path: Path, root: Path, runner: ProbeRunner) -> dict:
    fact = {
        "name": name,
        "path": str(path),
        "source": "BUNDLED",
        "status": "MISSING",
        "version": "",
        "binary_sha256": "",
        "probe_exit_code": None,
    }
    if not path.is_file():
        return fact
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        fact["binary_sha256"] = digest.hexdigest()
        result = runner(
            [str(path), *_VERSION_ARGS[name]],
            cwd=str(root),
            env=build_runtime_environment(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        fact["probe_exit_code"] = result.returncode
        version = " ".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )[:300]
        fact["version"] = version
        fact["status"] = "AVAILABLE" if result.returncode == 0 and bool(version) else "INVALID"
    except (OSError, subprocess.SubprocessError) as exc:
        fact["status"] = "INVALID"
        fact["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return fact


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def refresh_environment_ledger(
    root: str | Path | None = None,
    *,
    runner: ProbeRunner = subprocess.run,
) -> dict:
    base = _app_root(root)
    paths = runtime_tool_paths(base)
    tools = {name: _probe_tool(name, path, base, runner) for name, path in paths.items()}
    payload = {
        "schema": ENVIRONMENT_LEDGER_SCHEMA,
        "application_root": str(base),
        "checked_at": time.time(),
        "tools": tools,
    }
    _write_atomic(environment_ledger_path(base), payload)
    return payload


def read_environment_ledger(root: str | Path | None = None) -> dict:
    try:
        payload = json.loads(environment_ledger_path(_app_root(root)).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if payload.get("schema") == ENVIRONMENT_LEDGER_SCHEMA else {}


def planner_environment_snapshot(root: str | Path | None = None) -> dict:
    """Return only probe-owned fields that are safe to place in a planner prompt."""
    payload = refresh_environment_ledger(root)
    compact = {}
    for name in sorted(_TOOL_PATHS):
        fact = payload["tools"].get(name, {})
        compact[name] = {
            "status": str(fact.get("status", "MISSING")),
            "source": str(fact.get("source", "BUNDLED")),
            "path": str(fact.get("path", "")),
            "version": str(fact.get("version", ""))[:300],
            "binary_sha256": str(fact.get("binary_sha256", ""))[:64],
            "probe_exit_code": fact.get("probe_exit_code"),
        }
    return {
        "schema": ENVIRONMENT_LEDGER_SCHEMA,
        "checked_at": payload["checked_at"],
        "tools": compact,
    }


__all__ = [
    "ENVIRONMENT_LEDGER_SCHEMA", "build_runtime_environment",
    "planner_environment_snapshot", "read_environment_ledger",
    "refresh_environment_ledger", "runtime_tool_paths",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe bundled SmartAgent runtimes")
    parser.add_argument("--root", default=str(DEFAULT_APP_ROOT))
    args = parser.parse_args(argv)
    payload = refresh_environment_ledger(args.root)
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    tools = payload.get("tools") or {}
    return 0 if tools and all(item.get("status") == "AVAILABLE" for item in tools.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
