#!/usr/bin/env python3
from __future__ import annotations

"""Durable routing from a transport conversation to a logical Agent session."""

import threading
import uuid
from pathlib import Path

from .session_manager import SessionRecord, SessionStore
from .transport_message import NormalizedInboundMessage


class TransportSessionRouter:
    def __init__(self, path: str | Path):
        self.store = SessionStore(path)
        self.store.ensure_loaded()
        self._lock = threading.RLock()

    @staticmethod
    def _new_session_id() -> str:
        return "SESSION-" + uuid.uuid4().hex[:20].upper()

    @staticmethod
    def _matches(record: SessionRecord, message: NormalizedInboundMessage) -> bool:
        metadata = dict(record.metadata or {})
        return (
            str(metadata.get("transport", "")).upper() == message.transport.upper()
            and str(metadata.get("conversation_key", "")) == message.conversation_key
        )

    def list_for(self, message: NormalizedInboundMessage) -> list[SessionRecord]:
        with self._lock:
            return [record for record in self.store.all() if self._matches(record, message)]

    def resolve(
        self,
        message: NormalizedInboundMessage,
        *,
        workspace: str,
        force_new: bool = False,
    ) -> SessionRecord:
        """Return the active logical session, or durably create a new one."""
        with self._lock:
            matching = self.list_for(message)
            active = next(
                (record for record in reversed(matching) if record.metadata.get("active", False)),
                None,
            )
            if active is not None and not force_new:
                return active

            for record in matching:
                if record.metadata.get("active", False):
                    record.metadata["active"] = False
                    self.store.put(record)

            record = SessionRecord(
                session_id=self._new_session_id(),
                description=f"{message.transport} conversation {message.conversation_key}",
                workspace=str(Path(workspace).resolve()),
                conversation_url=message.conversation_key,
                metadata={
                    "transport": message.transport.upper(),
                    "endpoint": message.endpoint,
                    "conversation_key": message.conversation_key,
                    "sender_id": message.sender_id,
                    "active": True,
                },
            )
            return self.store.put(record)

