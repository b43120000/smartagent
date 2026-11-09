"""PokeClaw-style JSON extraction with deterministic, fail-closed decisions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


MAX_RESPONSE_BYTES = 32 * 1024
MAX_CANDIDATES = 48


class MinimalDecisionError(ValueError):
    def __init__(self, code: str, diagnostic: dict | None = None):
        super().__init__(code)
        self.code = code
        self.diagnostic = dict(diagnostic or {})
        self.diagnostic["reason_code"] = code


@dataclass(frozen=True)
class MinimalDecision:
    kind: str
    tool: str = ""
    arguments: dict | None = None
    message: str = ""
    expected_runtime: list[dict] | None = None
    goal_checks: list[dict] | None = None


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MinimalDecisionError("duplicate_json_key")
        result[key] = value
    return result


def _diagnostic(response: str) -> dict:
    text = str(response or "")
    # Only JSON structure survives. No raw reply, command or path in telemetry.
    syntax = set(' "{}[]:,\\' + "\r\n\t")
    return {
        "schema": "SMARTAGENT_MINIMAL_DECISION_DIAGNOSTIC_V1",
        "response_chars": len(text),
        "response_bytes": len(text.encode("utf-8", errors="replace")),
        "response_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "fence_detected": (chr(96) * 3) in text,
        "legacy_fence_detected": "smartagent_tool" in text.lower(),
        "response_structure_redacted": "".join(
            c if c in syntax else "x" for c in text[:600]
        ),
        "candidate_count": 0,
        "valid_decision_count": 0,
        "normalization": "NONE",
    }


def _balanced_objects(source: str) -> list[str]:
    """Extract outermost balanced objects, not nested dicts from their fields."""
    found = []
    start = -1
    depth = 0
    quoted = escaped = False
    for position, char in enumerate(source):
        if depth == 0:
            if char == "{":
                start, depth, quoted, escaped = position, 1, False, False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                found.append(source[start:position + 1])
                if len(found) > MAX_CANDIDATES:
                    raise MinimalDecisionError("json_candidate_limit_exceeded")
    return found


def _repair_explicit_path(candidate: str, original_goal: str) -> tuple[str, bool]:
    """Repair a single literal Windows path field only if verbatim in user goal.

    Never rewrite a command, a mixed escaped path or a guessed target path.
    """
    if not original_goal:
        return candidate, False
    rx = re.compile(r'("(?:path|working_directory)"\s*:\s*")([A-Za-z]:\\[^"\r\n]*?)(")')
    matches = list(rx.finditer(candidate))
    if len(matches) != 1:
        return candidate, False
    match = matches[0]
    value = match.group(2)
    if "\\\\" in value or value not in original_goal:
        return candidate, False
    repaired = candidate[:match.start(2)] + value.replace("\\", "\\\\") + candidate[match.end(2):]
    return repaired, True


def _field(data: dict, names: tuple[str, ...], meta: dict):
    existing = [name for name in names if name in data]
    if len(existing) > 1:
        raise MinimalDecisionError("conflicting_alias_fields", meta)
    if not existing:
        raise MinimalDecisionError("decision_required_field_missing", meta)
    if existing[0] != names[0] and meta["normalization"] == "NONE":
        meta["normalization"] = "POKECLAW_FIELD_ALIAS"
    return data[existing[0]]


def _to_decision(data: dict, meta: dict) -> MinimalDecision:
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise MinimalDecisionError("decision_type_required", meta)
    kind = data["type"].strip().lower()
    if kind in {"tool_call", "tool", "action"}:
        if not set(data).issubset({"type", "name", "tool", "arguments", "params"}):
            raise MinimalDecisionError("tool_call_fields_invalid", meta)
        name = _field(data, ("name", "tool"), meta)
        args = _field(data, ("arguments", "params"), meta)
        if not isinstance(name, str) or not name.strip() or not isinstance(args, dict):
            raise MinimalDecisionError("tool_name_or_arguments_invalid", meta)
        if kind != "tool_call" and meta["normalization"] == "NONE":
            meta["normalization"] = "POKECLAW_TYPE_ALIAS"
        return MinimalDecision("tool_call", tool=name.strip(), arguments=args)
    if kind in {"finish", "final"}:
        if not set(data).issubset({"type", "summary", "answer", "text", "expected_runtime", "goal_checks"}):
            raise MinimalDecisionError("decision_fields_invalid", meta)
        value = _field(data, ("summary", "answer", "text"), meta)
        if not isinstance(value, str) or not value.strip() or len(value) > 3000:
            raise MinimalDecisionError("decision_message_invalid", meta)
        expectations = data.get("expected_runtime")
        if expectations is not None and (
            not isinstance(expectations, list) or len(expectations) > 64
            or any(not isinstance(row, dict) for row in expectations)
        ):
            raise MinimalDecisionError("finish_expected_runtime_invalid", meta)
        goal_checks = data.get("goal_checks")
        if goal_checks is not None and (
            not isinstance(goal_checks, list) or len(goal_checks) > 64
            or any(not isinstance(row, dict) for row in goal_checks)
        ):
            raise MinimalDecisionError("finish_goal_checks_invalid", meta)
        return MinimalDecision(
            "finish", message=value.strip(), expected_runtime=expectations,
            goal_checks=goal_checks,
        )
    if kind == "blocked":
        if set(data) != {"type", "reason"}:
            raise MinimalDecisionError("decision_fields_invalid", meta)
        reason = data["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 3000:
            raise MinimalDecisionError("decision_message_invalid", meta)
        return MinimalDecision("blocked", message=reason.strip())
    raise MinimalDecisionError("unknown_decision_type", meta)


def _json_error_code(exc: json.JSONDecodeError) -> str:
    message = exc.msg.lower()
    if "invalid \\escape" in message:
        return "json_invalid_escape"
    if "invalid control character" in message:
        return "json_control_character"
    if "unterminated string" in message:
        return "json_unterminated_string"
    return "json_syntax_invalid"


def parse_minimal_decision_with_diagnostics(
    response: str, *, original_goal: str = "",
) -> tuple[MinimalDecision, dict]:
    text = str(response or "")
    meta = _diagnostic(text)
    source = text.lstrip("\ufeff").strip()
    if not source:
        raise MinimalDecisionError("decision_empty", meta)
    if meta["response_bytes"] > MAX_RESPONSE_BYTES:
        raise MinimalDecisionError("decision_oversized", meta)
    legacy_marker = (r"(?im)^[ \t]*" + re.escape(chr(96) * 3)
                     + r"smartagent_tool\b")
    if re.search(legacy_marker, source):
        raise MinimalDecisionError("legacy_protocol_response", meta)
    # A JSON array is not a single decision, even if it contains one object.
    if source.startswith("[") and source.endswith("]"):
        try:
            outer = json.loads(source, object_pairs_hook=_unique_keys)
        except (json.JSONDecodeError, MinimalDecisionError):
            outer = None
        if isinstance(outer, list):
            raise MinimalDecisionError("decision_object_required", meta)

    fence = re.escape(chr(96) * 3)
    full_fence = re.fullmatch(
        r"(?is)^" + fence + r"(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n" + fence + r"$",
        source,
    )
    if full_fence:
        source = full_fence.group(1).strip()
        meta["source_kind"] = "EXACT_JSON_FENCE"
    elif source.startswith("{") and source.endswith("}"):
        meta["source_kind"] = "DIRECT_JSON"
    else:
        meta["source_kind"] = "BALANCED_JSON_EXTRACTION"

    try:
        candidates = _balanced_objects(source)
    except MinimalDecisionError as exc:
        raise MinimalDecisionError(exc.code, meta) from exc
    meta["candidate_count"] = len(candidates)
    if not candidates:
        reason = "legacy_protocol_response" if meta["legacy_fence_detected"] else "json_object_not_found"
        raise MinimalDecisionError(reason, meta)

    valid = []
    first_problem = None
    for index, candidate in enumerate(candidates):
        # Explicit-path normalization runs before JSON decoding because
        # unescaped Windows sequences like backslash-n are syntactically
        # valid JSON but would otherwise silently change the target path.
        fixed, path_repaired = _repair_explicit_path(candidate, original_goal)
        try:
            parsed = json.loads(fixed, object_pairs_hook=_unique_keys)
        except json.JSONDecodeError as exc:
            if first_problem is None:
                meta.update(json_error_line=exc.lineno,
                            json_error_column=exc.colno,
                            json_error_position=exc.pos,
                            json_error_message_code=_json_error_code(exc))
                first_problem = _json_error_code(exc)
            continue
        except MinimalDecisionError as exc:
            first_problem = first_problem or exc.code
            continue
        if path_repaired:
            meta["normalization"] = "EXPLICIT_WINDOWS_PATH_BACKSLASH_ESCAPED"
        try:
            decision = _to_decision(parsed, meta)
        except MinimalDecisionError as exc:
            first_problem = first_problem or exc.code
            continue
        valid.append((decision, index))

    meta["valid_decision_count"] = len(valid)
    if len(valid) > 1:
        raise MinimalDecisionError("ambiguous_multiple_decisions", meta)
    if len(valid) == 1:
        if len(candidates) != 1:
            # Do not dispatch one actionable JSON from a response containing
            # other JSON candidates (even if those other candidates are invalid).
            raise MinimalDecisionError("ambiguous_mixed_json_candidates", meta)
        decision, index = valid[0]
        meta["selected_candidate_index"] = index
        meta["decision_type"] = decision.kind
        if meta["source_kind"] == "BALANCED_JSON_EXTRACTION" and meta["normalization"] == "NONE":
            meta["normalization"] = "EXTRACT_UNIQUE_EMBEDDED_JSON"
        meta["status"] = "ACCEPTED"
        return decision, meta
    if meta["legacy_fence_detected"] and first_problem in {
        "decision_type_required", "unknown_decision_type", None,
    }:
        first_problem = "legacy_protocol_response"
    raise MinimalDecisionError(first_problem or "decision_not_found", meta)


def parse_minimal_decision(response: str, *, original_goal: str = "") -> MinimalDecision:
    decision, _ = parse_minimal_decision_with_diagnostics(
        response, original_goal=original_goal)
    return decision
