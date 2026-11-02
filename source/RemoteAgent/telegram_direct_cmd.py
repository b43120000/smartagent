"""Opt-in, restricted Telegram software-only CMD controls."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

CONTROL_LABEL = "執行cmd"
HEADER = "run cmd by TG"
MAX_LINES = 12


class CmdControlError(ValueError):
    pass


def _parse_ids(raw: str) -> frozenset[int]:
    try:
        ids = frozenset(int(x.strip()) for x in raw.replace(";", ",").split(",") if x.strip())
    except ValueError:
        return frozenset()
    return ids if all(x > 0 for x in ids) else frozenset()


@dataclass(frozen=True)
class CmdControlPolicy:
    enabled: bool
    user_ids: frozenset[int]
    chat_ids: frozenset[int]

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None):
        env = os.environ if values is None else values
        return cls(
            str(env.get("SMARTAGENT_TG_CMD_ENABLED", "0")).lower() in {"1", "true", "on"},
            _parse_ids(env.get("SMARTAGENT_TG_CMD_ALLOWED_USER_IDS", "")),
            _parse_ids(env.get("SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS", "")),
        )

    def assert_authorized(self, user_id: int, chat_id: int) -> None:
        if not self.enabled:
            raise CmdControlError("TG_CMD_DISABLED")
        if not self.user_ids or not self.chat_ids or user_id not in self.user_ids or chat_id not in self.chat_ids:
            raise CmdControlError("TG_CMD_ADMIN_NOT_AUTHORIZED")


def is_direct_cmd_request(text: str) -> bool:
    return bool(re.match(r"^run cmd by tg\s*(?:\r?\n|$)", str(text or "").lstrip(), re.I))


def render_cmd_template(smartagent_root: str | Path) -> str:
    return (
        "執行cmd（固定軟體流程，不經 WebGPT）\n"
        "請複製以下格式、修改後回傳：\n\n"
        "run cmd by TG\n"
        f'cmd path: "{Path(smartagent_root).resolve()}"\n'
        "command:\n"
        '"echo hello"\n'
        '"echo time"\n\n'
        "每行分別執行並回傳 Exit Code、stdout、stderr。"
    )


def parse_cmd_request(text: str) -> tuple[Path, tuple[str, ...]]:
    value = str(text or "").strip()
    if len(value) > 8192:
        raise CmdControlError("TG_CMD_REQUEST_TOO_LARGE")
    rows = value.splitlines()
    if len(rows) < 4 or rows[0].strip().casefold() != HEADER.casefold():
        raise CmdControlError("TG_CMD_TEMPLATE_INVALID")
    path_match = re.fullmatch(r'cmd path\s*:\s*"([^"\r\n]+)"\s*', rows[1].strip(), re.I)
    if not path_match or not re.fullmatch(r"command\s*:", rows[2].strip(), re.I):
        raise CmdControlError("TG_CMD_TEMPLATE_FIELDS_INVALID")
    folder = Path(path_match.group(1)).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise CmdControlError("TG_CMD_WORKSPACE_INVALID")
    commands = []
    for raw in rows[3:]:
        cmd = raw.strip()
        if not cmd:
            continue
        if len(cmd) >= 2 and cmd[0] == cmd[-1] == '"':
            cmd = cmd[1:-1]
        if not cmd or len(cmd) > 600 or "\x00" in cmd:
            raise CmdControlError("TG_CMD_LINE_INVALID")
        commands.append(cmd)
    if not 1 <= len(commands) <= MAX_LINES:
        raise CmdControlError("TG_CMD_COMMAND_COUNT_INVALID")
    return folder.resolve(), tuple(commands)


def execute_cmd_request(
    text: str, *, user_id: int, chat_id: int,
    policy: CmdControlPolicy | None = None, runner=None,
) -> dict:
    """Sequential bounded cmd.exe invocation through the existing OS executor."""
    import time
    import uuid
    from agent_core.command_security import inspect_command
    from agent_core.path_security import is_within, reject_reparse_components
    from agent_core.workspace_access import access_snapshot
    from agent_core.windows_security import restricted_executor_required
    from agent_core.bounded_process import run_bounded_process

    (policy or CmdControlPolicy.from_env()).assert_authorized(user_id, chat_id)
    if os.name != "nt":
        raise CmdControlError("TG_CMD_WINDOWS_REQUIRED")
    if not restricted_executor_required():
        raise CmdControlError("TG_CMD_RESTRICTED_EXECUTOR_REQUIRED")
    cwd, commands = parse_cmd_request(text)
    roots = tuple(Path(p).resolve() for p in access_snapshot().get("writable_workspaces", ()))
    root = next((p for p in roots if is_within(cwd, p)), None)
    if root is None:
        raise CmdControlError("TG_CMD_WORKSPACE_NOT_AUTHORIZED")
    reject_reparse_components(cwd, root)

    # Verify the entire batch before the first process starts.
    for command in commands:
        admission = inspect_command(command, workspace=cwd)
        if not admission.allowed:
            raise CmdControlError("TG_CMD_COMMAND_REJECTED:" + admission.code)

    executor = runner or run_bounded_process
    exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
    started = time.monotonic()
    tracking = "TG-CMD-" + uuid.uuid4().hex[:12].upper()
    lines = [f"CMD 執行結果 {tracking}", f"cmd path: {cwd}"]
    results = []
    for index, command in enumerate(commands, 1):
        if time.monotonic() - started > 120:
            lines.append("總執行期限已到，後續命令不執行。")
            break
        info = executor(
            [exe, "/d", "/s", "/c", command],
            cwd=cwd, shell=False, timeout=20, max_stream_bytes=10000,
            security_command=command,
        )
        passed = (
            info.get("process_started") is True and info.get("exit_code") == 0
            and not info.get("timed_out") and not info.get("security_rejected")
        )
        results.append({"line": index, "exit_code": info.get("exit_code"),
                        "status": "SUCCESS" if passed else "FAILED"})
        lines.extend([
            "", f"[{index}/{len(commands)}] > {command}",
            "Exit Code: " + str(info.get("exit_code")),
            "stdout:\n" + str(info.get("stdout") or "(empty)")[:10000],
        ])
        if info.get("stderr"):
            lines.append("stderr:\n" + str(info["stderr"])[:10000])
        if info.get("error"):
            lines.append("error: " + str(info["error"])[:1000])
        if not passed:
            lines.append("指令未完成，停止執行後續行。")
            break
    success = len(results) == len(commands) and all(
        x["status"] == "SUCCESS" for x in results
    )
    lines.append("\n狀態：" + ("SUCCESS" if success else "FAILED/PARTIAL"))
    reply = "\n".join(lines)
    if len(reply) > 24000:
        reply = reply[:24000] + "\n...[回覆過長，後段省略]"
    return {"message": reply, "status": "SUCCESS" if success else "FAILED",
            "control_id": tracking, "results": results}
