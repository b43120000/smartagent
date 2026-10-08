#!/usr/bin/env python3
"""Behavior-preserving action dispatch boundary for SmartAgent Runtime actions."""
from __future__ import annotations

from .tools import execute_tool


class ActionDispatcher:
    """Dispatch canonical actions without changing executor semantics."""

    def dispatch(self, action: dict, *, agent=None, models: dict | None = None) -> str:
        return execute_tool(action, agent=agent, models=models)


_DEFAULT_DISPATCHER = ActionDispatcher()


def dispatch_action(action: dict, *, agent=None, models: dict | None = None) -> str:
    """Dispatch one action through the shared default dispatcher."""
    return _DEFAULT_DISPATCHER.dispatch(action, agent=agent, models=models)


__all__ = ["ActionDispatcher", "dispatch_action"]
