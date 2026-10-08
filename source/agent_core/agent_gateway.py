#!/usr/bin/env python3
from __future__ import annotations

"""Unified durable ingress gateway for WebCopilot and RemoteAgent transports."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transport_message import NormalizedInboundMessage
from .transport_sessions import TransportSessionRouter
from .plan_ledger import PlanLedger, PlanLedgerError, parse_execute_plan_command


@dataclass(frozen=True)
class IngressResult:
    task: Any | None
    created: bool
    session_id: str
    response: str = ""


class AgentIngressGateway:
    """Turn one transport-neutral message into one durable agent task.

    The queue is an execution-backend boundary. RemoteAgent and WebCopilot may
    keep different worker lifecycles while sharing this ingress contract.
    """

    def __init__(self, *, task_queue, session_router: TransportSessionRouter, event_store=None, runtime_log=None):
        self.task_queue = task_queue
        self.session_router = session_router
        self.event_store = event_store
        self.runtime_log = runtime_log

    def _log(self, event: str, message: NormalizedInboundMessage, **detail) -> None:
        if self.runtime_log is not None:
            self.runtime_log.write(
                event,
                component="agent_gateway",
                transport=message.transport.upper(),
                conversation_key=message.conversation_key,
                **detail,
            )

    @staticmethod
    def _request_id(message: NormalizedInboundMessage) -> str:
        explicit = str(message.metadata.get("request_id", "") or "").strip()
        if explicit:
            return explicit
        digest = hashlib.sha256(message.idempotency_key.encode("utf-8", errors="replace")).hexdigest()
        return f"RR-{message.transport.upper()}-{digest[:16].upper()}"

    def accept(self, message: NormalizedInboundMessage, *, workspace: str) -> IngressResult:
        root = Path(workspace).resolve()
        if not root.is_dir():
            self._log("ERROR", message, stage="AUTHORIZATION", error="authorized_workspace_unavailable")
            raise ValueError("authorized_workspace_unavailable")

        text = str(message.text or "").strip()
        if not text:
            raise ValueError("empty_transport_message")

        force_new = text == "/new" or text.startswith("/new ")
        if force_new:
            text = text[4:].strip()
        session = self.session_router.resolve(message, workspace=str(root), force_new=force_new)
        if not text:
            return IngressResult(
                task=None,
                created=False,
                session_id=session.session_id,
                response=f"已建立新對話 {session.session_id}。",
            )

        source_plan_id = parse_execute_plan_command(text)
        source_plan = None
        original_text = text
        if source_plan_id:
            plan_ledger = PlanLedger.from_task_store(self.task_queue.store.path)
            try:
                source_plan = plan_ledger.load(source_plan_id)
                plan_ledger.ensure_workspace(source_plan, root)
            except PlanLedgerError as exc:
                reason = str(exc)
                messages = {
                    "plan_not_found": f"找不到規劃 {source_plan_id}。請確認 plan_id 是否完整。",
                    "plan_workspace_mismatch": (
                        f"規劃 {source_plan_id} 屬於其他 Workspace，為避免越權，不會執行。"
                    ),
                }
                return IngressResult(
                    task=None,
                    created=False,
                    session_id=session.session_id,
                    response=messages.get(reason, f"無法載入規劃 {source_plan_id}：{reason}"),
                )
            text = plan_ledger.execution_request(source_plan)

        route = {
            "transport": message.transport.upper(),
            "endpoint": message.endpoint,
            "conversation_key": message.conversation_key,
            "session_id": session.session_id,
            **dict(message.reply_context or {}),
        }
        request = {
            "type": "AGENT_TASK_REQUEST",
            "protocol": "agent_gateway",
            "protocol_version": 1,
            "request_id": self._request_id(message),
            "request": text,
            "workspace": str(root),
            "conversation_url": message.conversation_key,
            "transport": message.transport.upper(),
            "endpoint": message.endpoint,
            "session_id": session.session_id,
        }
        metadata = {
            "transport": message.transport.upper(),
            "reply_route": route,
            "source_message_id": message.source_message_id,
            "sender_id": message.sender_id,
            "attachments": [item.__dict__ for item in message.attachments],
            "authorized": True,
            "ingress_schema": "AGENT_INGRESS_V1",
            "ingress_metadata": dict(message.metadata or {}),
            "origin_turn_index": message.metadata.get("origin_turn_index"),
            "origin_role": str(message.metadata.get("origin_role", "") or ""),
            "source_plan_id": source_plan_id,
            "source_plan_request_id": str((source_plan or {}).get("request_id", "") or ""),
            "source_plan_task_id": str((source_plan or {}).get("task_id", "") or ""),
            "source_user_command": original_text,
        }
        task, created = self.task_queue.enqueue_remote_request(
            request,
            origin_turn_fingerprint=message.idempotency_key,
            metadata=metadata,
        )
        if created:
            if self.event_store is not None:
                self.event_store.emit("TASK_ACCEPTED", task, status="QUEUED", payload={})
            self._log(
                "TASK_ACCEPTED",
                message,
                request_id=task.request_id,
                task_id=task.task_id,
                session_id=session.session_id,
            )
        return IngressResult(task=task, created=created, session_id=session.session_id)


# Compatibility name used by existing RemoteAgent/Telegram callers.
TransportIngressAdapter = AgentIngressGateway

__all__ = ["AgentIngressGateway", "IngressResult", "TransportIngressAdapter"]
