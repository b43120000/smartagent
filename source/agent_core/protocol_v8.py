"""SmartAgent Tool Protocol v8 contract primitives.

Milestone 1 deliberately keeps this module independent from the existing
execution loops.  It defines the v8 model-facing envelope, runtime-owned
correlation records, deterministic digests, attachment states, and bounded
field-repair rules.  Later milestones may use these primitives from Local,
WebDirect, and RemoteAgent without letting a transport redefine the contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .command_operation import operation_mismatch_detail


PROTOCOL_NAME = "smartagent"
PROTOCOL_FAMILY = "SMARTAGENT_V8"
PROTOCOL_VERSION = 8

MODEL_ACTION_FIELDS = {"tool", "action_id"}
MODEL_COMMIT_FIELDS = {"tool", "action_count"}
TOOL_REQUIRED_FIELDS = {
    "run_command": {"command", "operation"}, "read_file": {"path"},
    "write_file": {"path", "content"},
    "delete_path": {"path", "recursive", "reason"},
    "begin_file_write": {"write_id", "path", "encoding", "overwrite"},
    "write_file_chunk": {"write_id", "content"},
    "commit_file_write": {"write_id"}, "abort_file_write": {"write_id"},
    "inspect_directory": {"paths"}, "update_semantic_map": {"patch"},
    "update_semantic_map_file": {"path"},
    "compare_project_snapshot": {"known_snapshot"},
    "build_project_delta": {"base_snapshot_id"}, "project_sync": {"strategy"},
    "query_project": {"project_root", "queries"},
    "validate_edit_plan": {"plan"}, "apply_edit_plan": {"plan"},
    "aggregate_verification": {"commands"}, "propose_task_plan": {"plan"},
    "repair_task_plan": {"plan"},
    "propose_task_plan_file": {"path"}, "execute_frozen_plan": {"plan_id"},
    "web_search": {"query"}, "find_file": {"name"}, "upload_file": {"path"},
    "upload_files": {"paths"}, "return_artifact": {"path"},
    "google_drive_upload": {"path"},
    "web_edit_file": {"path", "instruction"},
    "download_artifact": {"output_path"}, "execute_artifact_bundle": {"path"},
    "save_session_summary": {"summary"},
    "ask_executor": {"instruction", "allow_local_ai_fallback", "run_id"},
    "report_progress": {"current_step", "total_steps", "current_focus"},
    "final_response": {"content"},
}
PROJECT_SYNC_STRATEGIES = frozenset({"INDEX_ONLY", "DELTA", "FULL_BUNDLE"})
RUNTIME_OWNED_FIELDS = {
    "protocol_name", "protocol_version", "request_id", "task_id", "task_epoch",
    "intent_digest", "action_digest", "result_id", "result_digest",
    "attachment_id", "turn_id", "local_nonce", "web_ack_id", "ack_result_id",
    "ack_web_ack_id", "checkpoint_id",
}

REPAIR_FIELD = "FIELD_REPAIR"
REPLAN_ACTION = "ACTION_REPLAN"
RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
ATTACHMENT_NOT_STABLE = "ATTACHMENT_NOT_STABLE"

ATTACHMENT_STATES = (
    "INTENT", "STAGED", "UPLOADING", "VISIBLE", "READY", "STABLE",
    "SUBMITTED", "CONSUMED", "FAILED",
)

_ATTACHMENT_TRANSITIONS = {
    "INTENT": {"STAGED", "FAILED"},
    "STAGED": {"UPLOADING", "FAILED"},
    "UPLOADING": {"VISIBLE", "FAILED"},
    "VISIBLE": {"READY", "FAILED"},
    "READY": {"STABLE", "FAILED"},
    "STABLE": {"SUBMITTED", "FAILED"},
    "SUBMITTED": {"CONSUMED", "FAILED"},
    "CONSUMED": set(),
    "FAILED": set(),
}


class ProtocolV8Error(ValueError):
    """Deterministic v8 contract violation."""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code)
        self.detail = str(detail)
        message = self.code if not self.detail else f"{self.code}:{self.detail}"
        super().__init__(message)


@dataclass
class TransportParseResult:
    """Detailed, non-authoritative observations from one transport parse.

    ``calls`` is populated only after the complete transport, action schemas,
    and trailing commit all validate. ``block_map`` and ``normalizations`` are
    diagnostic metadata and never grant execution authority on their own.
    """

    calls: list[dict[str, Any]]
    diagnostics: list[dict[str, str]]
    normalizations: list[str]
    transport_kind: str
    block_map: list[dict[str, Any]]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ProtocolV8Error("NON_CANONICAL_VALUE", type(value).__name__)


def canonical_json(value: Any) -> str:
    """Return the single JSON representation used by v8 digests."""
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ProtocolV8Error):
            raise
        raise ProtocolV8Error("NON_CANONICAL_VALUE", str(exc)) from exc


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_non_empty_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolV8Error("MISSING_OR_INVALID_FIELD", field)
    return value


@dataclass(frozen=True)
class V8RequestContext:
    request_id: str
    task_id: str
    task_epoch: str
    intent_digest: str

    def __post_init__(self) -> None:
        for field in ("request_id", "task_id", "task_epoch", "intent_digest"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ProtocolV8Error("INVALID_REQUEST_CONTEXT", field)

    def to_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "task_epoch": self.task_epoch,
            "intent_digest": self.intent_digest,
        }

    def matches(self, payload: Mapping[str, Any]) -> bool:
        return all(payload.get(key) == value for key, value in self.to_dict().items())


def session_identity(session_id: str, protocol_hash: str = "") -> dict[str, Any]:
    session = _require_non_empty_string({"session_id": session_id}, "session_id")
    return {
        "protocol_name": PROTOCOL_NAME,
        "protocol_family": PROTOCOL_FAMILY,
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session,
        "protocol_hash": str(protocol_hash or ""),
    }


def validate_session_identity(payload: Mapping[str, Any], session_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ProtocolV8Error("INVALID_SESSION_IDENTITY", "not_object")
    expected = session_identity(session_id, str(payload.get("protocol_hash", "") or ""))
    for field in ("protocol_name", "protocol_family", "protocol_version", "session_id"):
        if payload.get(field) != expected[field]:
            raise ProtocolV8Error("SESSION_PROTOCOL_MISMATCH", field)
    return dict(payload)


def validate_model_action(
    action: Mapping[str, Any],
    *,
    allow_missing_fields: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ProtocolV8Error("ACTION_NOT_OBJECT")
    tool = _require_non_empty_string(action, "tool")
    action_id = _require_non_empty_string(action, "action_id")
    if tool == "turn_commit":
        raise ProtocolV8Error("ACTION_TOOL_RESERVED", tool)
    for field in TOOL_REQUIRED_FIELDS.get(tool, set()):
        if field not in action and field not in allow_missing_fields:
            raise ProtocolV8Error("MISSING_OR_INVALID_FIELD", field)
    for field in RUNTIME_OWNED_FIELDS:
        if field in action:
            raise ProtocolV8Error("RUNTIME_FIELD_IN_MODEL_ACTION", field)
    result = dict(action)
    result["tool"] = tool
    result["action_id"] = action_id
    if tool == "run_command" and "operation" in result:
        try:
            mismatch = operation_mismatch_detail(result)
        except ValueError as exc:
            raise ProtocolV8Error("RUN_COMMAND_OPERATION_INVALID", str(exc)) from exc
        if mismatch:
            raise ProtocolV8Error("RUN_COMMAND_OPERATION_MISMATCH", mismatch)
    if tool == "project_sync" and "strategy" in result:
        strategy = str(result.get("strategy", "") or "").strip().upper()
        if strategy not in PROJECT_SYNC_STRATEGIES:
            raise ProtocolV8Error(
                "PROJECT_SYNC_STRATEGY_INVALID",
                f"actual={strategy or '(empty)'};allowed="
                + ",".join(sorted(PROJECT_SYNC_STRATEGIES))
                + ";DIRECT is an access mode, not a project_sync strategy",
            )
        result["strategy"] = strategy
    return result


def action_digest(action: Mapping[str, Any]) -> str:
    """Digest only the model decision, never runtime correlation metadata."""
    normalized = validate_model_action(action)
    return sha256_digest(normalized)


def admit_action(action: Mapping[str, Any], context: V8RequestContext) -> dict[str, Any]:
    normalized = validate_model_action(action)
    return {
        **context.to_dict(),
        "tool": normalized["tool"],
        "action_id": normalized["action_id"],
        "action": normalized,
        "action_digest": action_digest(normalized),
        "status": "ADMITTED",
    }


def validate_model_commit(commit: Mapping[str, Any], action_count: int) -> dict[str, Any]:
    """Validate the intentionally small WebGPT-facing v8 commit envelope."""
    if not isinstance(commit, Mapping):
        raise ProtocolV8Error("COMMIT_NOT_OBJECT")
    if commit.get("tool") != "turn_commit":
        raise ProtocolV8Error("COMMIT_TOOL_INVALID")
    unknown = set(commit) - MODEL_COMMIT_FIELDS
    if unknown:
        raise ProtocolV8Error("V7_OR_RUNTIME_COMMIT_FIELDS_FORBIDDEN", ",".join(sorted(unknown)))
    if type(commit.get("action_count")) is not int or commit["action_count"] < 0:
        raise ProtocolV8Error("COMMIT_ACTION_COUNT_INVALID")
    if commit["action_count"] != action_count:
        raise ProtocolV8Error(
            "COMMIT_ACTION_COUNT_MISMATCH",
            f"expected={action_count};actual={commit['action_count']}",
        )
    return {"tool": "turn_commit", "action_count": commit["action_count"]}


def _extract_dom_tool_payloads(source: str) -> tuple[list[str], bool]:
    """Extract the form exposed by browser ``inner_text()``.

    Markdown code fences are syntax, not DOM text.  ChatGPT therefore exposes
    a rendered block as a ``smartagent_tool`` language label followed by the
    JSON payload.  Keep this adapter deliberately narrow: headers must occupy
    their own lines and no prose may appear before the first header.
    """
    lines = source.splitlines()
    header_re = re.compile(r"^[ \t]*smartagent_tool(?:[ \t]+[^\r\n`]*)?[ \t]*$", re.IGNORECASE)
    header_indexes = [index for index, line in enumerate(lines) if header_re.fullmatch(line)]
    if not header_indexes:
        return [], False
    prefix = "\n".join(lines[:header_indexes[0]]).strip()
    payloads: list[str] = []
    for position, header_index in enumerate(header_indexes):
        next_index = header_indexes[position + 1] if position + 1 < len(header_indexes) else len(lines)
        payloads.append("\n".join(lines[header_index + 1:next_index]).strip())
    return payloads, not bool(prefix)


def _reject_transport_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolV8Error("DUPLICATE_JSON_KEY", key)
        value[key] = item
    return value


def _strip_terminal_fence_residue(value: str) -> tuple[str, bool]:
    """Remove one standalone terminal Markdown fence from rendered DOM text."""
    match = re.search(r"(?:^|\r?\n)[ \t]*```[ \t]*\Z", str(value or ""))
    if not match:
        return str(value or ""), False
    return str(value or "")[:match.start()].rstrip(), True


def _exact_bare_commit(value: str) -> dict[str, Any] | None:
    """Return one exact trailing commit object; never recover an action."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_reject_transport_duplicate_keys)
    try:
        decoded, end = decoder.raw_decode(candidate)
    except (json.JSONDecodeError, ProtocolV8Error):
        return None
    if candidate[end:].strip() or not isinstance(decoded, dict):
        return None
    return decoded if decoded.get("tool") == "turn_commit" else None


def parse_v8_tool_transport_detailed(
    text: str,
    *,
    missing_action_id_factory: Callable[[dict[str, Any], int], str] | None = None,
) -> TransportParseResult:
    """Parse compact v8 transport and report bounded lossless normalizations."""
    source = str(text or "").strip()
    normalizations: list[str] = []
    block_map: list[dict[str, Any]] = []

    def failed(reason: str, detail: str, *, kind: str) -> TransportParseResult:
        return TransportParseResult([], [{"reason": reason, "detail": detail}], normalizations, kind, block_map)

    if not source:
        return failed("empty_response", "response is empty", kind="empty")

    opening = re.compile(r"```smartagent_tool(?:[ \t]+[^\r\n`]*)?[ \t]*\r?\n", re.IGNORECASE)
    closing = re.compile(r"\r?\n?[ \t]*```")
    payloads: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    transport_kind = "fenced"
    while cursor < len(source):
        match = opening.search(source, cursor)
        if not match:
            break
        end = closing.search(source, match.end())
        if not end:
            return failed("malformed_transport", "unclosed smartagent_tool fence", kind=transport_kind)
        payloads.append(source[match.end():end.start()].strip())
        spans.append((match.start(), end.end()))
        cursor = end.end()

    if payloads:
        previous = 0
        for start, end in spans:
            if source[previous:start].strip():
                return failed("transport_not_exclusive", "content outside v8 blocks", kind=transport_kind)
            previous = end
        suffix = source[previous:].strip()
        if suffix:
            suffix, fence_removed = _strip_terminal_fence_residue(suffix)
            if fence_removed:
                normalizations.append("TERMINAL_FENCE_RESIDUE_REMOVED")
            bare_commit = _exact_bare_commit(suffix)
            if bare_commit is None:
                if not suffix and fence_removed:
                    pass
                else:
                    return failed("transport_not_exclusive", "content outside v8 blocks", kind=transport_kind)
            else:
                payloads.append(suffix)
                normalizations.append("TRAILING_BARE_TURN_COMMIT_ADOPTED")
    else:
        transport_kind = "dom_rendered"
        payloads, dom_exclusive = _extract_dom_tool_payloads(source)
        if not payloads:
            return failed(
                "missing_smartagent_tool_envelope",
                "no v8 fenced or DOM block",
                kind=transport_kind,
            )
        if not dom_exclusive:
            return failed("transport_not_exclusive", "content outside DOM v8 blocks", kind=transport_kind)
        payloads[-1], fence_removed = _strip_terminal_fence_residue(payloads[-1])
        if fence_removed:
            normalizations.append("TERMINAL_FENCE_RESIDUE_REMOVED")

    # Models occasionally render multiple otherwise valid JSON objects inside
    # one block. Treat a block as a strict JSON sequence. raw_decode still
    # rejects prose, malformed escapes, duplicate keys, arrays, and schema
    # errors; normalizing transport never grants action authority.
    decoder = json.JSONDecoder(object_pairs_hook=_reject_transport_duplicate_keys)
    calls: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads, 1):
        cursor = 0
        decoded_count = 0
        tools: list[str] = []
        while cursor < len(payload):
            whitespace = re.match(r"\s*", payload[cursor:])
            cursor += len(whitespace.group(0)) if whitespace else 0
            if cursor >= len(payload):
                break
            try:
                decoded, end = decoder.raw_decode(payload, cursor)
            except (json.JSONDecodeError, ProtocolV8Error) as exc:
                code = exc.code if isinstance(exc, ProtocolV8Error) else "JSON_DECODE_ERROR"
                detail = exc.detail if isinstance(exc, ProtocolV8Error) else str(exc)
                block_map.append({"block": index, "object_count": decoded_count, "tools": tools})
                return failed(code, f"block={index};{detail}", kind=transport_kind)
            if not isinstance(decoded, dict):
                block_map.append({"block": index, "object_count": decoded_count, "tools": tools})
                return failed(
                    "ENVELOPE_NOT_OBJECT",
                    f"block={index};object={decoded_count + 1}",
                    kind=transport_kind,
                )
            calls.append(decoded)
            tools.append(str(decoded.get("tool", "")))
            decoded_count += 1
            cursor = end
        block_map.append({"block": index, "object_count": decoded_count, "tools": tools})
        if decoded_count == 0:
            return failed("EMPTY_TOOL_BLOCK", f"block={index}", kind=transport_kind)
        if transport_kind == "dom_rendered" and decoded_count > 1:
            normalizations.append("DOM_JSON_SEQUENCE_ACCEPTED")

    if not calls or calls[-1].get("tool") != "turn_commit":
        return failed("missing_turn_commit", "last block is not turn_commit", kind=transport_kind)
    for index, action in enumerate(calls[:-1], 1):
        if "action_id" not in action and missing_action_id_factory is not None:
            generated_action_id = str(missing_action_id_factory(action, index) or "").strip()
            if not generated_action_id:
                return failed(
                    "MISSING_OR_INVALID_FIELD",
                    f"action_index={index};tool={action.get('tool', '(missing)')};action_id",
                    kind=transport_kind,
                )
            action["action_id"] = generated_action_id
            normalizations.append(
                f"RUNTIME_ACTION_ID_ASSIGNED:index={index};tool={action.get('tool', '')}"
            )
        try:
            # Keep the raw action available for runtime FIELD_REPAIR. The
            # admission layer performs the full tool-specific required-field
            # check; transport parsing only checks the envelope shape.
            validate_model_action(
                action,
                allow_missing_fields=TOOL_REQUIRED_FIELDS.get(str(action.get("tool", "")), set()),
            )
        except ProtocolV8Error as exc:
            # ``index`` is the action ordinal in the decoded transport, not the
            # Markdown/DOM block number.  Name it explicitly so recovery logs
            # identify the malformed tool instead of misleading operators with
            # messages such as ``block=1;action_id`` when one DOM block contains
            # several JSON objects.
            tool = str(action.get("tool", "") or "(missing)")
            return failed(
                exc.code,
                f"action_index={index};tool={tool};{exc.detail}",
                kind=transport_kind,
            )
    try:
        validate_model_commit(calls[-1], len(calls) - 1)
    except ProtocolV8Error as exc:
        return failed(exc.code, exc.detail, kind=transport_kind)
    return TransportParseResult(calls, [], normalizations, transport_kind, block_map)


def parse_v8_tool_transport(text: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Compatibility wrapper for compact v8 fenced or rendered transport."""
    result = parse_v8_tool_transport_detailed(text)
    return result.calls, result.diagnostics


def build_result(
    admitted_action: Mapping[str, Any],
    result_id: str,
    status: str,
    result: Any,
) -> dict[str, Any]:
    for field in ("request_id", "action_id", "action_digest"):
        if field not in admitted_action:
            raise ProtocolV8Error("ADMITTED_ACTION_FIELD_MISSING", field)
    _require_non_empty_string({"result_id": result_id}, "result_id")
    _require_non_empty_string({"status": status}, "status")
    return {
        "request_id": admitted_action["request_id"],
        "task_id": admitted_action["task_id"],
        "task_epoch": admitted_action["task_epoch"],
        "intent_digest": admitted_action["intent_digest"],
        "action_id": admitted_action["action_id"],
        "action_digest": admitted_action["action_digest"],
        "result_id": result_id,
        "result_digest": sha256_digest(result),
        "status": status,
    }


def validate_result_ack(
    context: V8RequestContext,
    result: Mapping[str, Any],
    ack_result_id: str,
) -> None:
    if not isinstance(result, Mapping):
        raise ProtocolV8Error("RESULT_NOT_OBJECT")
    if not context.matches(result):
        raise ProtocolV8Error("RESULT_REQUEST_SCOPE_MISMATCH")
    if result.get("result_id") != ack_result_id:
        raise ProtocolV8Error("RESULT_ACK_MISMATCH")
    if not isinstance(result.get("action_digest"), str) or not result["action_digest"]:
        raise ProtocolV8Error("RESULT_ACTION_DIGEST_MISSING")


def validate_field_repair(
    original: Mapping[str, Any],
    repaired: Mapping[str, Any],
    missing_fields: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Allow only the explicitly requested fields to be added."""
    original_normalized = validate_model_action(original, allow_missing_fields=missing_fields)
    repaired_normalized = validate_model_action(repaired)
    for field, value in original_normalized.items():
        if repaired_normalized.get(field) != value:
            raise ProtocolV8Error("ACTION_REPAIR_SCOPE_VIOLATION", field)
    added = set(repaired_normalized) - set(original_normalized)
    expected = set(missing_fields)
    if added != expected:
        raise ProtocolV8Error(
            "ACTION_REPAIR_FIELDS_MISMATCH",
            f"expected={sorted(expected)};actual={sorted(added)}",
        )
    return repaired_normalized


def next_attachment_state(current: str, target: str) -> str:
    if current not in ATTACHMENT_STATES:
        raise ProtocolV8Error("ATTACHMENT_STATE_INVALID", current)
    if target not in ATTACHMENT_STATES:
        raise ProtocolV8Error("ATTACHMENT_STATE_INVALID", target)
    if target not in _ATTACHMENT_TRANSITIONS[current]:
        raise ProtocolV8Error("ATTACHMENT_TRANSITION_INVALID", f"{current}->{target}")
    return target


__all__ = [
    "ATTACHMENT_NOT_STABLE", "ATTACHMENT_STATES", "MODEL_ACTION_FIELDS",
    "MODEL_COMMIT_FIELDS", "PROTOCOL_FAMILY", "PROTOCOL_NAME", "PROTOCOL_VERSION",
    "RECONCILE_REQUIRED", "REPAIR_FIELD", "REPLAN_ACTION", "ProtocolV8Error",
    "TransportParseResult",
    "RUNTIME_OWNED_FIELDS", "TOOL_REQUIRED_FIELDS", "PROJECT_SYNC_STRATEGIES", "V8RequestContext", "action_digest", "admit_action",
    "build_result", "canonical_json", "next_attachment_state", "parse_v8_tool_transport",
    "parse_v8_tool_transport_detailed", "session_identity",
    "sha256_digest", "validate_field_repair", "validate_model_action",
    "validate_model_commit", "validate_result_ack", "validate_session_identity",
]
