#!/usr/bin/env python3
"""SmartAgent Tool Protocol v9 compatibility surface.

The compact structured wire format remains v8-compatible.  Version 9 adds a
runtime-owned narrative decision bridge before admission; canonical envelopes
still pass through the proven v8 validators and exactly-once execution path.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import protocol_v8 as _v8
from .protocol_v8 import *  # noqa: F401,F403
from .protocol_v8 import parse_v8_tool_transport

PROTOCOL_FAMILY = "SMARTAGENT_V9"
PROTOCOL_VERSION = 9

SINGLE_FENCE_TRANSPORT_MARKER = "SMARTAGENT_SINGLE_FENCE_TRANSPORT_V1"
SINGLE_FENCE_TRANSPORT_CONTRACT = (
    f"[{SINGLE_FENCE_TRANSPORT_MARKER}]\n"
    "Use exactly one opening ```smartagent_tool fence and exactly one closing ``` fence "
    "for the entire reply. Put every complete JSON object on its own line inside that one fence. "
    "Do not open a separate fence for each object. Do not place any JSON or natural language "
    "outside the fence. The object order is: exactly one report_progress, then the currently "
    "decidable action object(s) or one final_response, then exactly one final "
    "{\"tool\":\"turn_commit\",\"action_count\":N} object inside the same fence. "
    "N counts every preceding JSON object and never counts turn_commit itself.\n"
    f"[/{SINGLE_FENCE_TRANSPORT_MARKER}]"
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_candidates(source: str) -> list[Any]:
    """Return one exclusive raw/fenced JSON value from a non-v8 response.

    JSON embedded in prose is intentionally left to the narrative bridge: an
    example quoted by the model must never become executable authority merely
    because it is the only object in the reply.
    """
    candidates: list[Any] = []
    generic_fence = re.fullmatch(
        r"```json\s*\r?\n([\s\S]*?)\r?\n```",
        source,
        flags=re.IGNORECASE,
    )
    probes = [generic_fence.group(1).strip()] if generic_fence else [source]
    for probe in probes:
        try:
            decoded = json.loads(probe, object_pairs_hook=_reject_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if decoded is not None:
            candidates.append(decoded)
    return candidates


def _decision_blocks(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, dict) and isinstance(value.get("blocks"), list):
        value = value["blocks"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, dict) and isinstance(item.get("tool"), str) for item in value):
        return None
    return [dict(item) for item in value]


def _canonicalize_json_transport(source: str, *, action_id_seed: str = "") -> str | None:
    candidates = _json_candidates(source)
    if len(candidates) != 1:
        return None
    blocks = _decision_blocks(candidates[0])
    if not blocks:
        return None
    commit_indexes = [
        index for index, block in enumerate(blocks)
        if block.get("tool") == "turn_commit"
    ]
    if len(commit_indexes) > 1 or (commit_indexes and commit_indexes[0] != len(blocks) - 1):
        return None
    blocks = [block for block in blocks if block.get("tool") != "turn_commit"]
    if not blocks:
        return None
    digest = hashlib.sha256(
        (str(action_id_seed or "") + "\0" + source).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    for index, block in enumerate(blocks, 1):
        # Raw/generic JSON is a compatibility fallback, not canonical model
        # authority. Preserve the existing contract: Runtime owns every
        # correlation ID on this path. Canonical fenced/DOM v9 transport only
        # fills an actually missing action_id in the primary parser above.
        block["action_id"] = f"V9-RUNTIME-{digest}-{index}"
    blocks.append({"tool": "turn_commit", "action_count": len(blocks)})
    return "\n".join(
        "```smartagent_tool\n"
        + json.dumps(block, ensure_ascii=False, separators=(",", ":"))
        + "\n```"
        for block in blocks
    )


def parse_v9_tool_transport_detailed(
    text: str,
    *,
    action_id_seed: str = "",
) -> _v8.TransportParseResult:
    """Parse v9 transport while preserving v8 normalization diagnostics."""
    source_text = str(text or "")
    digest = hashlib.sha256(
        (str(action_id_seed or "") + "\0" + source_text).encode("utf-8", errors="replace")
    ).hexdigest()[:12]

    def runtime_action_id(action: dict[str, Any], index: int) -> str:
        tool = re.sub(r"[^A-Za-z0-9]+", "-", str(action.get("tool", "ACTION"))).strip("-")
        return f"V9-RUNTIME-{digest}-{index}-{tool or 'ACTION'}"

    result = _v8.parse_v8_tool_transport_detailed(
        text,
        missing_action_id_factory=runtime_action_id,
    )
    if not result.diagnostics:
        return result
    source = source_text.strip()
    canonical = _canonicalize_json_transport(
        source,
        action_id_seed=action_id_seed,
    ) if source else None
    if canonical is None:
        return result
    recovered = _v8.parse_v8_tool_transport_detailed(canonical)
    if recovered.diagnostics:
        return recovered
    return _v8.TransportParseResult(
        calls=recovered.calls,
        diagnostics=[],
        normalizations=["V9_EXCLUSIVE_JSON_CANONICALIZED", *recovered.normalizations],
        transport_kind="v9_json_fallback",
        block_map=recovered.block_map,
    )


def parse_v9_tool_transport(text: str, *, action_id_seed: str = ""):
    """Compatibility wrapper for v9 transport parsing."""
    result = parse_v9_tool_transport_detailed(text, action_id_seed=action_id_seed)
    return result.calls, result.diagnostics


__all__ = [
    *[name for name in _v8.__all__ if name not in {"PROTOCOL_FAMILY", "PROTOCOL_VERSION"}],
    "PROTOCOL_FAMILY",
    "PROTOCOL_VERSION",
    "SINGLE_FENCE_TRANSPORT_CONTRACT",
    "SINGLE_FENCE_TRANSPORT_MARKER",
    "parse_v9_tool_transport",
    "parse_v9_tool_transport_detailed",
]
