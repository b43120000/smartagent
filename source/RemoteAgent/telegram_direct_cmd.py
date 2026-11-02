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
