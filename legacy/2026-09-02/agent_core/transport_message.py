#!/usr/bin/env python3
from __future__ import annotations

"""Transport-neutral inbound message contracts for RemoteAgent ingress."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundAttachment:
    kind: str
    transport_id: str
    file_name: str = ""
    mime_type: str = ""
    size: int = 0
    local_path: str = ""


@dataclass(frozen=True)
class NormalizedInboundMessage:
    transport: str
    endpoint: str
    conversation_key: str
    sender_id: str
    source_message_id: str
    text: str
    received_at: float
    idempotency_key: str
    reply_context: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[InboundAttachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attachments"] = [asdict(item) for item in self.attachments]
        return payload

