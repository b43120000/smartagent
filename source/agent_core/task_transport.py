#!/usr/bin/env python3
from __future__ import annotations

"""Transport-aware validation for durable Agent task origins."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from agent_core.workspace import normalize_chatgpt_url, normalize_workspace_path

WEBGPT_TRANSPORTS = {"WEBGPT", "WEBGPT_COPILOT"}


@dataclass(frozen=True)
class TaskTransportContext:
    transport: str
    conversation_key: str
    route_source: str
    chatgpt_url: str = ""

    @property
    def is_webgpt(self) -> bool:
        return self.transport in WEBGPT_TRANSPORTS


def task_transport(task: Any) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {})
    reply_route = dict(getattr(task, "reply_route", {}) or metadata.get("reply_route") or {})
    value = str(metadata.get("transport") or reply_route.get("transport") or "WEBGPT")
    return value.strip().upper() or "WEBGPT"


def validate_task_transport(task: Any, *, registry=None) -> TaskTransportContext:
    """Validate only the identity rules owned by the task's transport.

    Non-WebGPT transports must never enter ChatGPT URL normalization.
    """
    transport = task_transport(task)
    workspace = Path(str(getattr(task, "workspace", "") or "")).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError("remote_workspace_unavailable")

    metadata = dict(getattr(task, "metadata", {}) or {})
    reply_route = dict(getattr(task, "reply_route", {}) or metadata.get("reply_route") or {})
    conversation_key = str(
        reply_route.get("conversation_key")
        or reply_route.get("conversation_url")
        or getattr(task, "conversation_url", "")
        or ""
    ).strip()

    if transport in WEBGPT_TRANSPORTS:
        chatgpt_url = str(getattr(task, "conversation_url", "") or "").strip()
        if registry is None or registry.find(str(workspace), chatgpt_url) is None:
            raise RuntimeError("remote_task_origin_not_registered")
        return TaskTransportContext(
            transport=transport,
            conversation_key=conversation_key or chatgpt_url,
            route_source="WEBGPT_COPILOT" if transport == "WEBGPT_COPILOT" else "remote",
            chatgpt_url=chatgpt_url,
        )

    if not bool(metadata.get("authorized", False)):
        raise RuntimeError("remote_task_not_authorized")
    if str(reply_route.get("transport", "") or "").upper() != transport:
        raise RuntimeError("remote_reply_transport_mismatch")
    if not conversation_key:
        raise RuntimeError("remote_reply_route_missing")

    if transport == "TELEGRAM":
        if not conversation_key.startswith("telegram://chat/"):
            raise RuntimeError("telegram_conversation_key_invalid")
        try:
            chat_id = int(reply_route.get("chat_id", 0) or 0)
        except (TypeError, ValueError):
            chat_id = 0
        if chat_id <= 0:
            raise RuntimeError("telegram_reply_chat_missing")

    return TaskTransportContext(
        transport=transport,
        conversation_key=conversation_key,
        route_source=transport,
        chatgpt_url="",
    )


def _new_chat_execution_url(linked_url: str) -> str:
    """Return the canonical linked conversation used by the execution broker."""
    return normalize_chatgpt_url(linked_url)


def resolve_task_execution_chatgpt_url(task: Any, *, registry) -> str:
    """Resolve the WebGPT surface used by a request-scoped Agent1 worker.

    A non-WebGPT transport owns its own conversation identity (for example a
    Telegram chat), so transport validation must never reinterpret that value
    as a ChatGPT URL.  Execution is a separate concern: select the most useful
    registered ChatGPT conversation for the task workspace, preferring an
    enabled exact conversation over a generic ChatGPT home-page binding.
    """
    from .remote_binding import active
    binding = active()
    if binding:
        if normalize_workspace_path(str(getattr(task, 'workspace', '') or '')) != binding['workspace']:
            raise RuntimeError('remote_task_workspace_binding_mismatch')
        return binding['gpt_url']
    context = validate_task_transport(task, registry=registry)
    if context.chatgpt_url:
        return _new_chat_execution_url(context.chatgpt_url)

    workspace = normalize_workspace_path(str(getattr(task, "workspace", "") or ""))
    env_url = str(os.environ.get("SMARTAGENT_REMOTE_EXECUTION_URL", "") or "").strip()
    if env_url:
        return _new_chat_execution_url(normalize_chatgpt_url(env_url))
    primary_path = Path(registry.path).with_name("remote_primary_binding.json")
    try:
        primary = json.loads(primary_path.read_text(encoding="utf-8"))
        if normalize_workspace_path(str(primary.get("workspace", ""))) == workspace:
            primary_url = normalize_chatgpt_url(str(primary.get("gpt_url", "")))
            if registry.find(workspace, primary_url) is not None:
                return _new_chat_execution_url(primary_url)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    metadata = dict(getattr(task, "metadata", {}) or {})
    explicit = str(metadata.get("execution_chatgpt_url") or "").strip()
    if explicit:
        explicit = normalize_chatgpt_url(explicit)
        if registry.find(workspace, explicit) is not None:
            return _new_chat_execution_url(explicit)

    rows = registry.list_conversations()
    candidates: list[tuple[tuple[int, int, float], str]] = []
    for row in rows:
        if str(row.get("purpose", "general") or "general") != "general":
            continue
        try:
            row_workspace = normalize_workspace_path(str(row.get("workspace", "") or ""))
            url = normalize_chatgpt_url(str(row.get("gpt_url", "") or ""))
        except ValueError:
            continue
        if row_workspace != workspace:
            continue
        score = (
            int(bool(row.get("remote_enabled", False))),
            int("/c/" in url),
            float(row.get("updated_at", 0.0) or 0.0),
        )
        candidates.append((score, url))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return _new_chat_execution_url(candidates[0][1])
    return normalize_chatgpt_url("https://chatgpt.com/")


__all__ = [
    "TaskTransportContext", "WEBGPT_TRANSPORTS", "task_transport",
    "validate_task_transport", "resolve_task_execution_chatgpt_url",
]
