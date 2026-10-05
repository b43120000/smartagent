#!/usr/bin/env python3
"""Cross-process ownership metadata for canonical ChatGPT conversations."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .conversation_identity import conversation_id
from .process_file_lock import _pid_alive, exclusive_process_lock
from .paths import conversation_ownership_path


class ConversationOwnershipRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.state_path = conversation_ownership_path(self.root)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")

    @staticmethod
    def new_owner_token(interface: str) -> str:
        return f"{str(interface or 'unknown')}:{os.getpid()}:{uuid.uuid4().hex}"

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
            data = {}
        conversations = dict(data.get("conversations") or {})
        for cid, row in list(conversations.items()):
            owners = dict((row or {}).get("owners") or {})
            for token, owner in list(owners.items()):
                pid = int((owner or {}).get("pid", 0) or 0)
                if pid > 0 and not _pid_alive(pid):
                    owners.pop(token, None)
            if owners:
                row = dict(row or {})
                row["owners"] = owners
                conversations[cid] = row
            else:
                conversations.pop(cid, None)
        return {"version": 1, "conversations": conversations}

    def _write(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def claim(self, url_or_id: str, owner_token: str, *, interface: str, pid: int | None = None) -> str:
        cid = conversation_id(url_or_id) or str(url_or_id or "").strip().lower()
        if not cid:
            return ""
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0, label="conversation ownership"):
            state = self._load()
            conversations = state["conversations"]
            row = dict(conversations.get(cid) or {})
            owners = dict(row.get("owners") or {})
            owners[str(owner_token)] = {"pid": int(pid or os.getpid()), "interface": str(interface or "unknown"), "references": 1, "updated_at": time.time()}
            conversations[cid] = {"conversation_id": cid, "owners": owners, "references": len(owners)}
            self._write(state)
        return cid

    def release(self, url_or_id: str, owner_token: str) -> int:
        cid = conversation_id(url_or_id) or str(url_or_id or "").strip().lower()
        if not cid:
            return 0
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0, label="conversation ownership"):
            state = self._load()
            row = dict(state["conversations"].get(cid) or {})
            owners = dict(row.get("owners") or {})
            owners.pop(str(owner_token), None)
            remaining = len(owners)
            if remaining:
                row.update(owners=owners, references=remaining)
                state["conversations"][cid] = row
            else:
                state["conversations"].pop(cid, None)
            self._write(state)
            return remaining

    def references(self, url_or_id: str) -> int:
        cid = conversation_id(url_or_id) or str(url_or_id or "").strip().lower()
        if not cid:
            return 0
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0, label="conversation ownership"):
            state = self._load()
            self._write(state)
            return len(dict((state["conversations"].get(cid) or {}).get("owners") or {}))

    def prune_dead_owners(self) -> int:
        """Persist dead-PID pruning and return the remaining owner count."""
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="conversation ownership"
        ):
            state = self._load()
            self._write(state)
            return sum(
                len(dict((row or {}).get("owners") or {}))
                for row in state["conversations"].values()
            )
