"""Durable, conservative Telegram long-text coalescer.

Software ingress only. No Planner, Runtime, Host CMD or ACL changes.
A staged Telegram update is acknowledged only after atomic local persistence.
Replay after a crash is idempotent by the original update_id. No text leaves
this module except through the receiver's existing authenticated ingress.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

STATE_SCHEMA = "SMARTAGENT_TELEGRAM_LONG_MESSAGE_PENDING_V1"
MERGE_SCHEMA = "SMARTAGENT_TELEGRAM_LONG_MESSAGE_MERGE_V1"


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _identity(update: dict[str, Any]) -> tuple[str, int, int, int, int, str]:
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    text = message.get("text")
    if (type(text) is not str or type(update.get("update_id")) is not int
            or type(message.get("message_id")) is not int
            or type(message.get("date")) is not int
            or type(sender.get("id")) is not int or type(chat.get("id")) is not int
            or not text or message["message_id"] <= 0 or message["date"] <= 0
            or sender["id"] <= 0 or chat["id"] <= 0
            or sender.get("is_bot") is not False
            or chat.get("type") != "private"):
        raise ValueError("TELEGRAM_LONG_MESSAGE_IDENTITY_INVALID")
    return (
        f'{chat["id"]}:{sender["id"]}',
        chat["id"], sender["id"], update["update_id"],
        message["message_id"], text,
    )


class TelegramLongMessageCoalescer:
    def __init__(self, path: str | Path, *, window_sec: float = 2.5,
                 threshold_chars: int = 5000, min_chunk_chars: int = 900,
                 max_parts: int = 16, max_chars: int = 120000):
        self.path = Path(path)
        self.window_sec = max(0.25, min(float(window_sec), 10.0))
        self.threshold_chars = max(4096, min(int(threshold_chars), 25000))
        self.min_chunk_chars = max(1, min(int(min_chunk_chars), 4096))
        self.max_parts = max(2, min(int(max_parts), 32))
        self.max_chars = max(self.threshold_chars + 1, min(int(max_chars), 256000))
        self.groups: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        body = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(body, dict) or body.get("schema") != STATE_SCHEMA
                or not isinstance(body.get("groups"), dict)):
            raise ValueError("TELEGRAM_LONG_MESSAGE_PENDING_STATE_INVALID")
        restored = {}
        for key, entry in body["groups"].items():
            if (not isinstance(key, str) or not isinstance(entry, dict)
                    or not isinstance(entry.get("updates"), list)
                    or not 1 <= len(entry["updates"]) <= self.max_parts
                    or type(entry.get("last_arrival")) not in (int, float)):
                raise ValueError("TELEGRAM_LONG_MESSAGE_PENDING_GROUP_INVALID")
            items = entry["updates"]
            validated = [_identity(item) for item in items]
            if any(row[0] != key for row in validated):
                raise ValueError("TELEGRAM_LONG_MESSAGE_PENDING_IDENTITY_MISMATCH")
            if any(next_row[3] <= old_row[3] or next_row[4] != old_row[4] + 1
                   for old_row, next_row in zip(validated, validated[1:])):
                raise ValueError("TELEGRAM_LONG_MESSAGE_PENDING_ORDER_INVALID")
            restored[key] = entry
        self.groups = restored

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        payload = {"schema": STATE_SCHEMA, "groups": self.groups}
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    @property
    def has_pending(self) -> bool:
        return bool(self.groups)

    def key_for(self, update: dict) -> str:
        return _identity(update)[0]

    def has_key(self, key: str) -> bool:
        return key in self.groups

    def due(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else float(now)
        return [key for key, group in self.groups.items()
                if current - float(group["last_arrival"]) >= self.window_sec]

    def can_append(self, update: dict, *, now: float | None = None) -> bool:
        row = _identity(update)
        group = self.groups.get(row[0])
        if not group:
            return False
        items = group["updates"]
        prior = _identity(items[-1])
        current = time.time() if now is None else float(now)
        if row[3] == prior[3]:
            return True  # Same persisted update is a retry, not a new part.
        return (
            row[1:3] == prior[1:3]
            and row[3] > prior[3]
            and row[4] == prior[4] + 1
            and 0 <= (int(update["message"]["date"]) -
                      int(items[-1]["message"]["date"])) <= self.window_sec
            and 0 <= current - float(group["last_arrival"]) <= self.window_sec
            and len(items) < self.max_parts
            and sum(len(_identity(x)[5]) for x in items) + len(row[5])
                <= self.max_chars
        )

    def stage(self, update: dict, *, now: float | None = None) -> str:
        row = _identity(update)
        key = row[0]
        current = time.time() if now is None else float(now)
        group = self.groups.get(key)
        if group:
            if any(_identity(x)[3] == row[3] for x in group["updates"]):
                return "DUPLICATE_STAGED"
            if not self.can_append(update, now=current):
                raise ValueError("TELEGRAM_LONG_MESSAGE_NEW_GROUP_REQUIRES_FLUSH")
            group = {**group, "updates": [*group["updates"], update],
                     "last_arrival": current}
        else:
            group = {"updates": [update], "last_arrival": current}
        previous = self.groups.get(key)
        self.groups[key] = group
        try:
            self._write()
        except Exception:
            if previous is None:
                self.groups.pop(key, None)
            else:
                self.groups[key] = previous
            raise
        return "STAGED"

    def compose(self, key: str) -> list[tuple[dict, dict | None]]:
        """Return original updates or ONE local merged update + provenance."""
        group = self.groups[key]
        updates = group["updates"]
        texts = [_identity(x)[5] for x in updates]
        joined = "".join(texts)
        if len(updates) < 2 or len(joined) <= self.threshold_chars:
            return [(update, None) for update in updates]
        first, last = updates[0], updates[-1]
        synthetic = json.loads(json.dumps(first, ensure_ascii=False))
        synthetic["message"]["text"] = joined
        synthetic["message"]["message_id"] = last["message"]["message_id"]
        synthetic["message"]["date"] = last["message"]["date"]
        synthetic["message"].pop("entities", None)
        provenance = {
            "schema": MERGE_SCHEMA,
            "first_update_id": first["update_id"],
            "last_update_id": last["update_id"],
            "source_update_ids": [x["update_id"] for x in updates],
            "source_message_ids": [x["message"]["message_id"] for x in updates],
            "source_text_sha256": [_digest(t) for t in texts],
            "source_text_lengths": [len(t) for t in texts],
            "combined_sha256": _digest(joined),
            "combined_characters": len(joined),
            "separator": "NONE_EXACT_CONCATENATION",
            "part_count": len(updates),
            "software_merged": True,
        }
        return [(synthetic, provenance)]

    def clear(self, key: str) -> None:
        if key not in self.groups:
            return
        prior = self.groups.pop(key)
        try:
            self._write()
        except Exception:
            self.groups[key] = prior
            raise


def update_message_date_only(old: dict, new: dict) -> tuple[float]:
    return (float(new["message"]["date"] - old["message"]["date"]),)
