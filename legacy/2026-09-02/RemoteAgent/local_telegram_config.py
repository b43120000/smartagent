#!/usr/bin/env python3
from __future__ import annotations

"""Current-user encrypted Telegram defaults for one-click local startup."""

import argparse
import base64
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, MutableMapping

from agent_core.workspace import AGENT_PROJECT_ROOT


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for saved Telegram credentials")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"SmartAgent.RemoteAgent.Telegram.v1")
    output_blob = _DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), "SmartAgent Telegram Bot Token",
            ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, ctypes.byref(entropy_blob),
            None, None, flags, ctypes.byref(output_blob),
        )
    # Keep buffers alive through the native call.
    _ = input_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _protect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=False)


class TelegramLocalConfigStore:
    def __init__(
        self,
        path: str | Path,
        *,
        protector: Callable[[bytes], bytes] = _protect,
        unprotector: Callable[[bytes], bytes] = _unprotect,
    ):
        self.path = Path(path)
        self._protector = protector
        self._unprotector = unprotector

    def save(self, *, bot_token: str, workspace: str | Path, enabled: bool = True) -> dict:
        token = str(bot_token or "").strip()
        root = Path(workspace).resolve()
        if not token:
            raise ValueError("telegram_bot_token_missing")
        if not root.is_dir():
            raise ValueError(f"telegram_workspace_unavailable:{root}")
        encrypted = self._protector(token.encode("utf-8"))
        payload = {
            "version": 1,
            "provider": "windows_dpapi_current_user",
            "enabled": bool(enabled),
            "workspace": str(root),
            "bot_token_ciphertext": base64.b64encode(encrypted).decode("ascii"),
            "updated_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return {key: value for key, value in payload.items() if key != "bot_token_ciphertext"}

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0) or 0) != 1:
            raise ValueError("unsupported_telegram_local_config")
        ciphertext = base64.b64decode(str(payload.get("bot_token_ciphertext", "")), validate=True)
        token = self._unprotector(ciphertext).decode("utf-8")
        workspace = str(payload.get("workspace", "") or "").strip()
        if not token or not workspace or not Path(workspace).is_dir():
            raise ValueError("invalid_telegram_local_config")
        return {
            "enabled": bool(payload.get("enabled", True)),
            "workspace": str(Path(workspace).resolve()),
            "bot_token": token,
            "provider": str(payload.get("provider", "")),
        }


def default_store(root: str | Path = AGENT_PROJECT_ROOT) -> TelegramLocalConfigStore:
    return TelegramLocalConfigStore(Path(root) / ".agents" / "telegram_local_config.json")


def apply_saved_telegram_environment(
    *,
    root: str | Path = AGENT_PROJECT_ROOT,
    environ: MutableMapping[str, str] | None = None,
) -> dict:
    env = os.environ if environ is None else environ
    try:
        config = default_store(root).load()
    except Exception as exc:
        return {"loaded": False, "enabled": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not config or not config.get("enabled"):
        return {"loaded": False, "enabled": False, "reason": "not_configured"}
    env.setdefault("SMARTAGENT_TELEGRAM_BOT_TOKEN", config["bot_token"])
    env.setdefault("SMARTAGENT_TELEGRAM_WORKSPACE", config["workspace"])
    env.setdefault("SMARTAGENT_TELEGRAM_ENABLED", "1")
    env.setdefault("SMARTAGENT_TELEGRAM_PAIRING_ENABLED", "1")
    return {"loaded": True, "enabled": True, "workspace": env["SMARTAGENT_TELEGRAM_WORKSPACE"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted local Telegram startup settings")
    sub = parser.add_subparsers(dest="command", required=True)
    save = sub.add_parser("save")
    save.add_argument("--workspace", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    store = default_store()
    if args.command == "save":
        result = store.save(
            bot_token=os.environ.get("SMARTAGENT_TELEGRAM_BOT_TOKEN", ""),
            workspace=args.workspace,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        config = store.load()
        result = {
            "configured": bool(config),
            "enabled": bool(config.get("enabled")) if config else False,
            "workspace": str(config.get("workspace", "")) if config else "",
            "provider": str(config.get("provider", "")) if config else "",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"configured": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
