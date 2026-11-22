"""Run a Telegram-approved command batch in one real Windows CMD console.

The caller MUST authorize the Telegram sender, workspace and each complete
command before calling this module. This module never grants execution scope.
It starts one short-lived CMD console, dispatches one command at a time in
the same CMD session and reports per-command observed exit status.

Only the authenticated Telegram MANUAL host-control channel uses this
adapter, regardless of ACL/Turbo settings. It executes with the listener's
Windows token and is intentionally NOT an Agent action; it does not use
the Restricted Executor/AppContainer. The paired user explicitly owns each
command and Windows enforces that token's actual permissions.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence


class TelegramConsoleError(RuntimeError):
    pass


def _interactive_desktop_session() -> bool:
    """Session 0 cannot display an interactive console to the desktop user."""
    if os.name != "nt":
        return False
    session_id = ctypes.c_ulong(0)
    kernel = ctypes.windll.kernel32
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        return False
    return session_id.value != 0


def _batch_path(path: Path) -> str:
    """Literal Windows batch path, never interpolate a model/user command here."""
    raw = str(path)
    if re.search(r'[\x00-\x1f"<>|&^!]', raw):
        raise TelegramConsoleError("TG_CMD_CONSOLE_PATH_UNSAFE")
    return '"' + raw.replace("%", "%%") + '"'


def _write_batch(path: Path, lines: Sequence[str]) -> None:
    # cmd.exe executes BAT source using the active Windows ANSI/OEM code page,
    # not Python's default UTF-8. Refuse unrepresentable text instead of
    # silently executing a corrupted command.
    data = ("\r\n".join(lines) + "\r\n").encode("mbcs", errors="strict")
    path.write_bytes(data)


def _console_environment(cwd: Path) -> dict[str, str]:
    """Use the listener's OS environment rather than Runtime's bundled Git PATH.

    A normal manually opened CMD resolves installed system Git, while the
    SmartAgent Runtime deliberately prepends .venv/git for internal tools.
    This path normalization affects only this authorized TG desktop console.
    Do not invent proxy values or modify machine DNS or Git configuration.
    """
    env = dict(os.environ)
    entries = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    app_root = Path(__file__).resolve().parents[2]
    bundled_git = {
        os.path.normcase(str(app_root / ".venv" / "git" / "cmd")).casefold(),
        os.path.normcase(str(app_root / ".venv" / "git" / "bin")).casefold(),
    }
    kept = [
        p for p in entries
        if os.path.normcase(os.path.normpath(p)).casefold() not in bundled_git
    ]
    # Only prefer system Git when a real executable is present. On a fresh
    # portable-only installation preserve the packaged Git path.
    def has_git(directory: str) -> bool:
        try:
            return (Path(directory) / "git.exe").is_file()
        except OSError:
            return False

    if any(has_git(p) for p in kept):
        entries = kept
    env["PATH"] = os.pathsep.join(entries)
    env["SMARTAGENT_NONINTERACTIVE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return env


def _terminate_tree(process: subprocess.Popen) -> bool:
    if process.poll() is not None:
        return True
    exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "taskkill.exe")
    try:
        subprocess.run(
            [exe, "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return process.poll() is not None


def _read_excerpt(path: Path, limit: int = 10000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    # Windows text output uses the active console code page. When supported,
    # decode using the current OEM code page for compatibility with cmd.exe.
    if os.name == "nt":
        codepage = ctypes.windll.kernel32.GetOEMCP()
        enc = f"cp{codepage}"
    else:
        enc = "utf-8"
    try:
        return data[:limit].decode(enc, errors="replace").strip()
    except LookupError:
        return data[:limit].decode("utf-8", errors="replace").strip()


def run_visible_cmd_batch(
    commands: Sequence[str], *, cwd: Path,
    command_timeout_seconds: int, batch_timeout_seconds: int,
    require_interactive: bool = True,
) -> dict:
    """Open one visible CMD and wait for real per-command completion markers.

    The shell always closes after finishing. No detached interactive shell is
    left running. Independent output files are internal orchestration state,
    not a modification to the authorized user workspace.
    """
    if os.name != "nt":
        raise TelegramConsoleError("TG_CMD_WINDOWS_REQUIRED")
    if require_interactive and not _interactive_desktop_session():
        raise TelegramConsoleError("TG_CMD_DESKTOP_SESSION_REQUIRED")
    if (not commands or len(commands) > 12
            or any(not isinstance(c, str) or not c.strip()
                   or "\n" in c or "\r" in c for c in commands)):
        raise TelegramConsoleError("TG_CMD_CONSOLE_COMMANDS_INVALID")

    cwd = cwd.resolve()
    with tempfile.TemporaryDirectory(prefix="smartagent_tg_console_") as temp:
        staging = Path(temp)
        script = staging / "batch.cmd"
        main = ["@echo off", "setlocal DisableDelayedExpansion",
                f"cd /d {_batch_path(cwd)}",
                "if errorlevel 1 exit /b 98"]
        for i, command in enumerate(commands, 1):
            token = f"{i:02d}"
            started = staging / (token + ".running")
            output = staging / (token + ".stdout")
            errors = staging / (token + ".stderr")
            rc_path = staging / (token + ".exit")
            # Every command is a CALLed script within the SAME cmd.exe
            # process. Changes to CD or SET are retained for later commands.
            if command.strip().casefold() in {"pause", "@pause"}:
                main.extend([
                    f'echo SKIPPED_PAUSE > {_batch_path(output)}',
                    f'echo 0 > {_batch_path(rc_path)}',
                ])
                continue
            step = staging / (token + ".cmd")
            _write_batch(step, ["@echo off", command])
            main.extend([
                f'echo {token} > {_batch_path(started)}',
                f'echo [{i}/{len(commands)}] Running approved command...',
                # Keep STDIN attached to the actual console. Windows
                # timeout.exe rejects <NUL (ERROR: input redirection is not
                # supported), even though the console itself is real.
                # Standalone PAUSE remains a skip-only step; BAT-embedded
                # PAUSE must use the cooperative SMARTAGENT_NONINTERACTIVE
                # guard or it remains a genuine interactive wait.
                f'call {_batch_path(step)} 1>{_batch_path(output)} 2>{_batch_path(errors)}',
                'set "SMARTAGENT_TG_LAST_RC=%errorlevel%"',
                f'echo %SMARTAGENT_TG_LAST_RC% > {_batch_path(rc_path)}',
                f'if exist {_batch_path(output)} type {_batch_path(output)}',
                f'if exist {_batch_path(errors)} type {_batch_path(errors)} 1>&2',
                'if not "%SMARTAGENT_TG_LAST_RC%"=="0" exit /b %SMARTAGENT_TG_LAST_RC%',
            ])
        main.extend(["exit /b 0"])
        _write_batch(script, main)

        exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "cmd.exe")
        process: subprocess.Popen | None = None
        failure = ""
        timed_out_idx = None
        started_at = time.monotonic()
        active_step = None
        active_since = started_at
        try:
            process = subprocess.Popen(
                [exe, "/d", "/q", "/c", str(script)],
                cwd=str(cwd),
                # New console owns the interactive input handle, so
                # TIMEOUT waits normally. Never redirect STDIN to NUL or a
                # PIPE for this desktop-console pathway.
                stdin=None,
                stdout=None,
                stderr=None,
                shell=False,
                env=_console_environment(cwd),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            # Do not enqueue the next step from Python. CMD itself moves on
            # only after CALL has returned and its exit code is recorded.
            while process.poll() is None:
                now = time.monotonic()
                if now - started_at >= batch_timeout_seconds:
                    failure = "TG_CMD_BATCH_TIMEOUT"
                    break
                for i, command in enumerate(commands, 1):
                    if (staging / f"{i:02d}.running").is_file() and not (
                            staging / f"{i:02d}.exit").is_file():
                        if active_step != i:
                            active_step, active_since = i, now
                        if now - active_since >= command_timeout_seconds:
                            timed_out_idx = i
                            failure = "TG_CMD_LINE_TIMEOUT"
                        break
                if failure:
                    break
                time.sleep(0.1)
            if failure and not _terminate_tree(process):
                failure = "TG_CMD_PROCESS_TREE_UNCONFIRMED"
            elif not failure:
                process.wait(timeout=5)
        except Exception as exc:
            if process is not None and not _terminate_tree(process):
                failure = "TG_CMD_PROCESS_TREE_UNCONFIRMED"
            else:
                failure = "TG_CMD_CONSOLE_START_FAILED:" + type(exc).__name__
        rows = []
        for i, command in enumerate(commands, 1):
            idx = f"{i:02d}"
            exit_file = staging / (idx + ".exit")
            if not exit_file.is_file():
                if failure and (staging / (idx + ".running")).is_file():
                    rows.append({
                        "line": i, "exit_code": None,
                        "stdout": _read_excerpt(staging / (idx + ".stdout")),
                        "stderr": _read_excerpt(staging / (idx + ".stderr")),
                        "status": "UNKNOWN", "timed_out": timed_out_idx == i,
                        "reason": failure,
                    })
                break
            try:
                exit_code = int(exit_file.read_text(encoding="ascii").strip())
            except (ValueError, OSError, UnicodeError):
                exit_code = None
            skipped = command.strip().casefold() in {"pause", "@pause"}
            row = {
                "line": i, "exit_code": exit_code,
                "stdout": _read_excerpt(staging / (idx + ".stdout")),
                "stderr": _read_excerpt(staging / (idx + ".stderr")),
                "status": "SUCCESS" if exit_code == 0 else "FAILED",
                "timed_out": False,
            }
            if skipped:
                row["skipped_pause"] = True
            rows.append(row)
            if exit_code != 0:
                break
        return {
            "results": rows, "reason": failure,
            "console_started": process is not None,
            "console_exit_code": process.returncode if process else None,
            "completed": len(rows) == len(commands)
                         and all(row["status"] == "SUCCESS" for row in rows)
                         and not failure,
        }
