"""Tri-One 5B: shared V9 Worker is the default for WebCopilot.

Telegram/LOCAL_TEST already run this Worker. The existing queue, execution
page lease, dispatcher, TaskResult/Progress, and outbox remain authoritative.
WebCopilot can opt OUT explicitly without changing unrelated 2A/5A flags.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

WEB_COPILOT = "WEBGPT_COPILOT"
_SWITCH = "TRI_ONE_WEBCOPILOT_V9_V1"
_ENABLED = frozenset({"1", "true", "yes", "on"})
_DISABLED = frozenset({"0", "false", "no", "off"})


def use_shared_v9_worker(transport: str, env: Mapping[str, str] | None = None) -> bool:
    """Choose one existing worker path; no model/DOM task is executed here.

    Unset/blank switch: WebCopilot uses the common V9 worker.
    Explicit false: use the previous smart_agent.py worker.
    Unrecognized value: fail closed to the legacy path, not an implicit ON.

    TRI_ONE_TASK_RESULT_V1 and TRI_ONE_TEXT_ONLY_V1 remain independent:
    enabling a common Worker does not authorize 2A metadata or 5A fast path.
    """
    name = str(transport or "").strip().upper()
    if name in {"TELEGRAM", "LOCAL_TEST"}:
        return True
    if name != WEB_COPILOT:
        return False
    values = os.environ if env is None else env
    raw = str(values.get(_SWITCH, "") or "").strip().lower()
    if not raw:
        return True
    if raw in _ENABLED:
        return True
    return False


__all__ = ["WEB_COPILOT", "use_shared_v9_worker"]
