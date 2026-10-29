"""Tri-One 5B: explicit, opt-in common V9 Worker choice.

No alternate CDP/browser or parallel worker is created here. Existing queue,
page lease, dispatcher and outbox remain authoritative. Default stays legacy
for WEBGPT_COPILOT and all unsupported transports.
"""
from __future__ import annotations
import os
from collections.abc import Mapping

WEB_COPILOT = "WEBGPT_COPILOT"


def use_shared_v9_worker(transport: str, env: Mapping[str, str] | None = None) -> bool:
    name = str(transport or "").strip().upper()
    if name in {"TELEGRAM", "LOCAL_TEST"}:
        return True
    if name != WEB_COPILOT:
        return False
    values = os.environ if env is None else env
    def on(key: str) -> bool:
        return str(values.get(key, "")).strip().lower() in {"1", "true", "yes"}
    # The existing 2A result contract and 5A admission must both be available.
    # 5B flag is the independent transport-owner switch.
    return all(on(key) for key in (
        "TRI_ONE_TASK_RESULT_V1", "TRI_ONE_TEXT_ONLY_V1",
        "TRI_ONE_WEBCOPILOT_V9_V1",
    ))


__all__ = ["WEB_COPILOT", "use_shared_v9_worker"]
