#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility-first task-mode and carrier routing contracts.

This module introduces the selection boundary only.  Every task mode still
maps to the existing SmartAgent planner loop, and every remote network message
still maps to the existing ChatGPT conversation carrier.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

MODE_AUTO = "AUTO"
MODE_FAST_TOOL = "FAST_TOOL"
MODE_DEV_AGENT = "DEV_AGENT"
MODE_SYSTEM_OPS = "SYSTEM_OPS"
MODE_GENERAL_AGENT = "GENERAL_AGENT"
TASK_MODES = {
    MODE_AUTO, MODE_FAST_TOOL, MODE_DEV_AGENT, MODE_SYSTEM_OPS, MODE_GENERAL_AGENT,
}

EXECUTION_CURRENT_PLANNER_LOOP = "CURRENT_PLANNER_LOOP"

CARRIER_AUTO = "AUTO"
CARRIER_CHATGPT_CONVERSATION = "CHATGPT_CONVERSATION"
CARRIER_LOCAL_CONSOLE = "LOCAL_CONSOLE"
CARRIERS = {CARRIER_AUTO, CARRIER_CHATGPT_CONVERSATION, CARRIER_LOCAL_CONSOLE}

SYNC_AUTO="AUTO"
SYNC_NONE="NONE"
SYNC_DIRECT="DIRECT"
SYNC_DELTA="DELTA"
SYNC_FULL_BUNDLE="FULL_BUNDLE"
CONTEXT_SYNC_STRATEGIES={SYNC_AUTO,SYNC_NONE,SYNC_DIRECT,SYNC_DELTA,SYNC_FULL_BUNDLE}


@dataclass(frozen=True)
class ModeRoute:
    requested_mode: str
    suggested_mode: str
    selected_mode: str
    execution_path: str = EXECUTION_CURRENT_PLANNER_LOOP
    compatibility_fallback: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextSyncRoute:
    requested_strategy: str
    selected_strategy: str
    explicit_override: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CarrierRoute:
    requested_carrier: str
    selected_carrier: str
    source: str
    compatibility_fallback: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalized_choice(value: object, allowed: set[str], default: str) -> str:
    choice = str(value or default).strip().upper()
    return choice if choice in allowed else default


def suggest_task_mode(request: str) -> str:
    """Return advisory mode metadata without changing the execution algorithm."""
    text = str(request or "").lower()
    system_terms = ("安裝", "環境變數", "系統服務", "administrator", "系統設定")
    dev_terms = (
        "修改程式", "修改代碼", "修 bug", "除錯", "編譯", "build", "測試程式",
        "refactor", "source code", "程式碼",
    )
    fast_terms = ("列出", "統計", "多少檔案", "workspace 檔案", "workspace的檔案")
    if any(term in text for term in system_terms):
        return MODE_SYSTEM_OPS
    if any(term in text for term in dev_terms):
        return MODE_DEV_AGENT
    if any(term in text for term in fast_terms):
        return MODE_FAST_TOOL
    return MODE_GENERAL_AGENT


def select_task_mode(request: str, requested_mode: object = MODE_AUTO) -> ModeRoute:
    requested = _normalized_choice(requested_mode, TASK_MODES, MODE_AUTO)
    suggested = suggest_task_mode(request) if requested == MODE_AUTO else requested
    # Compatibility milestone: record the decision, but keep all work on the
    # one execution path that existed before task modes were introduced.
    return ModeRoute(
        requested_mode=requested,
        suggested_mode=suggested,
        selected_mode=suggested,
    )


def select_context_sync(request: str, requested_strategy: object = SYNC_AUTO) -> ContextSyncRoute:
    requested=_normalized_choice(requested_strategy,CONTEXT_SYNC_STRATEGIES,SYNC_AUTO)
    text=str(request or "").lower()
    explicit=bool("/bundle" in text or "/project-sync" in text or "完整 project sync" in text)
    selected=SYNC_FULL_BUNDLE if explicit else requested
    return ContextSyncRoute(requested_strategy=requested,selected_strategy=selected,explicit_override=explicit)


def select_carrier(
    *, source: str, conversation_url: str = "", requested_carrier: object = CARRIER_AUTO
) -> CarrierRoute:
    requested = _normalized_choice(requested_carrier, CARRIERS, CARRIER_AUTO)
    normalized_source = str(source or "local").strip().lower()
    if normalized_source == "remote" or conversation_url:
        selected = CARRIER_CHATGPT_CONVERSATION
    else:
        selected = CARRIER_LOCAL_CONSOLE
    # Requested future adapters intentionally fall back until implemented.
    if requested not in {CARRIER_AUTO, selected}:
        selected = CARRIER_CHATGPT_CONVERSATION if conversation_url else CARRIER_LOCAL_CONSOLE
    return CarrierRoute(
        requested_carrier=requested,
        selected_carrier=selected,
        source=normalized_source,
    )


def build_route_context(
    *, request: str, source: str, conversation_url: str = "",
    requested_mode: object = MODE_AUTO, requested_carrier: object = CARRIER_AUTO,
    requested_context_sync: object = SYNC_AUTO,
) -> dict[str, Any]:
    return {
        "mode": select_task_mode(request, requested_mode).as_dict(),
        "context_sync": select_context_sync(request, requested_context_sync).as_dict(),
        "carrier": select_carrier(
            source=source,
            conversation_url=conversation_url,
            requested_carrier=requested_carrier,
        ).as_dict(),
    }


def route_context_lines(context: dict[str, Any]) -> list[str]:
    """Return stable human/WebGPT-visible routing facts."""
    mode = dict(context.get("mode") or {})
    carrier = dict(context.get("carrier") or {})
    sync = dict(context.get("context_sync") or {})
    return [
        f"Mode={mode.get('selected_mode', MODE_GENERAL_AGENT)}",
        f"Execution={mode.get('execution_path', EXECUTION_CURRENT_PLANNER_LOOP)}",
        f"ContextSync={sync.get('selected_strategy', SYNC_AUTO)}",
        f"Carrier={carrier.get('selected_carrier', CARRIER_LOCAL_CONSOLE)}",
        f"Source={carrier.get('source', 'local')}",
    ]


def format_route_context(context: dict[str, Any], *, heading: str = "SmartAgent Route") -> str:
    return f"[{heading}]\n" + "\n".join(route_context_lines(context))
