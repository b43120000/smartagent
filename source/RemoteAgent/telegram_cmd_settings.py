"""Load persistent Telegram CMD administrator IDs without executing local code.

A file inside the ignored, installation-specific localdata tree is the only
persisted configuration source. Reading it does not grant workspace access,
disable command security, or bypass the independent Telegram sender check.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import MutableMapping

SCHEMA = "SMARTAGENT_TG_CMD_SETTINGS_V1"
FILENAME = "telegram_cmd_settings.json"
_ALLOWED_KEYS = frozenset({"schema", "enabled", "allowed_user_ids", "allowed_chat_ids"})
_MAX_BYTES = 4096
_MAX_IDS = 16

_ENV_ENABLED = "SMARTAGENT_TG_CMD_ENABLED"
_ENV_USER_IDS = "SMARTAGENT_TG_CMD_ALLOWED_USER_IDS"
_ENV_CHAT_IDS = "SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS"


class TelegramCmdSettingsError(ValueError):
    """Fail-closed local settings validation."""


def settings_path(root: str | Path) -> Path:
    """Fixed local-only path; never follow a path supplied from Telegram."""
    return Path(root).expanduser().resolve() / "localdata" / "secure" / FILENAME


def _validate_ids(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > _MAX_IDS:
        raise TelegramCmdSettingsError(name + "_invalid")
    ids = []
    for item in value:
        if type(item) is not int or item <= 0:
            raise TelegramCmdSettingsError(name + "_invalid")
        ids.append(item)
    if len(set(ids)) != len(ids):
        raise TelegramCmdSettingsError(name + "_duplicate")
    return tuple(ids)


def read_saved_telegram_cmd_settings(root: str | Path) -> dict | None:
    """Return strict parsed configuration, or None if no local file exists.

    A malformed, unreadable or replaced file raises. The caller must then
    explicitly disable CMD rather than falling back to inherited permission.
    """
    path = settings_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise TelegramCmdSettingsError("telegram_cmd_settings_file_invalid")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TelegramCmdSettingsError("telegram_cmd_settings_unreadable") from exc
    if not isinstance(data, dict) or set(data) != _ALLOWED_KEYS:
        raise TelegramCmdSettingsError("telegram_cmd_settings_fields_invalid")
    if data.get("schema") != SCHEMA or type(data.get("enabled")) is not bool:
        raise TelegramCmdSettingsError("telegram_cmd_settings_schema_invalid")
    users = _validate_ids(data["allowed_user_ids"], "telegram_cmd_user_ids")
    chats = _validate_ids(data["allowed_chat_ids"], "telegram_cmd_chat_ids")
    enabled = data["enabled"]
    if enabled and (not users or not chats):
        raise TelegramCmdSettingsError("telegram_cmd_settings_allowlist_required")
    return {"enabled": enabled, "user_ids": users, "chat_ids": chats}


def apply_saved_telegram_cmd_environment(
    *, root: str | Path, environ: MutableMapping[str, str] | None = None,
) -> dict:
    """Load settings on HostSupervisor startup, prior to accepting messages.

    If a persisted file exists, it overrides inherited CMD-only env variables.
    Absence preserves the original explicit environment-variable workflow.
    Invalid persisted settings always disable, never enable, remote execution.
    No IDs or source file contents are returned for startup logs.
    """
    env = os.environ if environ is None else environ
    try:
        value = read_saved_telegram_cmd_settings(root)
    except (TelegramCmdSettingsError, OSError, ValueError) as exc:
        env[_ENV_ENABLED] = "0"
        env.pop(_ENV_USER_IDS, None)
        env.pop(_ENV_CHAT_IDS, None)
        return {"loaded": False, "enabled": False,
                "reason": "invalid_local_config:" + type(exc).__name__}
    if value is None:
        return {"loaded": False, "enabled": False, "reason": "not_configured"}

    env[_ENV_ENABLED] = "1" if value["enabled"] else "0"
    env[_ENV_USER_IDS] = ",".join(map(str, value["user_ids"]))
    env[_ENV_CHAT_IDS] = ",".join(map(str, value["chat_ids"]))
    return {"loaded": True, "enabled": value["enabled"],
            "reason": "configured" if value["enabled"] else "disabled"}


__all__ = [
    "SCHEMA", "FILENAME", "TelegramCmdSettingsError", "settings_path",
    "read_saved_telegram_cmd_settings", "apply_saved_telegram_cmd_environment",
]
