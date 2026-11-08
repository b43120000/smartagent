"""Single-decision JSON transport for the primary SmartAgent core loop."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re


class MinimalDecisionError(ValueError):
    pass


@dataclass(frozen=True)
class MinimalDecision:
    kind: str
    tool: str = ""
    arguments: dict | None = None
    message: str = ""


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MinimalDecisionError("duplicate_json_key:" + key)
        result[key] = value
    return result


def parse_minimal_decision(response: str) -> MinimalDecision:
    """Accept only one complete JSON object, never JSON fragments from prose."""
    source = str(response or "").strip()
    fence = chr(96) * 3
    pattern = re.escape(fence) + r"(?:json)?\s*\r?\n([\s\S]*?)\r?\n" + re.escape(fence)
    fenced = re.fullmatch(pattern, source, re.I)
    if fenced:
        source = fenced.group(1).strip()
    if not source or len(source.encode("utf-8")) > 32 * 1024:
        raise MinimalDecisionError("decision_empty_or_oversized")
    try:
        raw = json.loads(source, object_pairs_hook=_unique_keys)
    except MinimalDecisionError:
        raise
    except (ValueError, TypeError) as exc:
        raise MinimalDecisionError("invalid_json_decision") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise MinimalDecisionError("decision_type_required")
    kind = raw["type"].strip().lower()
    if kind == "tool_call":
        if set(raw) != {"type", "name", "arguments"}:
            raise MinimalDecisionError("tool_call_fields_invalid")
        tool, args = raw.get("name"), raw.get("arguments")
        if not isinstance(tool, str) or not tool or not isinstance(args, dict):
            raise MinimalDecisionError("tool_name_or_arguments_invalid")
        return MinimalDecision(kind, tool=tool, arguments=args)
    if kind in {"finish", "blocked"}:
        field = "summary" if kind == "finish" else "reason"
        if set(raw) != {"type", field}:
            raise MinimalDecisionError("decision_fields_invalid")
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 3000:
            raise MinimalDecisionError("decision_message_invalid")
        return MinimalDecision(kind, message=value.strip())
    raise MinimalDecisionError("unknown_decision_type")
