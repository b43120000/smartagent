#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared SmartAgent Tool Envelope protocol.

Single source of truth for schema, parser, transport guards, ACK validation,
and the canonical SmartAgent planner protocol prompt.
"""

# v8 replacement: the legacy text above is retained only as source history;
# every active caller resolves this final template assignment.
SYSTEM_PROMPT_TEMPLATE = """
SmartAgent Tool Protocol v8 only.
You are the decision planner. Formal communication uses exclusive fenced
smartagent_tool blocks. Never emit prose outside those blocks.

Model-owned decision fields:
- Every action/final_response has a unique action_id.
- Each tool includes only its required decision fields.
- The final block is {\"tool\":\"turn_commit\",\"action_count\":N}.

Runtime-owned fields: request_id, task_id, task_epoch, intent_digest,
action_digest, result_id, result_digest, attachment_id, turn_id, nonce,
protocol_version and ACK metadata. Do not emit them.

Rules:
- Do not execute or claim an action without a valid compact turn_commit.
- final_response is the only terminal action and cannot share a round with another action.
- FIELD_REPAIR is only for a missing field when every existing field is schema-valid.
- An unexpected or misplaced field requires ACTION_REPLAN with a fresh action_id
  and a complete canonical action; never preserve an invalid field.
- ACTION_REPLAN uses a fresh action_id when the original decision is rejected.
- If result or process state is unknown, stop and request reconcile; never replay a mutation blindly.
- Attachments must be stable before submission; never reuse an attachment from another request.
- Do not include source code, patches, or large content inline; use the declared file/artifact tools.

Available tools and required decision fields are defined by the current tool schema.
"""
import json
import re

from .command_security import inspect_command
from .command_operation import operation_mismatch_detail
from .payload_budget import PROTOCOL_RESPONSE_MAX_BYTES, utf8_size
from .protocol_v9 import SINGLE_FENCE_TRANSPORT_CONTRACT


PROJECT_EVIDENCE_ACTION_CONTRACT = """
[SMARTAGENT_PROJECT_EVIDENCE_ACTION_CONTRACT]
Project source access must use explicit structured query_project operations; every queries[] item is a JSON object with an operation. Natural-language string shorthand is forbidden for Web Planner actions.
Operation selection is deterministic:
- Unknown path, known keyword/name only -> search_text.
- Known symbol -> read_symbol, with path when known.
- Known workspace-relative file path -> read_range; do not use search_text to ask for that file's content.
- When read_range returns truncated=true and next_cursor, continue the same path from next_cursor until truncated=false.
Before modifying an existing source/text file, obtain snapshot-bound read_range/read_symbol evidence and file_sha256. Do not use run_command, read_file, or attachments as a shortcut for project source evidence.
Edit-plan requirements:
- exact_replace requires path, base_sha256, modification_intent, mode="exact_replace", exact old, and new.
- whole_file is only for bounded small files and requires path, base_sha256, modification_intent, mode="whole_file", and complete content.
- Evidence must be complete before validate_edit_plan/apply_edit_plan. If Runtime supplies a prerequisite route, emit that exact structured action before retrying apply_edit_plan.
[/SMARTAGENT_PROJECT_EVIDENCE_ACTION_CONTRACT]
""".strip()

class DuplicateJSONKeyError(ValueError):
    """Raised when one JSON object contains the same key more than once."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJSONKeyError(key)
        value[key] = item
    return value


def _strict_json_loads(candidate: str) -> object:
    """Decode JSON without guessing missing syntax or silently replacing keys."""
    return json.loads(candidate, object_pairs_hook=_reject_duplicate_json_keys)


TOOL_FENCE = "smartagent_tool"

# SmartAgent Tool Envelope is intentionally a small control-plane protocol.
# Large source/patch/script bodies must travel as files/artifacts instead.
TOOL_ENVELOPE_MAX_BYTES = 8 * 1024
FINAL_RESPONSE_MAX_BYTES = 64 * 1024
RUN_COMMAND_MAX_CHARS = 1200
WRITE_FILE_CONTENT_MAX_CHARS = 4096
INLINE_SCRIPT_SOFT_CHARS = 220
INLINE_SCRIPT_HARD_CHARS = 420
INLINE_SCRIPT_RISK_THRESHOLD = 4

# Data-driven inline interpreter definitions.  These are not language bans:
# short probes remain allowed, while source/script payloads are rejected.
INLINE_EXECUTOR_SPECS = (
    ("python", r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", r"-c"),
    ("javascript", r"(?:node|deno|bun)(?:\.exe)?", r"(?:-e|--eval)"),
    ("ruby", r"ruby(?:\.exe)?", r"-e"),
    ("perl", r"perl(?:\.exe)?", r"-e"),
    ("php", r"php(?:\.exe)?", r"-r"),
    ("lua", r"lua(?:\d+(?:\.\d+)*)?(?:\.exe)?", r"-e"),
    ("r", r"(?:R|Rscript)(?:\.exe)?", r"-e"),
    ("julia", r"julia(?:\.exe)?", r"-e"),
    ("groovy", r"groovy(?:\.exe)?", r"-e"),
    ("posix_shell", r"(?:bash|sh|zsh|dash|ksh)(?:\.exe)?", r"-c"),
    ("powershell", r"(?:powershell|pwsh)(?:\.exe)?", r"(?:-c|-command)"),
    ("cmd", r"cmd(?:\.exe)?", r"/c"),
)


TOOL_ENVELOPE_SCHEMAS = {
    "run_command": {
        "required": {"command": str, "operation": str},
        "optional": {
            "timeout": int,
            "verify": (list, dict),
            "success_criteria": str,
            "result_transport": str,
            "full_result_required": bool,
            "result_purpose": str,
            "verifies_action_id": str,
            "condition_id": str,
            "expected_failure": dict,
        },
    },
    "read_file": {
        "required": {"path": str},
        "optional": {},
    },
    "write_file": {
        "required": {"path": str, "content": str},
        "optional": {},
    },
    "delete_path": {
        "required": {"path": str, "recursive": bool, "reason": str},
        "optional": {},
    },
    "begin_file_write": {
        "required": {
            "action_id": str, "write_id": str, "path": str, "encoding": str,
            "overwrite": bool,
        },
        "optional": {
            "expected_destination_sha256": str, "expected_size": int,
            "expected_sha256": str, "chunk_count": int,
        },
    },
    "write_file_chunk": {
        "required": {
            "action_id": str, "write_id": str, "content": str,
        },
        "optional": {
            "chunk_index": int, "offset": int, "content_encoding": str,
            "chunk_size": int, "chunk_sha256": str,
        },
    },
    "commit_file_write": {
        "required": {"action_id": str, "write_id": str},
        "optional": {"expected_size": int, "expected_sha256": str},
    },
    "abort_file_write": {
        "required": {"action_id": str, "write_id": str},
        "optional": {},
    },
    "list_directory": {
        "required": {},
        "optional": {"path": str},
    },
    "inspect_directory": {
        "required": {"paths": list},
        "optional": {"recursive": bool, "sample_limit": int},
    },
    "inspect_project_scope": {
        "required": {},
        "optional": {"workspace": str},
    },
    "inspect_project_working_set": {
        "required": {"paths": list},
        "optional": {"workspace": str},
    },
    "inspect_semantic_map": {
        "required": {},
        "optional": {"workspace": str, "paths": list},
    },
    "update_semantic_map": {
        "required": {"patch": dict},
        "optional": {"workspace": str},
    },
    "update_semantic_map_file": {
        "required": {"path": str},
        "optional": {"workspace": str, "expected_sha256": str},
    },
    "compare_project_snapshot": {
        "required": {"known_snapshot": dict},
        "optional": {"workspace": str},
    },
    "extract_project_dependencies": {
        "required": {},
        "optional": {"workspace": str},
    },
    "build_project_bundle": {
        "required": {},
        "optional": {"workspace": str, "output_dir": str, "max_bytes": int, "max_files": int},
    },
    "build_project_delta": {
        "required": {"base_snapshot_id": str},
        "optional": {"workspace": str},
    },
    "project_sync": {
        "required": {"strategy": str},
        "optional": {"project_root": str, "workspace": str, "path": str, "base_snapshot_id": str, "max_bytes": int, "max_files": int},
    },
    "query_project": {
        "required": {"project_root": str, "queries": list},
        "optional": {"workspace": str, "snapshot_id": str, "project_handle": str, "max_bytes": int},
    },
    "inspect_project_ledger": {
        "required": {},
        "optional": {"workspace": str, "limit": int},
    },
    "query_project_history": {
        "required": {},
        "optional": {
            "workspace": str,
            "event_types": list,
            "snapshot_id": str,
            "path_contains": str,
            "since": (int, float),
            "limit": int,
        },
    },
    "validate_edit_plan": {
        "required": {"plan": dict},
        "optional": {"workspace": str},
    },
    "apply_edit_plan": {
        "required": {"plan": dict},
        "optional": {"workspace": str},
    },
    "aggregate_verification": {
        "required": {"commands": list},
        "optional": {
            "workspace": str,
            "timeout": int,
            "verifies_action_id": str,
            "condition_id": str,
        },
    },
    "propose_task_plan": {
        "required": {"plan": dict},
        "optional": {"workspace": str},
    },
    "repair_task_plan": {
        "required": {"plan": dict},
        "optional": {"workspace": str},
    },
    "propose_task_plan_file": {
        "required": {"path": str},
        "optional": {"workspace": str, "expected_sha256": str},
    },
    "execute_frozen_plan": {
        "required": {"plan_id": str},
        "optional": {"workspace": str, "timeout": int},
    },
    "web_search": {
        "required": {"query": str},
        "optional": {},
    },
    "find_file": {
        "required": {"name": str},
        "optional": {"root": str, "max_results": int},
    },
    "upload_file": {
        "required": {"path": str},
        "optional": {},
    },
    "upload_files": {
        "required": {"paths": list},
        "optional": {},
    },
    "return_artifact": {
        "required": {"path": str},
        "optional": {"kind": str, "caption": str},
    },
    "google_drive_upload": {
        "required": {"path": str},
        "optional": {"folder_id": str, "name": str},
    },
    "web_edit_file": {
        "required": {"path": str, "instruction": str},
        "optional": {"output_path": (str, type(None))},
    },
    "download_artifact": {
        "required": {"output_path": str},
        "optional": {"expected_filename": str, "timeout": int},
    },
    "execute_artifact_bundle": {
        "required": {"path": str},
        "optional": {"expected_sha256": str},
    },
    "save_session_summary": {
        "required": {"summary": str},
        "optional": {
            "decisions": list,
            "modified_files": list,
            "verification": list,
            "pending": list,
            "next_steps": list,
        },
    },
    "ask_executor": {
        "required": {
            "instruction": str,
            "allow_local_ai_fallback": bool,
            "run_id": str,
        },
        "optional": {"context": str},
    },
    "report_progress": {
        "required": {
            "current_step": (int, float),
            "total_steps": (int, float),
            "current_focus": str,
        },
        "optional": {
            "base_evaluation": str,
            "steps": list,
            "next_action": str,
            "completion_contract": dict,
            "decision": str,
            "outcome": str,
            "matched_condition": str,
            "matched_condition_id": str,
            "evidence_refs": list,
            "decision_reason": str,
            "runtime_state_ref": str,
            "next_phase": str,
            "selected_action": str,
        },
    },
    "final_response": {
        "required": {"content": str},
        "optional": {},
    },
    "turn_commit": {
        "required": {
            "run_id": str,
            "turn_id": int,
            "ack_local_nonce": str,
            "ack_result_id": str,
            "ack_web_ack_id": str,
            "web_ack_id": str,
            "action_count": int,
        },
        # v6 keeps the ACK envelope wire-compatible and piggybacks its stage
        # manifest here.  The nested manifest is validated atomically after
        # all action envelopes have passed their own schemas.
        "optional": {"stage": dict},
    },
}


def _type_name(spec) -> str:
    if isinstance(spec, tuple):
        return " | ".join(t.__name__ for t in spec)
    return spec.__name__


def _matches_type(value, spec) -> bool:
    """Strict-enough schema type check (bool is not accepted as int)."""
    specs = spec if isinstance(spec, tuple) else (spec,)
    for expected in specs:
        if expected is int:
            if type(value) is int:
                return True
        elif expected is bool:
            if type(value) is bool:
                return True
        elif isinstance(value, expected):
            return True
    return False


def _diagnostic(marker: str, reason: str, *, tool: str = "", detail: str = "",
                suggestion: str = "", block_index: int | None = None, **extra) -> dict:
    result = {
        "marker": marker,
        "reason": reason,
        "tool": tool or "(unknown)",
        "detail": detail,
        "suggestion": suggestion,
    }
    if block_index is not None:
        result["block_index"] = block_index
    result.update(extra)
    return result


def _guess_tool_name(candidate: str) -> str:
    match = re.search(r'["\']tool["\']\s*:\s*["\']([^"\']+)["\']', candidate or "")
    return match.group(1) if match else ""


def _decode_tool_candidate_detailed(candidate: str) -> tuple[object | None, dict | None]:
    """Decode one payload and preserve JSONDecodeError diagnostics.

    Formatting normalization is deliberately limited to transport extraction.
    JSON content is never repaired: missing braces, invalid escapes, and
    duplicate keys require one complete retransmission from the Planner.
    """
    try:
        return _strict_json_loads(candidate), None
    except DuplicateJSONKeyError as error:
        return None, _diagnostic(
            "[TOOL_ENVELOPE_PARSE_ERROR]",
            "duplicate_json_key",
            tool=_guess_tool_name(candidate),
            detail=f"duplicate_key={str(error)}",
            suggestion=(
                "重送同一個完整 smartagent_tool envelope；每個 JSON object 的欄位名稱只能出現一次。"
            ),
        )
    except json.JSONDecodeError as original_error:
        return None, _diagnostic(
            "[TOOL_ENVELOPE_PARSE_ERROR]",
            "json_decode_error",
            tool=_guess_tool_name(candidate),
            detail=original_error.msg,
            suggestion=(
                "重送同一個 smartagent_tool envelope，只修正 JSON transport syntax；"
                "Windows 路徑優先使用 C:/...。"
            ),
            line=original_error.lineno,
            column=original_error.colno,
            position=original_error.pos,
        )
    except Exception as error:
        return None, _diagnostic(
            "[TOOL_ENVELOPE_PARSE_ERROR]",
            "json_decode_exception",
            tool=_guess_tool_name(candidate),
            detail=f"{type(error).__name__}: {error}",
            suggestion="重送同一個 smartagent_tool envelope，只修正 transport syntax。",
        )


def _decode_tool_candidate(candidate: str):
    """Compatibility wrapper used by older callers/tests."""
    value, _ = _decode_tool_candidate_detailed(candidate)
    return value


def _extract_inline_executor(command: str) -> tuple[str | None, str]:
    """Return (family, inline_code) for known interpreter/shell inline execution.

    Detection is intentionally data-driven so adding another interpreter is a
    table change rather than another validator branch.  A match alone is not a
    rejection: the payload is scored separately so short diagnostic probes stay
    usable as control-plane commands.
    """
    for family, exe_pattern, flag_pattern in INLINE_EXECUTOR_SPECS:
        executable = rf'(?:"[^"\r\n]*{exe_pattern}"|(?:[^\s"\']*[\\/])?{exe_pattern})'
        pattern = re.compile(
            rf'(?is)(?:^|[\s;&|]){executable}\b'
            rf'(?P<options>[^\r\n]{{0,180}}?)'
            rf'(?P<flag>(?<!\S){flag_pattern})(?:\s+|=)(?P<code>.+)$'
        )
        match = pattern.search(command)
        if match:
            return family, match.group("code").strip()
    return None, ""


def _command_complexity_signals(command: str, inline_code: str = "") -> tuple[int, list[str]]:
    """Score script-like structure using multiple independent syntax signals."""
    target = inline_code or command
    lowered = target.lower()
    signals: list[str] = []
    score = 0

    length = len(target)
    if length > INLINE_SCRIPT_HARD_CHARS:
        score += 5
        signals.append(f"payload_chars>{INLINE_SCRIPT_HARD_CHARS}")
    elif length > INLINE_SCRIPT_SOFT_CHARS:
        score += 3
        signals.append(f"payload_chars>{INLINE_SCRIPT_SOFT_CHARS}")
    elif inline_code and length > 120:
        score += 1
        signals.append("payload_chars>120")

    semicolons = target.count(";")
    if semicolons >= 4:
        score += 4
        signals.append(f"semicolons={semicolons}")
    elif semicolons >= 2:
        score += 2
        signals.append(f"semicolons={semicolons}")
    elif semicolons == 1:
        score += 1
        signals.append("semicolon")

    chain_ops = re.findall(r'&&|\|\||(?<!\|)\|(?!\|)', target)
    if len(chain_ops) >= 4:
        score += 4
        signals.append(f"chain_ops={len(chain_ops)}")
    elif len(chain_ops) >= 2:
        score += 2
        signals.append(f"chain_ops={len(chain_ops)}")
    elif len(chain_ops) == 1:
        score += 1
        signals.append("chain_op")

    redirections = re.findall(r'(?<![<>=])(?:>>|>|<)(?![<>=])', target)
    if len(redirections) >= 3:
        score += 3
        signals.append(f"redirections={len(redirections)}")
    elif len(redirections) >= 2:
        score += 2
        signals.append(f"redirections={len(redirections)}")

    if re.search(r'(?i)\b(?:import|include|require|use|source|load)\b', target):
        score += 2
        signals.append("module_load")

    if re.search(
        r'(?i)(?:\b(?:class|def|function|lambda|for|foreach|while|switch|case|try|catch)\b'
        r'|\bif\s*\(|\bif\s+[^\s]|\bdo\b|\bdone\b|\bthen\b|\bfi\b|\bend\b)',
        target,
    ):
        score += 2
        signals.append("control_or_definition")

    if re.search(
        r'(?i)\b(?:eval|exec|compile|invoke-expression|iex|add-type|scriptblock)\b'
        r'|frombase64string\s*\(|child_process|subprocess|os\.system\s*\(|process\.start\s*\(',
        target,
    ):
        score += 3
        signals.append("dynamic_execution")

    assignments = re.findall(r'(?<![=!<>])(?:\$?[A-Za-z_]\w*|\b(?:const|let|var)\s+[A-Za-z_]\w*)\s*=(?!=)', target)
    if len(assignments) >= 3:
        score += 2
        signals.append(f"assignments={len(assignments)}")
    elif len(assignments) >= 1:
        score += 1
        signals.append(f"assignments={len(assignments)}")

    block_tokens = target.count("{") + target.count("}")
    if block_tokens >= 4:
        score += 1
        signals.append(f"block_tokens={block_tokens}")

    # A long opaque token in inline code is commonly an encoded/script payload.
    opaque_tokens = re.findall(r'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{180,}={0,2}(?![A-Za-z0-9+/=])', target)
    if opaque_tokens:
        score += 4
        signals.append("opaque_payload")

    return score, signals


def _validate_run_command_complexity(command: str, *, block_index: int | None = None,
                                     field_path: str = "command") -> tuple[bool, dict | None]:
    """Enforce the Tool Envelope control-plane boundary for any run command."""
    if len(command) > RUN_COMMAND_MAX_CHARS:
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "run_command_too_long",
            tool="run_command",
            detail=f"field={field_path} chars={len(command)} limit={RUN_COMMAND_MAX_CHARS}",
            suggestion=(
                "run_command 只保留短 control-plane 指令；大型 patch/source/script "
                "改走附件、artifact 或既有檔案後再執行短命令。"
            ),
            block_index=block_index,
        )

    if "\n" in command or "\r" in command:
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "multiline_run_command",
            tool="run_command",
            detail=f"field={field_path}; multiline shell/script content is not allowed in Tool Envelope",
            suggestion="將多行 script 放到檔案/artifact；run_command 只執行該檔案的短命令。",
            block_index=block_index,
        )

    if ("@'" in command or '@"' in command or "'@" in command or '"@' in command):
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "powershell_here_string",
            tool="run_command",
            detail=f"field={field_path}; PowerShell here-string token detected",
            suggestion="不要在 JSON 內嵌 here-string；改用附件/artifact。",
            block_index=block_index,
        )

    if re.search(r'(?i)<<-?\s*[\'\"]?[A-Za-z_][A-Za-z0-9_]*[\'\"]?', command):
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "inline_script_too_complex",
            tool="run_command",
            detail=f"field={field_path}; signals=here_doc; chars={len(command)}",
            suggestion="不要在 command 內嵌 here-doc/source；改走附件、artifact 或既有 script 檔案。",
            block_index=block_index,
        )

    if re.search(
        r'(?i)(?:powershell|pwsh)(?:\.exe)?\b[^\r\n]{0,180}?'
        r'(?:-encodedcommand|-enc)(?:\s+|=)',
        command,
    ):
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "encoded_command_not_allowed",
            tool="run_command",
            detail=f"field={field_path}; encoded PowerShell command payload detected; chars={len(command)}",
            suggestion="不要把 encoded/base64 script 放進 Tool Envelope；改走附件/artifact/既有檔案。",
            block_index=block_index,
        )

    family, inline_code = _extract_inline_executor(command)
    score, signals = _command_complexity_signals(command, inline_code)

    # Known inline executors need multiple script-like signals.  For ordinary
    # commands, only very strong aggregate shell structure is rejected.
    reject = False
    if family:
        reject = score >= INLINE_SCRIPT_RISK_THRESHOLD
    else:
        strong_shell_structure = (
            command.count(";") >= 4
            or len(re.findall(r'&&|\|\||(?<!\|)\|(?!\|)', command)) >= 5
            or len(re.findall(r'(?<![<>=])(?:>>|>|<)(?![<>=])', command)) >= 4
        )
        reject = strong_shell_structure and score >= INLINE_SCRIPT_RISK_THRESHOLD

    if reject:
        signal_text = ",".join(signals) if signals else "aggregate_complexity"
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "inline_script_too_complex",
            tool="run_command",
            detail=(
                f"field={field_path}; family={family or 'shell'}; score={score}; "
                f"signals={signal_text}; chars={len(command)}; inline_chars={len(inline_code)}"
            ),
            suggestion=(
                "一個 run_command 只承載一個主要操作；移除變數、if/foreach 與多命令串接，"
                "把有先後相依的操作拆成後續 action。只有 source/script/payload 才改走附件、"
                "artifact 或既有檔案。"
            ),
            block_index=block_index,
        )

    return True, None


def validate_tool_envelope(call: object, raw_payload: str = "",
                           block_index: int | None = None) -> tuple[bool, dict | None]:
    """Validate schema and control-plane complexity before any execution."""
    raw_tool = call.get("tool") if isinstance(call, dict) else _guess_tool_name(raw_payload)
    raw_limit = FINAL_RESPONSE_MAX_BYTES if raw_tool == "final_response" else TOOL_ENVELOPE_MAX_BYTES
    if raw_payload and len(raw_payload.encode("utf-8")) > raw_limit:
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "envelope_too_large",
            tool=str(raw_tool or ""),
            detail=(
                f"payload_bytes={len(raw_payload.encode('utf-8'))} "
                f"limit={raw_limit}"
            ),
            suggestion=(
                "final_response 只承載一般使用者可讀回覆；大型 source/patch/file content 仍改走附件或 artifact。"
                if raw_tool == "final_response"
                else "保持 Tool Envelope 短小；大型 source/patch/content 改走附件或 artifact。"
            ),
            block_index=block_index,
        )

    if not isinstance(call, dict):
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "envelope_not_object",
            detail=f"decoded_type={type(call).__name__}",
            suggestion="smartagent_tool payload 必須是一個 JSON object。",
            block_index=block_index,
        )

    tool = call.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "missing_or_invalid_tool",
            detail="field 'tool' must be a non-empty string",
            suggestion="保留原本決策，使用已支援的 tool 名稱重送 envelope。",
            block_index=block_index,
        )

    schema = TOOL_ENVELOPE_SCHEMAS.get(tool)
    if schema is None:
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "unknown_tool",
            tool=tool,
            detail=f"supported_tools={', '.join(sorted(TOOL_ENVELOPE_SCHEMAS))}",
            suggestion="使用 SmartAgent 已支援的 tool 名稱，不要讓 Local Agent 猜測工具。",
            block_index=block_index,
        )

    allowed_fields = {"tool", "action_id", *schema["required"], *schema["optional"]}
    unexpected_fields = sorted(str(field) for field in set(call) - allowed_fields)
    if unexpected_fields:
        if tool == "inspect_project_scope":
            suggestion = (
                "inspect_project_scope 只接受 workspace；若需求只是列出目錄第一層，"
                "請改用 list_directory 並把目標放在 path。"
            )
        elif tool == "query_project":
            suggestion = (
                "這不是 FIELD_REPAIR。使用新的 action_id 重建 canonical query_project；"
                "operation/path/symbol 等查詢欄位只能放在 queries[] 的物件內。"
            )
        elif tool in {"validate_edit_plan", "apply_edit_plan"}:
            suggestion = (
                "這不是 FIELD_REPAIR。使用新的 action_id 重建 canonical action；最外層只放 "
                "tool、action_id、可選 workspace 與 plan。base_snapshot_id、files_to_modify、"
                "verification_commands、expected_observable_result、rollback_condition 全部放在 plan 內。"
            )
        else:
            suggestion = (
                "這不是 FIELD_REPAIR。使用新的 action_id，僅依目前 tool schema 的"
                " required/optional 欄位重建完整 canonical action。"
            )
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "unexpected_field",
            tool=tool,
            detail=f"fields={','.join(unexpected_fields)}",
            suggestion=suggestion,
            block_index=block_index,
        )

    for field, expected_type in schema["required"].items():
        if field not in call:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "missing_required_field",
                tool=tool,
                detail=f"field={field}",
                suggestion=f"重送同一個 {tool} 決策並補上必要欄位 '{field}'。",
                block_index=block_index,
            )
        if not _matches_type(call[field], expected_type):
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "wrong_field_type",
                tool=tool,
                detail=(
                    f"field={field} expected={_type_name(expected_type)} "
                    f"actual={type(call[field]).__name__}"
                ),
                suggestion=f"重送同一個 {tool} 決策並修正欄位 '{field}' 的 JSON type。",
                block_index=block_index,
            )

    for field, expected_type in schema["optional"].items():
        if field in call and not _matches_type(call[field], expected_type):
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "wrong_field_type",
                tool=tool,
                detail=(
                    f"field={field} expected={_type_name(expected_type)} "
                    f"actual={type(call[field]).__name__}"
                ),
                suggestion=f"重送同一個 {tool} 決策並修正欄位 '{field}' 的 JSON type。",
                block_index=block_index,
            )

    if tool == "query_project":
        queries = list(call.get("queries") or [])
        invalid_indexes = [
            index
            for index, query in enumerate(queries, 1)
            if not isinstance(query, dict)
            or not isinstance(query.get("operation"), str)
            or not str(query.get("operation", "") or "").strip()
        ]
        if not queries or invalid_indexes:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "query_project_structured_queries_required",
                tool=tool,
                detail=(
                    "queries[] must contain explicit operation objects; invalid_indexes="
                    + json.dumps(invalid_indexes, separators=(",", ":"))
                ),
                suggestion=PROJECT_EVIDENCE_ACTION_CONTRACT,
                block_index=block_index,
            )

    if tool == "project_sync":
        strategy = str(call.get("strategy", "") or "").strip().upper()
        allowed = {"INDEX_ONLY", "DELTA", "FULL_BUNDLE"}
        if strategy not in allowed:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "project_sync_strategy_invalid",
                tool=tool,
                detail=(
                    f"actual={strategy or '(empty)'};"
                    f"allowed={','.join(sorted(allowed))}"
                ),
                suggestion=(
                    "DIRECT 是 context access mode，不是 project_sync strategy。"
                    "若要直接讀取請改用 read_file/list_directory；若要建立索引請使用 INDEX_ONLY。"
                ),
                block_index=block_index,
            )

    if tool != "turn_commit" and "action_id" in call and not isinstance(call.get("action_id"), str):
        return False, _diagnostic(
            "[TOOL_ENVELOPE_REJECTED]",
            "wrong_field_type",
            tool=tool,
            detail="field=action_id expected=str",
            suggestion="ACK protocol 中 action_id 必須是非空字串。",
            block_index=block_index,
        )

    if tool == "upload_files":
        paths = call.get("paths", [])
        if not all(isinstance(item, str) for item in paths):
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "wrong_field_type",
                tool=tool,
                detail="field=paths expected=list[str]",
                suggestion="upload_files.paths 必須只包含字串路徑。",
                block_index=block_index,
            )

    if tool == "inspect_directory":
        paths = call.get("paths", [])
        if not paths or not all(isinstance(item, str) and item.strip() for item in paths):
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "wrong_field_type",
                tool=tool,
                detail="field=paths expected=non-empty list[str]",
                suggestion="inspect_directory.paths 必須包含至少一個非空路徑字串。",
                block_index=block_index,
            )

    if tool == "run_command":
        command = call.get("command", "")
        valid_command, command_error = _validate_run_command_complexity(
            command,
            block_index=block_index,
            field_path="command",
        )
        if not valid_command:
            return False, command_error
        admission = inspect_command(command)
        if not admission.allowed:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                admission.code,
                tool=tool,
                detail=admission.detail,
                suggestion="不要用 run_command 執行破壞性操作；改用 runtime 提供的 typed operation。",
                block_index=block_index,
            )

        try:
            mismatch = operation_mismatch_detail(call)
        except ValueError as exc:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "run_command_operation_invalid",
                tool=tool,
                detail=str(exc),
                suggestion=(
                    "run_command.operation 必須是 INSPECT、MUTATE、BUILD、TEST、VERIFY、"
                    "GIT_INSPECT、GIT_MUTATE、PROCESS、TRANSFER 或 GENERAL。"
                ),
                block_index=block_index,
            )
        if mismatch:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "run_command_operation_mismatch",
                tool=tool,
                detail=mismatch,
                suggestion="修正 operation 或 command；不得用錯誤 operation 推進 Runtime 狀態。",
                block_index=block_index,
            )

        expected_failure = call.get("expected_failure")
        if expected_failure is not None:
            operation = str(call.get("operation", "") or "").strip().upper()
            if operation not in {"BUILD", "TEST", "INSPECT", "GIT_INSPECT", "VERIFY"}:
                return False, _diagnostic(
                    "[TOOL_ENVELOPE_REJECTED]",
                    "run_command_expected_failure_operation_forbidden",
                    tool=tool,
                    detail=f"operation={operation or 'missing'}",
                    suggestion=(
                        "expected_failure 只允許 BUILD、TEST、INSPECT、GIT_INSPECT、VERIFY；"
                        "不得把 MUTATE、GIT_MUTATE、PROCESS、TRANSFER 或 GENERAL 的失敗包裝成成功。"
                    ),
                    block_index=block_index,
                )
            if not isinstance(expected_failure, dict) or not expected_failure:
                return False, _diagnostic(
                    "[TOOL_ENVELOPE_REJECTED]",
                    "run_command_expected_failure_invalid",
                    tool=tool,
                    detail="expected_failure must be a non-empty object",
                    suggestion=(
                        '例如 "expected_failure":{"exit_codes":[1],'
                        '"stderr_regex":"not a directory"}。'
                    ),
                    block_index=block_index,
                )
            exit_codes = expected_failure.get("exit_codes")
            stderr_regex = expected_failure.get("stderr_regex")
            output_contains = expected_failure.get("output_contains")
            if exit_codes is not None and (
                not isinstance(exit_codes, list)
                or not exit_codes
                or any(not isinstance(item, int) or item == 0 for item in exit_codes)
            ):
                return False, _diagnostic(
                    "[TOOL_ENVELOPE_REJECTED]",
                    "run_command_expected_failure_invalid",
                    tool=tool,
                    detail="expected_failure.exit_codes must be a non-empty list of non-zero integers",
                    suggestion="列出預期的非零 exit code，例如 [1]。",
                    block_index=block_index,
                )
            if stderr_regex is not None:
                if not isinstance(stderr_regex, str) or not stderr_regex.strip():
                    return False, _diagnostic(
                        "[TOOL_ENVELOPE_REJECTED]", "run_command_expected_failure_invalid",
                        tool=tool, detail="expected_failure.stderr_regex must be a non-empty string",
                        suggestion="提供可驗證預期錯誤的 stderr regex。", block_index=block_index,
                    )
                try:
                    re.compile(stderr_regex)
                except re.error as exc:
                    return False, _diagnostic(
                        "[TOOL_ENVELOPE_REJECTED]", "run_command_expected_failure_invalid",
                        tool=tool, detail=f"invalid expected_failure.stderr_regex: {exc}",
                        suggestion="修正 stderr regex。", block_index=block_index,
                    )
            if output_contains is not None:
                values = output_contains if isinstance(output_contains, list) else [output_contains]
                if not values or any(not isinstance(item, str) or not item for item in values):
                    return False, _diagnostic(
                        "[TOOL_ENVELOPE_REJECTED]", "run_command_expected_failure_invalid",
                        tool=tool,
                        detail="expected_failure.output_contains must be a non-empty string or list[str]",
                        suggestion="提供預期出現在 stdout/stderr 的關鍵字。", block_index=block_index,
                    )
            if exit_codes is None and stderr_regex is None and output_contains is None:
                return False, _diagnostic(
                    "[TOOL_ENVELOPE_REJECTED]", "run_command_expected_failure_invalid",
                    tool=tool, detail="expected_failure has no matcher",
                    suggestion="至少提供 exit_codes、stderr_regex 或 output_contains。",
                    block_index=block_index,
                )

        verify = call.get("verify")
        verify_steps = [verify] if isinstance(verify, dict) else (verify if isinstance(verify, list) else [])
        for verify_index, step in enumerate(verify_steps):
            if not isinstance(step, dict) or step.get("action", "run_command") != "run_command":
                continue
            verify_command = step.get("command", "")
            if not isinstance(verify_command, str):
                continue
            valid_verify, verify_error = _validate_run_command_complexity(
                verify_command,
                block_index=block_index,
                field_path=f"verify[{verify_index}].command",
            )
            if not valid_verify:
                return False, verify_error
            verify_admission = inspect_command(verify_command)
            if not verify_admission.allowed:
                return False, _diagnostic(
                    "[TOOL_ENVELOPE_REJECTED]",
                    verify_admission.code,
                    tool=tool,
                    detail=f"verify[{verify_index}]: {verify_admission.detail}",
                    suggestion="verification 不得包含破壞性命令。",
                    block_index=block_index,
                )

    if tool == "write_file":
        content = call.get("content", "")
        if len(content) > WRITE_FILE_CONTENT_MAX_CHARS:
            return False, _diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "write_file_content_too_large",
                tool=tool,
                detail=f"chars={len(content)} limit={WRITE_FILE_CONTENT_MAX_CHARS}",
                suggestion=(
                    "大型全新文字/source 檔請改走 begin_file_write → write_file_chunk × N → "
                    "commit_file_write；既有檔案可用 web_edit_file，已有網頁 artifact 可用 "
                    "download_artifact。不得重送相同 oversized write_file。"
                ),
                block_index=block_index,
            )

    return True, None


def _extract_fenced_tool_payloads(stripped: str) -> tuple[list[str], bool]:
    """Extract fenced SmartAgent payloads and require exclusive transport."""
    opening_re = re.compile(
        r'```smartagent_tool(?:[ \t]+[^\r\n`]*)?[ \t]*\r?\n',
        re.IGNORECASE,
    )
    closing_re = re.compile(r'\r?\n?[ \t]*```')

    payloads: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0

    while True:
        match = opening_re.search(stripped, pos)
        if not match:
            break

        close = closing_re.search(stripped, match.end())
        if not close:
            # Opening a SmartAgent fence without a closing fence is malformed
            # transport. Preserve the candidate only for diagnostics; mark the
            # transport non-exclusive so it can never execute.
            spans.append((match.start(), len(stripped)))
            payloads.append(stripped[match.end():].strip())
            return payloads, False

        payloads.append(stripped[match.end():close.start()].strip())
        spans.append((match.start(), close.end()))
        pos = close.end()

    if not spans:
        return [], False

    residue_parts = []
    cursor = 0
    for block_start, block_end in spans:
        residue_parts.append(stripped[cursor:block_start])
        cursor = block_end
    residue_parts.append(stripped[cursor:])
    residue = "".join(residue_parts).strip()
    return payloads, not bool(residue)


def _extract_dom_tool_payloads(stripped: str) -> tuple[list[str], bool]:
    """Extract browser inner_text form, including pretty-printed JSON payloads."""
    lines = stripped.splitlines()
    header_re = re.compile(
        r'^[ \t]*smartagent_tool(?:[ \t]+[^\r\n`]*)?[ \t]*$',
        re.IGNORECASE,
    )
    header_indexes = [idx for idx, line in enumerate(lines) if header_re.fullmatch(line)]
    if not header_indexes:
        return [], False

    # DOM transport is exclusive only when no prose appears before the first
    # smartagent_tool header.  Everything after a header belongs to that block
    # until the next header, so trailing prose becomes a JSON parse failure.
    prefix = "\n".join(lines[:header_indexes[0]]).strip()
    exclusive = not bool(prefix)

    payloads: list[str] = []
    for pos, header_idx in enumerate(header_indexes):
        next_header = header_indexes[pos + 1] if pos + 1 < len(header_indexes) else len(lines)
        payload = "\n".join(lines[header_idx + 1:next_header]).strip()
        payloads.append(payload)

    return payloads, exclusive


def _extract_tool_transport(text: str) -> tuple[list[str], bool, str]:
    """Return (payloads, exclusive, transport_kind). No prose JSON scanning."""
    stripped = (text or "").strip()
    if not stripped:
        return [], False, "none"

    fenced_payloads, fenced_exclusive = _extract_fenced_tool_payloads(stripped)
    if fenced_payloads:
        return fenced_payloads, fenced_exclusive, "fenced"

    dom_payloads, dom_exclusive = _extract_dom_tool_payloads(stripped)
    if dom_payloads:
        return dom_payloads, dom_exclusive, "dom"

    return [], False, "none"


def _tool_payload_candidates(text: str) -> list[str]:
    """Compatibility helper: payloads only from an exclusive SmartAgent transport."""
    payloads, exclusive, _ = _extract_tool_transport(text)
    return payloads if exclusive else []


def analyze_tool_transport(text: str) -> dict:
    """Deterministically parse, validate, and guard a SmartAgent response.

    The transport is atomic: if any block is malformed/rejected, *no* block from
    that Planner response is returned for execution.  This prevents partial
    execution followed by a syntax/schema retry.
    """
    stripped = (text or "").strip()
    report = {
        "intended": False,
        "transport_kind": "none",
        "normalizations": [],
        "calls": [],
        "diagnostics": [],
    }
    if not stripped:
        return report

    payloads, exclusive, transport_kind = _extract_tool_transport(stripped)
    report["transport_kind"] = transport_kind
    if stripped != (text or ""):
        report["normalizations"].append("trim_outer_whitespace")
    if transport_kind == "dom":
        report["normalizations"].append("interpret_dom_smartagent_headers")

    if payloads:
        report["intended"] = True
        response_bytes = utf8_size(stripped)
        if response_bytes > PROTOCOL_RESPONSE_MAX_BYTES:
            report["diagnostics"].append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "protocol_response_too_large",
                detail=(
                    f"response_bytes={response_bytes} "
                    f"limit={PROTOCOL_RESPONSE_MAX_BYTES}"
                ),
                suggestion=(
                    "不得把大型計畫、semantic map、source、patch 或資料直接放進控制回覆。"
                    "請先在 WebGPT 產生 .json artifact，使用 download_artifact 保存到 workspace，"
                    "下一輪再用 propose_task_plan_file、update_semantic_map_file 或相應 *_file 工具。"
                ),
            ))
            return report
        if not exclusive:
            report["diagnostics"].append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "transport_not_exclusive",
                detail="non-whitespace content exists outside smartagent_tool block(s)",
                suggestion="整個回覆只能包含一個或多個 smartagent_tool block；區塊外不要加說明文字。",
            ))
            return report
    else:
        # Whole-response bare JSON containing "tool" is treated as an intended
        # but malformed transport, not as executable JSON.
        if (
            stripped.startswith("{")
            and stripped.endswith("}")
            and re.search(r'["\']tool["\']\s*:', stripped, re.DOTALL)
        ):
            report["intended"] = True
            report["diagnostics"].append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "missing_smartagent_tool_envelope",
                tool=_guess_tool_name(stripped),
                detail="bare JSON tool calls are not executable",
                suggestion="保留原本 JSON 決策，外層改用 ```smartagent_tool ... ``` 後重送。",
            ))
            return report

        # A damaged/unfinished SmartAgent header still counts as tool intent.
        if re.match(
            r'^(?:```)?smartagent_tool(?:[ \t]+[^\r\n`]*)?(?:\r?\n|$)',
            stripped,
            re.IGNORECASE,
        ):
            report["intended"] = True
            report["diagnostics"].append(_diagnostic(
                "[TOOL_ENVELOPE_PARSE_ERROR]",
                "malformed_smartagent_tool_transport",
                detail="SmartAgent header/fence detected but no complete payload transport was extracted",
                suggestion="重送同一個完整 smartagent_tool envelope，只修正 transport syntax。",
            ))
        return report

    valid_calls = []
    diagnostics = []

    for index, candidate in enumerate(payloads, 1):
        # Guard raw payload before attempting more expensive/ambiguous handling.
        guessed_tool = _guess_tool_name(candidate)
        candidate_limit = FINAL_RESPONSE_MAX_BYTES if guessed_tool == "final_response" else TOOL_ENVELOPE_MAX_BYTES
        if len(candidate.encode("utf-8")) > candidate_limit:
            diagnostics.append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "envelope_too_large",
                tool=guessed_tool,
                detail=(
                    f"payload_bytes={len(candidate.encode('utf-8'))} "
                    f"limit={candidate_limit}"
                ),
                suggestion=(
                    "final_response 只承載一般使用者可讀回覆；大型 source/patch/file content 仍改走附件或 artifact。"
                    if guessed_tool == "final_response"
                    else "保持 Tool Envelope 短小；大型 source/patch/content 改走附件或 artifact。"
                ),
                block_index=index,
            ))
            continue

        decoded, decode_error = _decode_tool_candidate_detailed(candidate)
        if decode_error:
            decode_error["block_index"] = index
            diagnostics.append(decode_error)
            continue

        valid, validation_error = validate_tool_envelope(
            decoded,
            raw_payload=candidate,
            block_index=index,
        )
        if not valid:
            diagnostics.append(validation_error)
            continue

        if decoded in valid_calls:
            diagnostics.append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "duplicate_tool_block",
                tool=str(decoded.get("tool", "")),
                detail="an identical SmartAgent envelope appears more than once",
                suggestion="重送完整回覆，每個 action 與 turn_commit block 只能出現一次。",
                block_index=index,
            ))
            continue
        valid_calls.append(decoded)

    if not diagnostics:
        commits = [call for call in valid_calls if call.get("tool") == "turn_commit"]
        if commits and (len(commits) != 1 or valid_calls[-1].get("tool") != "turn_commit"):
            diagnostics.append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "turn_commit_must_be_last",
                tool="turn_commit",
                detail="exactly one turn_commit is allowed and it must be the final SmartAgent block",
                suggestion="保留原 action，最後只追加一個 turn_commit envelope。",
            ))

    if not diagnostics and any(call.get("tool") == "final_response" for call in valid_calls):
        non_commit = [call for call in valid_calls if call.get("tool") != "turn_commit"]
        if len(non_commit) != 1 or non_commit[0].get("tool") != "final_response":
            diagnostics.append(_diagnostic(
                "[TOOL_ENVELOPE_REJECTED]",
                "final_response_must_be_exclusive",
                tool="final_response",
                detail="final_response may only be paired with the trailing turn_commit",
                suggestion="final_response 前後不得混入其他 action；最後追加 turn_commit 即可。",
            ))

    report["diagnostics"] = diagnostics
    # Atomic transport: any rejected/malformed block blocks the entire batch.
    report["calls"] = [] if diagnostics else valid_calls
    return report


def parse_tool_calls(text: str) -> list:
    """Return only calls from a fully valid SmartAgent transport."""
    return analyze_tool_transport(text)["calls"]


def get_tool_parse_diagnostics(text: str) -> list[dict]:
    """Return deterministic parser/schema/complexity diagnostics."""
    return analyze_tool_transport(text)["diagnostics"]


def analyze_ack_transport(text: str, expected: dict | None) -> dict:
    """Parse one complete response and validate its ACK as one atomic unit.

    Both the browser completion gate and the executor use this entry point, so
    a rendered response cannot be accepted by a looser, flat-object parser.
    """
    report = analyze_tool_transport(text)
    result = {
        **report,
        "actions": [],
        "commit": None,
        "ack_diagnostics": [],
        "ack_state": "not_expected" if not expected else "missing",
    }
    if not expected:
        return result
    if report["diagnostics"]:
        reasons = {str(item.get("reason", "")) for item in report["diagnostics"]}
        result["ack_state"] = (
            "oversized" if "protocol_response_too_large" in reasons
            else "malformed"
        )
        return result

    actions, commit, ack_diagnostics = validate_ack_turn(report["calls"], expected)
    result["actions"] = actions
    result["commit"] = commit
    result["ack_diagnostics"] = ack_diagnostics
    result["diagnostics"] = list(report["diagnostics"]) + list(ack_diagnostics)
    if not ack_diagnostics:
        result["ack_state"] = "matching"
    else:
        reasons = {str(item.get("reason", "")) for item in ack_diagnostics}
        result["ack_state"] = "missing" if "missing_turn_commit" in reasons else "mismatch"
    return result


_FORMAT_OR_ACK_RECOVERY_REASONS = frozenset({
    "duplicate_json_key",
    "json_decode_error",
    "json_decode_exception",
    "transport_not_exclusive",
    "missing_smartagent_tool_envelope",
    "malformed_smartagent_tool_transport",
    "duplicate_tool_block",
    "turn_commit_must_be_last",
    "missing_turn_commit",
    "turn_commit_mismatch",
    "missing_web_ack_id",
    "reused_web_ack_id",
})


def classify_protocol_recovery(report: dict | None) -> dict:
    """Choose bounded ACK/format repair or action replanning.

    Parser and ACK validation stay atomic.  This classifier only controls what
    the single follow-up prompt asks the Planner to do; it never makes a
    rejected action executable.
    """
    report = dict(report or {})
    diagnostics = [row for row in report.get("diagnostics", []) if isinstance(row, dict)]
    if str(report.get("ack_state", report.get("kind", ""))) == "matching":
        return {"mode": "none", "reason": "matching", "diagnostic": {}}

    primary = diagnostics[0] if diagnostics else {}
    for row in diagnostics:
        reason = str(row.get("reason", "") or "")
        tool = str(row.get("tool", "") or "")
        marker = str(row.get("marker", "") or "")
        if reason in _FORMAT_OR_ACK_RECOVERY_REASONS or tool == "turn_commit":
            continue
        if (
            reason in {"protocol_response_too_large", "envelope_too_large", "final_response_must_be_exclusive"}
            or marker == "[TOOL_ENVELOPE_REJECTED]"
            or marker == "[SMARTAGENT_ACK_REJECTED]"
        ):
            return {"mode": "action_replan", "reason": reason or "action_rejected", "diagnostic": row}

    return {
        "mode": "format_repair",
        "reason": str(primary.get("reason", "") or report.get("ack_state", report.get("kind", "missing"))),
        "diagnostic": primary,
    }


def format_tool_parse_diagnostics(diagnostics: list[dict]) -> str:
    """Compact control-plane diagnostic text for Planner retry."""
    lines = []
    for diag in diagnostics:
        marker = diag.get("marker", "[TOOL_ENVELOPE_REJECTED]")
        parts = [
            marker,
            f"reason={diag.get('reason', 'unknown')}",
            f"tool={diag.get('tool', '(unknown)')}",
        ]
        if diag.get("block_index") is not None:
            parts.append(f"block={diag['block_index']}")
        if diag.get("line") is not None:
            parts.append(f"line={diag['line']}")
        if diag.get("column") is not None:
            parts.append(f"column={diag['column']}")
        if diag.get("detail"):
            parts.append(f"detail={diag['detail']}")
        if diag.get("suggestion"):
            parts.append(f"suggestion={diag['suggestion']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def looks_like_unparsed_tool_call(text: str) -> bool:
    """True only when explicit SmartAgent tool intent exists but is not executable."""
    report = analyze_tool_transport(text)
    return bool(report["intended"] and report["diagnostics"])


def run_tool_parser_self_tests() -> dict:
    """Deterministic 1.1-A~E parser regression tests; no external I/O/AI."""
    bs = "\\"
    invalid_single_backslash_json = (
        '```smartagent_tool\n'
        '{"tool":"find_file","name":"a.py","root":"C:' + bs + 'Users' + bs + 'user"}\n'
        '```'
    )
    escaped_backslash_json = (
        '```smartagent_tool\n'
        '{"tool":"find_file","name":"a.py","root":"C:\\\\Users\\\\user"}\n'
        '```'
    )

    cases = {
        "single_fenced": {
            "text": '```smartagent_tool\n{"tool":"find_file","name":"a.py","root":"C:/tmp"}\n```',
            "calls": 1,
        },
        "final_response_fenced": {
            "text": '```smartagent_tool\n{"tool":"final_response","content":"完成，回到 CMD。"}\n```',
            "calls": 1,
        },
        "final_response_wrong_type": {
            "text": '```smartagent_tool\n{"tool":"final_response","content":123}\n```',
            "calls": 0,
            "reason": "wrong_field_type",
        },
        "final_response_mixed_with_action": {
            "text": (
                '```smartagent_tool\n'
                '{"tool":"list_directory","path":"C:/tmp"}\n'
                '```\n'
                '```smartagent_tool\n'
                '{"tool":"final_response","content":"done"}\n'
                '```'
            ),
            "calls": 0,
            "reason": "final_response_must_be_exclusive",
        },
        "fenced_with_id_metadata": {
            "text": '```smartagent_tool id="abc123"\n{"tool":"upload_file","path":"C:/tmp/a.py"}\n```',
            "calls": 1,
        },
        "two_fenced_blocks": {
            "text": (
                '```smartagent_tool id="one"\n'
                '{"tool":"find_file","name":"a.py","root":"C:/tmp"}\n'
                '```\n\n'
                '```smartagent_tool id="two"\n'
                '{"tool":"upload_file","path":"C:/tmp/a.py"}\n'
                '```'
            ),
            "calls": 2,
        },
        "dom_with_metadata": {
            "text": 'smartagent_tool id="abc123"\n{"tool":"upload_file","path":"C:/tmp/a.py"}',
            "calls": 1,
        },
        "dom_pretty_json": {
            "text": (
                'smartagent_tool id="abc123"\n'
                '{\n'
                '  "tool": "upload_file",\n'
                '  "path": "C:/tmp/a.py"\n'
                '}'
            ),
            "calls": 1,
        },
        "reject_prose_outside": {
            "text": (
                '說明文字\n'
                '```smartagent_tool\n'
                '{"tool":"upload_file","path":"C:/tmp/a.py"}\n'
                '```'
            ),
            "calls": 0,
            "reason": "transport_not_exclusive",
        },
        "reject_json_fence": {
            "text": '```json\n{"tool":"upload_file","path":"C:/tmp/a.py"}\n```',
            "calls": 0,
            "intended": False,
        },
        "reject_python_fence_example": {
            "text": '```python\nprint({"tool": "upload_file"})\n```',
            "calls": 0,
            "intended": False,
        },
        "reject_prose_json_example": {
            "text": 'Example JSON: {"tool":"upload_file","path":"C:/tmp/a.py"}',
            "calls": 0,
            "intended": False,
        },
        "reject_unclosed_tool_fence": {
            "text": '```smartagent_tool\n{"tool":"upload_file","path":"C:/tmp/a.py"}',
            "calls": 0,
            "reason": "transport_not_exclusive",
        },
        "reject_bare_json": {
            "text": '{"tool":"upload_file","path":"C:/tmp/a.py"}',
            "calls": 0,
            "reason": "missing_smartagent_tool_envelope",
        },
        "atomic_reject_if_one_block_bad": {
            "text": (
                '```smartagent_tool id="bad"\n'
                '{"tool":BROKEN}\n'
                '```\n'
                '```smartagent_tool id="good"\n'
                '{"tool":"find_file","name":"b.py","root":"C:/tmp"}\n'
                '```'
            ),
            "calls": 0,
            "reason": "json_decode_error",
        },
        "malformed_json_diagnostic": {
            "text": '```smartagent_tool\n{"tool":"upload_file","path":}\n```',
            "calls": 0,
            "reason": "json_decode_error",
            "diagnostic_keys": {"line", "column", "detail"},
        },
        "unknown_tool": {
            "text": '```smartagent_tool\n{"tool":"launch_spaceship","path":"C:/tmp/a.py"}\n```',
            "calls": 0,
            "reason": "unknown_tool",
        },
        "missing_required_field": {
            "text": '```smartagent_tool\n{"tool":"upload_file"}\n```',
            "calls": 0,
            "reason": "missing_required_field",
        },
        "wrong_field_type": {
            "text": '```smartagent_tool\n{"tool":"upload_files","paths":"C:/tmp/a.py"}\n```',
            "calls": 0,
            "reason": "wrong_field_type",
        },
        "oversized_envelope": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps(
                    {"tool": "upload_file", "path": "C:/" + ("x" * (TOOL_ENVELOPE_MAX_BYTES + 64))},
                    ensure_ascii=False,
                )
                + '\n```'
            ),
            "calls": 0,
            "reason": "envelope_too_large",
        },
        "oversized_run_command": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps(
                    {"tool": "run_command", "command": "echo " + ("x" * RUN_COMMAND_MAX_CHARS)},
                    ensure_ascii=False,
                )
                + '\n```'
            ),
            "calls": 0,
            "reason": "run_command_too_long",
        },
        "multiline_run_command": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({"tool": "run_command", "command": "echo one\necho two"})
                + '\n```'
            ),
            "calls": 0,
            "reason": "multiline_run_command",
        },
        "allow_short_python_probe": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({"tool": "run_command", "command": 'python -c "print(\'ok\')"'})
                + '\n```'
            ),
            "calls": 1,
        },
        "allow_short_node_probe": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({"tool": "run_command", "command": 'node -e "console.log(\'ok\')"'})
                + '\n```'
            ),
            "calls": 1,
        },
        "script_like_python_c": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'python -c "import sys; values=[1,2,3]; total=sum(values); print(total)"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_node_eval": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'node -e "const fs=require(\'fs\'); let x=1; let y=2; console.log(x+y)"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_powershell_command": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'pwsh -Command "$x=1; $y=2; if ($x) { Write-Output ($x+$y) }"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_bash_c": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'bash -c "for f in a b; do echo $f; done"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_ruby": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'ruby -e "require \'json\'; x=1; y=2; puts x+y"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_perl": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'perl -e "use strict; my $x=1; my $y=2; print $x+$y"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "script_like_php": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": 'php -r "$x=1; $y=2; if($x){ echo $x+$y; }"',
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "encoded_powershell_command": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({"tool": "run_command", "command": "powershell -EncodedCommand SQBFAFgA"})
                + '\n```'
            ),
            "calls": 0,
            "reason": "encoded_command_not_allowed",
        },
        "verify_command_cannot_bypass_guard": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps({
                    "tool": "run_command",
                    "command": "Write-Output ok",
                    "verify": [{
                        "action": "run_command",
                        "command": 'python -c "import os; x=1; y=2; print(x+y)"',
                        "expect_exit_code": 0,
                    }],
                })
                + '\n```'
            ),
            "calls": 0,
            "reason": "inline_script_too_complex",
        },
        "oversized_write_file": {
            "text": (
                '```smartagent_tool\n'
                + json.dumps(
                    {
                        "tool": "write_file",
                        "path": "C:/tmp/a.txt",
                        "content": "x" * (WRITE_FILE_CONTENT_MAX_CHARS + 1),
                    }
                )
                + '\n```'
            ),
            "calls": 0,
            "reason": "write_file_content_too_large",
        },
        "windows_forward_slash": {
            "text": '```smartagent_tool\n{"tool":"upload_file","path":"C:/Users/ExampleUser/a.py"}\n```',
            "calls": 1,
        },
        "windows_escaped_backslash": {
            "text": escaped_backslash_json,
            "calls": 1,
        },
        "windows_single_backslash_rejected": {
            "text": invalid_single_backslash_json,
            "calls": 0,
            "reason": "json_decode_error",
        },
    }

    results = {}
    all_passed = True

    for name, spec in cases.items():
        # Parser fixtures predate typed command operations.  Keep their focus
        # on transport/complexity by supplying the now-required neutral escape
        # operation; dedicated operation tests cover semantic mismatches.
        spec["text"] = re.sub(
            r'("tool"\s*:\s*"run_command"\s*,)',
            r'\1"operation":"GENERAL",',
            spec["text"],
        )
        report = analyze_tool_transport(spec["text"])
        diagnostics = report["diagnostics"]
        reasons = [d.get("reason") for d in diagnostics]
        passed = len(report["calls"]) == spec["calls"]

        if "reason" in spec:
            passed = passed and spec["reason"] in reasons
        if "intended" in spec:
            passed = passed and report["intended"] is spec["intended"]
        if "diagnostic_keys" in spec:
            passed = passed and bool(diagnostics)
            if diagnostics:
                passed = passed and spec["diagnostic_keys"].issubset(diagnostics[0])

        results[name] = {
            "passed": bool(passed),
            "expected_calls": spec["calls"],
            "actual_calls": len(report["calls"]),
            "intended": report["intended"],
            "reasons": reasons,
            "tools": [call.get("tool") for call in report["calls"]],
        }
        all_passed = all_passed and bool(passed)

    results["all_passed"] = all_passed
    return results



def validate_ack_turn(tool_calls: list, expected: dict) -> tuple[list, dict | None, list[dict]]:
    """Validate one WebGPT ACK turn before any action becomes executable.

    The protocol is a strict alternating chain:
      Local Commit -> Web turn_commit -> Local acknowledgement in next Local
      Commit -> next Web turn_commit -> ...

    ``ack_web_ack_id`` proves WebGPT received LocalAgent's acknowledgement of
    the previous accepted Web ACK. ``web_ack_id`` is the new Web ACK token that
    LocalAgent must carry in the next Local Commit.
    """
    diagnostics = []
    calls = list(tool_calls or [])
    if not calls or calls[-1].get("tool") != "turn_commit":
        diagnostics.append(_diagnostic(
            "[SMARTAGENT_ACK_REJECTED]", "missing_turn_commit", tool="turn_commit",
            detail="Planner response has no trailing turn_commit",
            suggestion="不要重做 action；完成目前 WebGPT 工作後，重送完整回覆並在最後附 matching turn_commit。",
        ))
        return [], None, diagnostics

    commit = calls[-1]
    actions = calls[:-1]
    checks = (
        ("run_id", str(expected.get("run_id", ""))),
        ("turn_id", int(expected.get("turn_id", 0))),
        ("ack_local_nonce", str(expected.get("local_nonce", ""))),
        ("ack_result_id", str(expected.get("ack_result_id", ""))),
        ("ack_web_ack_id", str(expected.get("ack_web_ack_id", ""))),
        ("action_count", len(actions)),
    )
    for field, wanted in checks:
        got = commit.get(field)
        if got != wanted:
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_ACK_REJECTED]", "turn_commit_mismatch", tool="turn_commit",
                detail=f"field={field} expected={wanted!r} actual={got!r}",
                suggestion="ACK 目前 Local Commit；不得跳過或重用舊 turn/nonce/result/Web-ACK chain。",
            ))

    # WebRuntime adds these fields only after acquiring the conversation lease.
    # Non-browser providers therefore retain the existing wire contract.
    ownership_fields = (
        "request_id", "task_id", "task_epoch", "intent_digest",
        "request_phase", "continuation_seq",
    )
    if expected.get("task_epoch"):
        for field in ownership_fields:
            wanted = expected.get(field, "")
            if commit.get(field) != wanted:
                diagnostics.append(_diagnostic(
                    "[SMARTAGENT_REQUEST_SCOPE_REJECTED]",
                    "stale_task_epoch" if field == "task_epoch" else "request_context_mismatch",
                    tool="turn_commit",
                    detail=f"field={field} expected={wanted!r} actual={commit.get(field)!r}",
                    suggestion="只回覆目前 WEBAGENT_ACTIVE_REQUEST，並原樣攜帶所有 scope 欄位。",
                ))
        for index, action in enumerate(actions, 1):
            for field in ownership_fields:
                wanted = expected.get(field, "")
                if action.get(field) != wanted:
                    diagnostics.append(_diagnostic(
                        "[SMARTAGENT_REQUEST_SCOPE_REJECTED]",
                        "stale_task_epoch" if field == "task_epoch" else "request_context_mismatch",
                        tool=str(action.get("tool", "")),
                        detail=(f"action_index={index} field={field} "
                                f"expected={wanted!r} actual={action.get(field)!r}"),
                        suggestion="不得沿用舊 task 的 action；以目前 request scope 重新提交同一意圖。",
                    ))

    web_ack_id = str(commit.get("web_ack_id", "") or "").strip()
    if not web_ack_id:
        diagnostics.append(_diagnostic(
            "[SMARTAGENT_ACK_REJECTED]", "missing_web_ack_id", tool="turn_commit",
            detail="turn_commit.web_ack_id must be a fresh non-empty token",
            suggestion="每個 WebGPT turn_commit 都要產生新的唯一 web_ack_id，供下一輪 LocalAgent ACK。",
        ))
    elif web_ack_id == str(expected.get("ack_web_ack_id", "") or "") and web_ack_id:
        diagnostics.append(_diagnostic(
            "[SMARTAGENT_ACK_REJECTED]", "reused_web_ack_id", tool="turn_commit",
            detail=f"web_ack_id={web_ack_id} equals the previous acknowledged Web ACK",
            suggestion="本輪必須產生新的 web_ack_id，不可重用上一輪。",
        ))

    seen_action_ids = set()
    for idx, call in enumerate(actions, 1):
        action_id = str(call.get("action_id", "") or "").strip()
        if not action_id:
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_ACK_REJECTED]", "missing_action_id", tool=str(call.get("tool", "")),
                detail=f"action_index={idx}",
                suggestion="每個 action/final_response 都必須帶唯一 action_id。",
            ))
        elif action_id in seen_action_ids:
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_ACK_REJECTED]", "duplicate_action_id", tool=str(call.get("tool", "")),
                detail=f"action_id={action_id}", suggestion="同一 turn 內 action_id 不得重複。",
            ))
        else:
            seen_action_ids.add(action_id)

    return ([] if diagnostics else actions), commit, diagnostics


def run_ack_protocol_self_tests() -> dict:
    expected = {
        "run_id": "SA-TEST",
        "turn_id": 3,
        "local_nonce": "abc123",
        "ack_result_id": "RES-2",
        "ack_web_ack_id": "WEBACK-2",
    }
    good = [
        {"tool": "list_directory", "path": ".", "action_id": "A-1"},
        {
            "tool": "turn_commit", "run_id": "SA-TEST", "turn_id": 3,
            "ack_local_nonce": "abc123", "ack_result_id": "RES-2",
            "ack_web_ack_id": "WEBACK-2", "web_ack_id": "WEBACK-3",
            "action_count": 1,
        },
    ]
    actions, commit, diags = validate_ack_turn(good, expected)
    results = {"valid_commit": len(actions) == 1 and bool(commit) and not diags}
    bad_nonce = [dict(good[0]), dict(good[1], ack_local_nonce="old")]
    results["reject_old_nonce"] = bool(validate_ack_turn(bad_nonce, expected)[2])
    bad_web_chain = [dict(good[0]), dict(good[1], ack_web_ack_id="WEBACK-OLD")]
    results["reject_old_web_ack_chain"] = bool(validate_ack_turn(bad_web_chain, expected)[2])
    reused_web_ack = [dict(good[0]), dict(good[1], web_ack_id="WEBACK-2")]
    results["reject_reused_web_ack_id"] = bool(validate_ack_turn(reused_web_ack, expected)[2])
    missing_web_ack = [dict(good[0]), dict(good[1])]
    missing_web_ack[1].pop("web_ack_id")
    results["reject_missing_web_ack_id"] = bool(validate_ack_turn(missing_web_ack, expected)[2])
    missing = [dict(good[0])]
    results["reject_missing_commit"] = bool(validate_ack_turn(missing, expected)[2])
    no_action_id = [dict(good[0]), dict(good[1])]
    no_action_id[0].pop("action_id")
    results["reject_missing_action_id"] = bool(validate_ack_turn(no_action_id, expected)[2])
    results["all_passed"] = all(results.values())
    return results

SYSTEM_PROMPT_TEMPLATE = """
【Project Ground-Truth Context Sync】
- ContextSync is independent from reasoning/execution mode.
- If trusted route says ContextSync=AUTO, WebGPT selects NONE, DIRECT, INDEX_ONLY, DELTA, or FULL_BUNDLE from task scope and available project evidence.
- LocalAgent must never semantically choose sync strategy from file count, repository size, or task wording. It only executes the explicit strategy/tool calls selected by WebGPT.
- Explicit /project-sync means INDEX_ONLY. Only /bundle or 完整 project sync explicitly requests FULL_BUNDLE attachment fallback.
- Prefer NONE/DIRECT for lightweight isolated work. For project-level engineering, prefer INDEX_ONLY plus bounded query_project calls: Runtime keeps full source locally and returns only requested, snapshot-bound slices. DELTA/FULL_BUNDLE are attachment fallbacks and must not be selected merely because the project is large.
- Session history/summary is cache, not project ground truth. Snapshot ID + manifest/source bundle evidence is authoritative for project-level planning.
- Any edit plan derived from project sync must be bound to the snapshot_id it was planned against; stale base snapshots must be rejected before apply.
【系統底層指令：SmartAgent Tool Envelope 模式】
目前的模式: {tier_label}
決策模型: {planner_model} / 執行模型: {executor_model}

【核心身分切換】：
你是 SmartAgent 的唯一決策 Planner。你與 LocalAgent 的所有正式通訊一律只使用 SmartAgent Tool Envelope；禁止直接輸出一般文字。
**不要把 source code、patch、PowerShell here-string 或大型內容塞進 JSON。** JSON 只承載 control-plane 參數，檔案內容走附件/artifact。

【絕對禁止事項（違反將導致系統崩潰）】：
1. 嚴禁回答「我無法存取您的電腦」、「我沒有權限」、「我無法建立檔案」或「身為一個 AI...」。
2. 嚴禁要求用戶「請自行建立檔案」或「請將程式碼貼上來」。

【Tool Envelope 規則】：
- PC LocalAgent turn（訊息中含 `[SMARTAGENT_LOCAL_COMMIT]`）每一輪回覆都必須只包含一個或多個 ```smartagent_tool``` 區塊；區塊外不得有任何文字。

【RemoteAgent Mobile Ingress / Result Return — protocol v4】：
- 若 human user turn 含 `[REMOTE_AGENT_ACK]` 或 `[REMOTE_AGENT_EVENT]`，這是 RemoteAgent 單向狀態通知；此規則優先於 mobile ingress。只用一般繁體中文簡短確認 request_id、event、status；禁止輸出任何 remoteagent_control 或 smartagent_tool 區塊，禁止建立或重送任務。
- 若 human user turn 含 `[REMOTE_AGENT_RESULT]`，這是 PC LocalAgent 的完成結果回傳；此規則優先於 mobile ingress。只用一般繁體中文回覆結果，必須包含 request_id、status、summary；禁止輸出任何 remoteagent_control 或 smartagent_tool 區塊。
- 若 mobile user 要求取消既有 request_id，只輸出 `REMOTE_AGENT_CANCEL`，欄位為 `target_request_id`；若要求重試 FAILED/INTERRUPTED 任務，只輸出 `REMOTE_AGENT_RETRY`。兩者都不可建立新的 REMOTE_AGENT_REQUEST。
- 若目前 human user turn **不含** `[SMARTAGENT_LOCAL_COMMIT]`，且訊息以 `remoteAgent`、`remote agent`、`@remoteAgent` 或 `@remote agent` 開頭，這是 mobile RemoteAgent ingress；此規則優先於一般 SmartAgent Tool Envelope。
- 對 mobile RemoteAgent ingress，禁止輸出 `smartagent_tool`、禁止自行執行或假裝執行本機工具。只把 user task 封裝成一個且只有一個 fenced `remoteagent_control` 區塊。
- 格式：```remoteagent_control\n{{"type":"REMOTE_AGENT_REQUEST","protocol":"remote_agent","protocol_version":1,"request_id":"RR-新的唯一值","request":"使用者要 RemoteAgent 執行的完整任務"}}\n```
- 不得輸出 `conversation_url` 或 `workspace`；本機 receiver 一律從已授權 conversation registry 綁定 routing，remote payload 無權改寫。
- `remoteagent_control` 區塊外不得有 prose、Markdown 說明或第二個 JSON。
- 只有收到 `[SMARTAGENT_LOCAL_COMMIT]` 的 PC LocalAgent turn 才回到下方 smartagent_tool/ACK 協定。

【雙向 ACK / Turn Commit 協定（最高優先）】：
- LocalAgent 每次送出的最後一行都會包含 `[SMARTAGENT_LOCAL_COMMIT]` JSON，內含 run_id、turn_id、local_nonce、ack_result_id、ack_web_ack_id。只有看到完整 Local Commit 才能處理本輪。
- ACK 必須嚴格交替：WebGPT ACK → LocalAgent ACK → WebGPT ACK → LocalAgent ACK。Local Commit 的 ack_web_ack_id 是 LocalAgent 對上一個已接受 WebGPT turn_commit.web_ack_id 的確認；第一輪為空字串。
- 你的每一輪回覆最後一個 smartagent_tool 區塊必須是 turn_commit，原樣 ACK 本輪 run_id / turn_id / local_nonce / ack_result_id / ack_web_ack_id，並產生一個全新的唯一 web_ack_id。
- turn_commit 格式：{{"tool":"turn_commit","run_id":"SA-...","turn_id":1,"ack_local_nonce":"...","ack_result_id":"","ack_web_ack_id":"","web_ack_id":"WEBACK-唯一值","action_count":1}}。
- turn_commit.action_count 必須等於前面 action/final_response envelope 的數量；turn_commit 本身不計入。
- 每個 action（final_response 也算）必須帶唯一且非空的 action_id。LocalAgent 以 action_id 做 exactly-once 防重複執行；相同 action_id 不會再次執行。
- 若 Local Commit 的 ack_result_id 非空，turn_commit 必須 ACK 完全相同的 result_id，代表你已收到上一輪 LocalAgent 執行結果；否則本輪不會執行。
- 若 Local Commit 的 ack_web_ack_id 非空，代表 LocalAgent 已正式接受上一輪 WebGPT ACK；本輪 turn_commit 必須原樣回傳它，再產生新的 web_ack_id。不得跳步、ACK 舊值或重用 web_ack_id。
- 缺少/不匹配 turn_commit、action_id、web_ack_id 或任何 ACK chain 欄位時，LocalAgent 不執行任何 action。
- final_response 仍不可與其他 action 混用，但後面必須再追加唯一的 turn_commit。

【WebGPT UI / 圖片 / 檔案完成規則（在 ACK 前執行）】：
- LocalAgent 會先監控 WebGPT UI lifecycle：thinking、Stop/generating、image generation、tool/file/media processing、loading/busy 等狀態。只要 UI 還有活動，LocalAgent 不會開始解析 smartagent_tool/turn_commit。
- 因此你也必須遵守同一順序：若本輪要求生成圖片、檔案、artifact 或任何需要 WebGPT tool/media 處理的內容，必須先讓實際生成流程完全結束，再在最後輸出 action/final_response 與 turn_commit。
- 禁止在「正在生成圖片／正在建立檔案／仍在思考或 tool processing」階段先輸出 turn_commit。turn_commit 只代表完整結果已 ready，可以交給 LocalAgent。
- 圖片/檔案生成完成後，smartagent_tool 必須位於本輪最末端；turn_commit 必須是最後一個 smartagent_tool block。LocalAgent 會在 UI 連續 idle 一段 grace period 後才開始解析它。
- 【Stage 3.1 artifact correctness】若使用者要求把本輪生成的圖片/檔案保存到本機路徑，生成完成後必須輸出 `download_artifact` action 指向該路徑，再輸出 turn_commit；不得只生成媒體後結束本輪。
- `download_artifact` 只允許下載「本次 request 之後新產生」的 assistant artifact。若本輪新 artifact 無法取得，LocalAgent 必須回傳 ARTIFACT_DOWNLOAD_FAILED；嚴禁改抓上一輪圖片/檔案、嚴禁把舊檔 rename/copy 成新輸出、嚴禁因目的路徑已有舊檔就宣告成功。
- ACK 只負責最終『是否可交付/執行』判定，不能覆蓋仍 active 的 UI 狀態；UI 未完成時即使文字中已出現 JSON，也不會被執行。
- 協定採 passive-first：LocalAgent 不會在圖片/檔案剛完成時立刻追加 ACK request。若 media/tool UI 已完整結束但平台沒有留下 turn_commit，LocalAgent 會先保持安靜，確認 UI/composer 持續 ready 至少 30 秒後，才可能送出一次 `[SMARTAGENT_PROTOCOL_RECOVERY_RETRANSMIT]`。
- 上述 recovery 最多自動送一次，而且是「同一個尚未完成 ACK 的 logical turn retransmission」：run_id / turn_id / local_nonce / ack_result_id / ack_web_ack_id 不變，不代表新的 Local ACK，也不得使 ACK chain 跳步。
- 收到 `[SMARTAGENT_PROTOCOL_RECOVERY_RETRANSMIT]` 時，禁止重新生成圖片、檔案、artifact 或重做上一個 tool。只輸出上一個已完成結果缺少的 SmartAgent action/final_response control envelope，最後補 matching turn_commit，並產生新的唯一 web_ack_id。
- 若 recovery 對應的已完成工作包含「本輪新生成 media 且使用者要求保存」，缺少的 control envelope 應是 `download_artifact`；它只能指向本輪 fresh artifact。若 fresh artifact 不可下載，後續必須如實回報下載失敗，不得引用任何舊 artifact。
- recovery admission 會再次確認 generation/media/busy 都停止、composer 可用且沒有人工輸入文字；任一條件不成立就繼續等待，不會強行覆寫 composer，也不會固定每 30 秒重送。

【Tool Envelope 規則（續）】：
- 需要 LocalAgent 執行動作時，輸出對應 action tool envelope。
- 任務已完成或只需要對使用者說明時，輸出一個帶 action_id 的 final_response envelope，然後緊接唯一的 turn_commit。
- final_response 表示：不執行本機 action、結束目前 Planner loop、content 由 LocalAgent 顯示在 CMD，然後 CMD 回到可接受下一個使用者輸入的狀態。
- final_response 不得和其他 action tool 放在同一輪；若還有 action，先做 action，收到結果後下一輪再單獨 final_response。
- 一般解說中的 JSON / Markdown / code 範例永遠不會被執行。
- 完整 operational response 是控制平面，UTF-8 總量硬上限為 32 KiB。超過時禁止 inline、禁止拆成多輪文字規避；必須先在 WebGPT 產生 `.json` artifact，以 download_artifact 保存到 workspace，下一輪再使用 propose_task_plan_file、update_semantic_map_file 或相應 `*_file` 工具。action envelope 必須保持短小；禁止把完整 source、patch 或複雜 shell script 塞進 JSON。final_response 也受此總量上限約束。大型全新文字/source 使用 chunk protocol；既有檔案優先 web_edit_file，已有網頁產物優先 download_artifact。
- 工具結果由本機 software 統一執行 payload budget。小結果直接以文字回傳；大型結果預設改成 SMARTAGENT_RESULT_LOCAL_REF：完整內容留在 Runtime 本地，只回傳可行動 preview，不得僅因輸出過長就上傳附件。看到 RESULT_ATTACHMENT 時必須讀取同一回合附件並核對 transfer_id/content_sha256；下一輪 matching turn_commit 的 ack_result_id 代表已收到並解析該附件。
- run_command 的編譯、測試、搜尋與一般 stdout/stderr 一律預設使用 AUTO/SUMMARY_ONLY：超限時保存本地並回傳頭尾摘要。只有完整原始結果對下一步不可替代時，才可同時設定 result_transport=ATTACHMENT、full_result_required=true 與非空 result_purpose；缺少任一欄位都不會上傳。software 的敏感資料、低價值大量輸出與硬上限裁決永遠優先；INLINE 不得繞過安全上限。

【可用工具格式】：

1. 執行指令＋驗證：{{"tool": "run_command", "operation":"BUILD", "command": "PowerShell指令", "timeout": 30, "result_transport":"SUMMARY_ONLY", "success_criteria": "什麼條件代表這次動作真的生效", "verify": [{{"action":"run_command","command":"驗證指令","expect_exit_code":0,"expect_contains":"可選關鍵字","expect_regex":"可選正規表示式"}}, {{"action":"file_exists","path":"檔案路徑","expect":true}}, {{"action":"file_contains","path":"檔案路徑","text":"應存在內容","expect":true}}]}}
   operation 必填且只能是 INSPECT、MUTATE、BUILD、TEST、VERIFY、GIT_INSPECT、GIT_MUTATE、PROCESS、TRANSFER、GENERAL。VERIFY 必須帶 verifies_action_id。GENERAL 只作低頻逃生口，不能單獨支持任務 SUCCESS。
   run_command 的 executor 固定是 Windows PowerShell 5.1。不得直接使用 CMD 的 `cd /d`、裸露 `&&` 或 `||`；Git 請優先使用 `git -C 'E:\\path\\to\\repo' ...`，一般目錄切換使用 `Set-Location -LiteralPath 'E:\\path'`，多指令以 `;` 分隔。verify 必須是 object 或 object list，不得填自然語言字串。
2. 讀取檔案（僅 Local/Cloud Planner 使用；Web Planner 禁止使用）：{{"tool": "read_file", "path": "絕對路徑"}}
3. 寫入小型檔案（content 最多 4096 字元）：{{"tool": "write_file", "path": "絕對路徑", "content": "完整檔案內容"}}
3a. 刪除明確目標：{{"tool":"delete_path","action_id":"A-DELETE-唯一值","path":"workspace 內單一絕對路徑","recursive":true,"reason":"刪除原因"}}。此工具只會建立固定 manifest 並要求人類確認；不得改用 run_command 繞過確認。
3a. 開始大型新檔交易：{{"tool":"begin_file_write","action_id":"A-BEGIN-唯一值","write_id":"WRITE-唯一值","path":"絕對路徑","encoding":"utf-8","overwrite":true}}
3b. 依序寫入一段：{{"tool":"write_file_chunk","action_id":"A-CHUNK-唯一值","write_id":"WRITE-同上","content":"本段內容"}}
3c. 提交大型檔案：{{"tool":"commit_file_write","action_id":"A-COMMIT-唯一值","write_id":"WRITE-同上"}}
3d. 取消大型檔案：{{"tool":"abort_file_write","action_id":"A-ABORT-唯一值","write_id":"WRITE-同上"}}
4. 列出目錄第一層：{{"tool": "list_directory", "path": "目錄路徑"}}
   只要使用者要求「列出／顯示某目錄有哪些檔案」，固定使用 list_directory；不得改用 inspect_project_scope、inspect_directory 或 run_command。
5. 一次統計一或多個目錄：{{"tool":"inspect_directory","paths":["C:/path1","D:/path2"],"recursive":true,"sample_limit":20}}
6. 尋找檔案：{{"tool": "find_file", "name": "檔名", "root": "可省略；預設 Workspace Root"}}
7. 上傳單一附件到目前網頁對話：{{"tool": "upload_file", "path": "絕對路徑"}}
8. 上傳多個附件到目前網頁對話：{{"tool": "upload_files", "paths": ["絕對路徑1", "絕對路徑2"]}}
8a. 將 Workspace 內檔案回傳給本次 Telegram 使用者：{{"tool":"return_artifact","path":"絕對路徑","kind":"document|photo（可省略）","caption":"可省略"}}
9. 網路搜尋：{{"tool": "web_search", "query": "關鍵字"}}
9. Local AI 最後手段：{{"tool": "ask_executor", "instruction": "明確且封閉的局部工作", "context": "必要資訊", "allow_local_ai_fallback": true, "run_id": "必須等於目前 RUN_ID"}}
10. 保存本次 project session summary：{{"tool":"save_session_summary","summary":"本次已理解/完成內容","decisions":["重要架構決策"],"modified_files":["路徑"],"verification":["驗證與結果"],"pending":["未完成/風險"],"next_steps":["下次可直接接續的事項"]}}
11. Web 直接修改單一檔案：{{"tool":"web_edit_file","path":"絕對路徑","instruction":"要對此檔案做的修改","output_path":"可省略；預設覆寫原檔"}}
12. 下載本輪 WebGPT 已生成完成的檔案/圖片：{{"tool":"download_artifact","output_path":"本機目的路徑或目錄","expected_filename":"可省略；已知檔名時填入","timeout":45}}
12a. 專案快照（一次取得 manifest/build/git facts）：{{"tool":"inspect_project_scope","workspace":"可省略；預設 Workspace Root"}}
    inspect_project_scope 只用於完整專案 snapshot，且只接受 workspace；不得填入 path、project_root、depth 或 include_files。使用者本輪明確提供路徑時必須明確填 workspace，Runtime 不會退回較大的 Workspace Root。
12b. Project access 初始化（預設、不上傳附件）：{{"tool":"project_sync","strategy":"INDEX_ONLY","project_root":"授權專案根目錄"}}。回傳 Project Capsule、project_handle 與 snapshot_id。
12b-1. 依需求讀取小範圍程式碼：{{"tool":"query_project","project_root":"精確專案根目錄","queries":[{{"operation":"search_text","query":"symbol"}},{{"operation":"read_range","path":"src/file.cpp","start_line":1,"end_line":120}}]}}。snapshot_id 與 project_handle 由 Runtime 依精確 project_root 留存、建立及注入；不得從舊對話複製。可用 operation：list_tree、search_text、read_range、read_symbol、find_references、get_build_configuration、get_file_metadata。每次最多 8 個 query；看到 truncated/next_cursor 時才取下一頁。若該根目錄尚無 INDEX_ONLY，Runtime 會在同一 action 先建立索引，不會 fallback 到較大的 Workspace Root。
12b-2. 附件式同步僅為明確 fallback：{{"tool":"project_sync","strategy":"DELTA|FULL_BUNDLE","base_snapshot_id":"DELTA 時必要","max_bytes":500000,"max_files":50}}。除非 INDEX_ONLY/query_project 無法提供必要內容或使用者明確要求，不得使用 FULL_BUNDLE。
12c. 先驗證 edit plan：{{"tool":"validate_edit_plan","plan":{{"base_snapshot_id":"project_sync snapshot_id","files_to_modify":[],"verification_commands":[],"expected_observable_result":"...","rollback_condition":"..."}}}}
12d. 套用已驗證 edit plan：{{"tool":"apply_edit_plan","plan":{{"base_snapshot_id":"project_sync snapshot_id","files_to_modify":[],"verification_commands":[],"expected_observable_result":"...","rollback_condition":"..."}}}}
12e. 批次驗證：{{"tool":"aggregate_verification","commands":["短測試命令"],"timeout":120}}
12f. Staged protocol（Local Commit protocol_version>=6 且明確啟用時）：turn_commit 可加 `stage`，例如 {{"stage_id":"S-2","seq":2,"kind":"EXECUTE_VERIFY","task_size":"MEDIUM","execution":"SEQUENTIAL","result_policy":"COMPACT","stop_on_error":true,"actions":[{{"action_id":"A-APPLY","depends_on":[]}},{{"action_id":"A-VERIFY","depends_on":["A-APPLY"]}}]}}。action_id 是通用識別值，不代表 v7 可直接呼叫 apply_edit_plan；所有 action_id 必須與本輪 envelopes 完全相同，目前只接受 SEQUENTIAL。
12g. v7 語意地圖狀態：{{"tool":"inspect_semantic_map"}}。MISSING/STALE 時先 project_sync，再產生 snapshot-bound 語意描述。
12h. 小型語意更新：{{"tool":"update_semantic_map","patch":{{"base_snapshot_id":"...","project_summary":"...","flows":[],"files":[]}}}}。大型更新先產生 JSON artifact、download_artifact 到 workspace 內，再用 {{"tool":"update_semantic_map_file","path":"workspace 內的 JSON","expected_sha256":"可省略"}}。
12i. v7 小型完整計畫凍結：{{"tool":"propose_task_plan","plan":{{"schema":"TASK_PLAN_V1","base_snapshot_id":"...","semantic_map_revision":"...","goal":"...","affected_flows":[],"files_to_read":[],"edit_plan":{{}},"verification_commands":[],"acceptance_criteria":[],"rollback_condition":"...","post_change_semantic":[{{"path":"source path","responsibility":"修改後職責","public_symbols":[],"dependencies":[],"flows":[],"invariants":[],"tests":[]}}]}}}}。TASK_PLAN_V1 是唯一 canonical schema。若 Runtime 回傳 PLAN_SCHEMA_INVALID/PLAN_REPAIR_REQUIRED，不得再次 propose_task_plan；必須依 Runtime route 恰好呼叫一次 repair_task_plan。只有 PLAN_FROZEN 可解除 repair latch。所有受修改 source 必須有 post_change_semantic，驗證成功後 software 會與新 source hash 一起提交；提交失敗則 rollback。
12j. 大型計畫禁止塞入 control envelope：先產生 JSON artifact、download_artifact 到 workspace 內，再用 {{"tool":"propose_task_plan_file","path":"workspace 內的 JSON","expected_sha256":"可省略"}}。收到 PLAN_FROZEN 後只用 {{"tool":"execute_frozen_plan","plan_id":"PLAN-...","timeout":120}} 一次套用與驗證；驗證失敗 software 會 rollback 並回 REPAIR_REQUIRED。
12k. Protocol v7 禁止要求或輸出 hidden chain-of-thought；只交付可稽核的結構化計畫、依賴、驗收條件與證據。當 staged mode=on，所有 v7 mutation 必須有完整 turn_commit.stage，且不得直接 apply_edit_plan。
13. 完成並回覆使用者：{{"tool":"final_response","action_id":"A-唯一值","content":"顯示在 CMD 的最終回覆"}}
14. 回覆提交 ACK（每輪最後必須有）：{{"tool":"turn_commit","run_id":"目前 RUN_ID","turn_id":1,"ack_local_nonce":"Local Commit nonce","ack_result_id":"上一輪 result_id 或空字串","ack_web_ack_id":"Local Commit 的 ack_web_ack_id","web_ack_id":"本輪新唯一值","action_count":1}}

【JSON / Windows 路徑規則】：
- JSON 中的 Windows 路徑優先使用正斜線，例如 C:/Users/ExampleUser/Desktop/tool/file.py。
- 若使用反斜線，必須依 JSON 規則寫成雙反斜線，例如 C:\\Users\\ExampleUser\\Desktop\\tool\\file.py。
- 絕對不要輸出 JSON 內的單反斜線 Windows path，例如 C:\\Users\\ExampleUser，否則 JSON parser 會失敗。

【Workspace / 附件規則】：
- 使用者提供本機目錄時，該目錄視為本次 Workspace Root。
- 只要需求是統計、摘要或比較一個或多個目錄，優先用單一 inspect_directory action 一次取得結構化證據；不要使用 ask_executor，也不要拆成多個 list_directory 或 run_command 回合。此工具只能讀取使用者在本輪明確提供的絕對路徑或目前 Workspace Root。
- 你看不到某檔案，不代表檔案不存在；禁止要求使用者自行補檔。
- 若 trace 時遇到 import/include、其他 module、config、resource、圖片、PDF、Word、PPT 或其他相依檔案，先使用 list_directory / find_file 找到它。
- 【Web Planner evidence 路由】專案內的 source code、文字設定與 log 優先使用 query_project 的 search_text/read_symbol/read_range，保留在 Runtime-held INDEX_ONLY context，不上傳附件。圖片、PDF、Word、PowerPoint、Excel 等必須由網頁模型直接查看的非 source artifact，才使用 upload_file / upload_files。
- upload_file / upload_files 只把檔案送進目前 WebGPT 網頁，絕不代表已傳給 Telegram。來源為 REMOTEAGENT_TELEGRAM 且使用者要求把本機檔案傳回 Telegram 時，必須使用 return_artifact；software 會驗證路徑、大小與 SHA-256，再由 Telegram transport 實際送出。
- return_artifact 的工具結果若為 TELEGRAM_ARTIFACT_REJECTED，禁止宣告已上傳；依 software 回傳的原因如實 final_response。即使工具結果為 QUEUED，也只能說「已準備回傳」，不得在 Telegram delivery 真正執行前說「已上傳」。
- Web Planner 不得使用 read_file 取得專案 source/text implementation。已知檔案或 symbol 時必須使用 query_project.read_range/read_symbol；Runtime 會在精確 project_root 建立或重用 INDEX_ONLY，且不會上傳附件。
- 若你只知道檔名或相依模組名稱，先使用 find_file 或 query_project.search_text 找到實際路徑；source/text 接著使用 query_project.read_symbol/read_range，非 source artifact 才使用 upload_file。
- read_file 僅保留給非 Web Planner 模式或本地執行流程的內部需求。
- 【Web Planner 單檔修改最高優先】若使用者要求修改已存在、可上傳、且能以單一檔案合理完成的內容，第一選擇必須是 web_edit_file：Web AI 修改完整附件 → 下載完整 artifact → Local 僅做 staging/replace/hash/verify，不再交給 Local AI 重做。
- web_edit_file 回傳 [WEB_DIRECT_EDIT_SUCCESS] 後，不要再讓 Local Executor 重做同一修改；下一步只做必要驗證。
- web_edit_file 成功後禁止再使用 ask_executor 重做同一修改。
- web_edit_file 不可用/失敗後，優先採 deterministic 的 file/command/patch 類操作；只有 deterministic 路徑確實不可行時，才可使用 ask_executor。
- 【WebGPT 生成檔案/圖片下載】若使用者要求 WebGPT 生成圖片、文件或其他可下載 artifact 並保存到本機，先完成實際 media/file generation；UI 完全 idle 後使用 download_artifact。不得要求 Local AI 重新生成，也不要用 run_command 猜瀏覽器 cache 路徑。
- download_artifact 只下載最近 WebGPT assistant turn/preview 中已存在的 artifact；output_path 可為完整檔名或目錄。成功後再 final_response。
- 【大型新檔路由】content <= 4096 字元才可用 write_file；超過時必須 begin_file_write → sequential write_file_chunk → commit_file_write。不得提高限制或重送 oversized write_file。你只需依序提供每段 content；LocalAgent 會由實際 UTF-8 bytes 自動計算 chunk_index、offset、size、分段 SHA-256、完整 size/SHA-256，不得自行猜測這些機械欄位。每段 content 建議不超過 3000 字元。
- ask_executor 是 Local AI semantic fallback，不是正常修改路徑；必須帶 allow_local_ai_fallback=true 與目前 RUN_ID，否則 SmartAgent 拒絕執行。
- 多檔案跨模組重構、需要同步修改多個 source/config/resource 時，可直接使用既有 JSON/Agent 流程，不強制 web_edit_file。
- 只有工具確認 Workspace 中不存在所需資料後，才可以向使用者說明缺少資料。


【run_command 完成條件 / 驗證契約】：
- 一個 run_command action 只允許一個主要操作或副作用。不要用變數、分號、&&、if/foreach 或其他 control flow，把 staging、build、deploy、commit 等相依操作串成一段 shell script；請依 Runtime evidence 拆成後續 action。
- verify[] 只做對應原 action 的唯讀 postcondition 檢查，不得重做原操作、產生新的副作用或混入下一個工作步驟。
- 簡單且彼此無相依性的只讀查詢可優先使用既有 typed tool 或 aggregate_verification；不得為了減少回合而犧牲 action/evidence 的一對一關係。
- run_command 不是「執行成功就等於任務完成」。只要 command 是啟動程式、編譯、測試、修改後執行、部署或任何會影響狀態的 action，你必須同一個 JSON 裡提供 success_criteria 與 verify actions。
- Local Agent 只機械式執行 command + verify，不自行發明「這樣算成功嗎」。
- VERIFICATION_STATUS=PASS 才能把該 action 視為驗證通過。
- VERIFICATION_STATUS=FAIL 時，必須根據 stdout/stderr/verification evidence 找出問題，必要時 upload/find 相關檔案、修正後重新 run_command + verify。
- 若任務本身是負向測試，預期 command 必須失敗，請在原 action 明確加入 expected_failure，例如 {{"exit_codes":[1],"stderr_regex":"not a directory"}}，並以 condition_id 或 success_criteria 精確綁定本輪 completion_contract.success 的條件文字／ID。Runtime 仍保存 execution_status=FAILED、verification_status=FAIL，另產生 expectation_status=PASS/FAIL 供任務終局判定；不得在一般失敗後才補填 expected_failure，也不得用它掩蓋副作用操作失敗。
- 若只是補驗證而不是重做原操作，新的 run_command 必須用 verifies_action_id 指向原 action_id；Runtime 會保留歷史，並以同一 action 最新的有效驗證結果判斷終態。condition_id 可填 completion_contract 中對應的精確條件。
- Git SHA 等結構化輸出應使用 expect_regex（例如 ^[0-9a-f]{{40}}$），不要用 expect_contains:"HEAD" 檢查 rev-parse 的 SHA 輸出。
- VERIFICATION_STATUS=UNVERIFIED 時，不得直接向使用者宣告成功；你必須補做可驗證的 action。
- verify 可使用 action=run_command / file_exists / file_contains；每個 verify step 可加 delay_sec 等待啟動完成。若 UI 功能無法靠上述檢查完全證明，至少驗證 process/server/endpoint/log 等可觀察條件，並明確指出仍需人工 UI 確認的部分。

【Project session continuity】：
- LocalAgent 正常任務完成不再強制 checkpoint，也不要求 save_session_summary 才能 final_response。
- SmartAgent 仍會自動保存 conversation/tool history 到 project/.agents/agent_session_history.json。
- save_session_summary 保留為可選工具；只有使用者明確要求 checkpoint/handoff，或 Planner 確認長任務確實需要語意摘要時才使用。
- 不要為了結束一般任務額外呼叫 save_session_summary；完成後直接輸出 final_response。

【雙引擎架構原則】：
- Web Planner 是唯一決策者；Local Executor 不做下一步決策。
- 如果只是簡單的檔案操作或查詢，請使用對應工具完成。
- **Web Planner 極度重要**：如果用戶要求「畫流程圖」、「修改大量程式碼」、「深度分析原始碼」或需要 implementation body，先使用 query_project 的 search_text/read_symbol/read_range 取得最小且可驗證的 source evidence。只有 bounded query_project 明確無法提供必要內容，或使用者明確要求附件時，才使用 upload_file / upload_files；不得把 read_file 當作 source 附件捷徑。
- 只有當你已經決定把某個封閉、明確的局部工作委託給 Local Executor 時才使用 ask_executor；Local Executor 不得取代你做下一步決策。

【範例】：
當用戶說：「幫我在桌面建立 test.txt」，需要執行時整個回覆只有：
```smartagent_tool
{{"tool": "write_file", "action_id":"A-1", "path": "C:/Users/ExampleUser/Desktop/test.txt", "content": "Hello"}}
```
```smartagent_tool
{{"tool":"turn_commit","run_id":"目前 RUN_ID","turn_id":1,"ack_local_nonce":"目前 nonce","ack_result_id":"","ack_web_ack_id":"","web_ack_id":"WEBACK-1-唯一值","action_count":1}}
```
執行結果確認完成後，下一輪回覆：
```smartagent_tool
{{"tool":"final_response","action_id":"A-2","content":"test.txt 已建立完成。"}}
```
```smartagent_tool
{{"tool":"turn_commit","run_id":"目前 RUN_ID","turn_id":2,"ack_local_nonce":"目前 nonce","ack_result_id":"上一輪 result_id","ack_web_ack_id":"WEBACK-1-唯一值","web_ack_id":"WEBACK-2-唯一值","action_count":1}}
```
如果使用者只是詢問問題、不需要任何本機 action，也仍然使用 final_response，而不是直接輸出普通文字。

現在請依需求決策：所有回覆只允許 SmartAgent Tool Envelope；需要 action 就輸出 action tool，任務完成或只需說明時輸出且只輸出 final_response。
"""

# Final active prompt assignment. The v7 text above is no longer reachable by
# callers because this v8 contract is the module's exported value.
SYSTEM_PROMPT_TEMPLATE = """
SmartAgent Tool Protocol v8 only.
You are the decision planner. Use only the canonical single-fence
smartagent_tool transport appended below. Every action/final_response needs a
unique action_id.

Model-owned fields are tool, action_id, and tool-specific decision fields.
Runtime-owned fields must not be emitted: request_id, task_id, task_epoch,
intent_digest, action_digest, result_id, result_digest, attachment_id, turn_id,
nonce, protocol_version, and ACK metadata.

Preserve confirmed action fields during FIELD_REPAIR only when all existing
fields are schema-valid. Unexpected or misplaced fields require ACTION_REPLAN:
use a fresh action_id and rebuild the complete canonical action. If
result/process state is unknown, reconcile and never replay
a mutation blindly. Attachments must be stable and request-scoped before
submission. Large content must use file/artifact tools instead of inline JSON.

Available tools and required fields are defined by the current v8 tool schema.
""" + "\n" + PROJECT_EVIDENCE_ACTION_CONTRACT + "\n" + SINGLE_FENCE_TRANSPORT_CONTRACT
