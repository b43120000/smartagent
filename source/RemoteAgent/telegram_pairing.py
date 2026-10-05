#!/usr/bin/env python3
from __future__ import annotations

"""One-time, local QR pairing for the Telegram RemoteAgent transport.

Only a SHA-256 digest of a pairing secret is persisted.  The secret itself is
placed in the QR deep link and is never written to state or runtime logging.
"""

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _process_lock(path: Path, *, timeout_sec: float = 5.0):
    """Exclusive lock valid across the Agent0 process and pairing CLI."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # `msvcrt.locking()` cannot lock a range beyond EOF.  Create the single
    # lock byte only when the file does not exist; never read or write it while
    # another process might hold its lock.
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    else:
        try:
            os.write(descriptor, b"0")
        finally:
            os.close(descriptor)
    with lock_path.open("r+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + max(0.1, float(timeout_sec))
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("telegram_pairing_lock_timeout")
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TelegramPairingStore:
    """Durable pairing state with one-time token consumption.

    State contains pending token digests and authorized user/chat pairs only;
    neither a bot token nor a pairing secret is ever persisted.
    """

    VERSION = 1

    def __init__(self, path: str | Path, *, clock=time.time, lock_timeout_sec: float = 5.0):
        self.path = Path(path)
        self.clock = clock
        self.lock_timeout_sec = max(0.1, float(lock_timeout_sec))
        self._thread_lock = threading.RLock()

    @staticmethod
    def _hash(secret: str) -> str:
        return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": self.VERSION, "pending": [], "paired": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            # A corrupt pairing file must not implicitly authorize a sender.
            return {"version": self.VERSION, "pending": [], "paired": []}
        if not isinstance(value, dict):
            return {"version": self.VERSION, "pending": [], "paired": []}
        value["version"] = self.VERSION
        value["pending"] = list(value.get("pending") or [])
        value["paired"] = list(value.get("paired") or [])
        return value

    @staticmethod
    def _prune(payload: dict[str, Any], now: float) -> bool:
        before = len(payload["pending"])
        payload["pending"] = [
            item for item in payload["pending"]
            if isinstance(item, dict) and float(item.get("expires_at", 0) or 0) > now
        ]
        return len(payload["pending"]) != before

    def issue(self, *, ttl_sec: int = 300) -> str:
        ttl = max(60, min(900, int(ttl_sec)))
        secret = secrets.token_urlsafe(32)
        now = float(self.clock())
        with self._thread_lock, _process_lock(self.path, timeout_sec=self.lock_timeout_sec):
            payload = self._load_unlocked()
            self._prune(payload, now)
            payload["pending"].append({
                "secret_hash": self._hash(secret),
                "expires_at": now + ttl,
                "created_at": now,
            })
            _atomic_json_write(self.path, payload)
        return secret

    def consume(self, secret: str, *, user_id: int, chat_id: int) -> bool:
        """Atomically consume a non-expired secret and authorize this pair."""
        if not secret or int(user_id) <= 0 or int(chat_id) <= 0:
            return False
        digest = self._hash(secret)
        now = float(self.clock())
        with self._thread_lock, _process_lock(self.path, timeout_sec=self.lock_timeout_sec):
            payload = self._load_unlocked()
            self._prune(payload, now)
            match = next(
                (row for row in payload["pending"] if secrets.compare_digest(str(row.get("secret_hash", "")), digest)),
                None,
            )
            if match is None:
                _atomic_json_write(self.path, payload)
                return False
            payload["pending"].remove(match)
            pair = {"user_id": int(user_id), "chat_id": int(chat_id), "paired_at": now}
            payload["paired"] = [
                row for row in payload["paired"]
                if not (int(row.get("user_id", 0) or 0) == pair["user_id"] and int(row.get("chat_id", 0) or 0) == pair["chat_id"])
            ]
            payload["paired"].append(pair)
            _atomic_json_write(self.path, payload)
            return True

    def is_paired(self, *, user_id: int, chat_id: int) -> bool:
        with self._thread_lock, _process_lock(self.path, timeout_sec=self.lock_timeout_sec):
            payload = self._load_unlocked()
            changed = self._prune(payload, float(self.clock()))
            if changed:
                _atomic_json_write(self.path, payload)
            return any(
                int(row.get("user_id", 0) or 0) == int(user_id)
                and int(row.get("chat_id", 0) or 0) == int(chat_id)
                for row in payload["paired"] if isinstance(row, dict)
            )

    def paired_chat_ids(self) -> tuple[int, ...]:
        with self._thread_lock, _process_lock(self.path, timeout_sec=self.lock_timeout_sec):
            payload = self._load_unlocked()
            changed = self._prune(payload, float(self.clock()))
            if changed:
                _atomic_json_write(self.path, payload)
            return tuple(sorted({int(row.get("chat_id", 0) or 0) for row in payload["paired"] if isinstance(row, dict) and int(row.get("chat_id", 0) or 0) > 0}))


def create_deep_link(*, bot_username: str, secret: str) -> str:
    username = str(bot_username or "").strip().lstrip("@").strip()
    if not username or any(char.isspace() for char in username):
        raise ValueError("telegram_bot_username_missing")
    return f"https://t.me/{username}?start=pair_{secret}"


def _pairing_ttl_from_env() -> int:
    try:
        return max(60, min(900, int(os.environ.get("SMARTAGENT_TELEGRAM_PAIR_TTL_SEC", "300"))))
    except ValueError:
        return 300


def main() -> int:
    # Import lazily so this module remains testable without the network client.
    from RemoteAgent.telegram_transport import TelegramBotClient, TelegramReceiverConfig
    from agent_core.workspace import AGENT_PROJECT_ROOT
    from agent_core.paths import telegram_pairing_path

    config = TelegramReceiverConfig.from_env()
    if not config.enabled or not config.pairing_enabled:
        raise SystemExit("Telegram QR pairing requires SMARTAGENT_TELEGRAM_ENABLED=1 and SMARTAGENT_TELEGRAM_PAIRING_ENABLED=1.")
    config.validate()
    profile = TelegramBotClient(config).get_me()
    username = str(profile.get("username", "") or "")
    store = TelegramPairingStore(telegram_pairing_path())
    secret = store.issue(ttl_sec=config.pairing_ttl_sec)
    deep_link = create_deep_link(bot_username=username, secret=secret)

    try:
        import qrcode
        print("[Telegram pairing] Scan this QR code with Telegram before it expires.", flush=True)
        code = qrcode.QRCode(border=1)
        code.add_data(deep_link)
        code.make(fit=True)
        code.print_ascii(invert=True)
    except Exception as exc:
        # QR rendering is a convenience, not part of the authorization
        # boundary. Keep pairing usable and expose the real import/render
        # failure instead of incorrectly claiming the package is absent.
        print(
            f"[Telegram pairing] QR 顯示失敗（{type(exc).__name__}: {exc}）。",
            flush=True,
        )
        print("請在手機 Telegram 開啟以下一次性配對連結：", flush=True)
        print(deep_link, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
