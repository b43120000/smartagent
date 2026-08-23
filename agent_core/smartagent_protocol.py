#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared SmartAgent Tool Envelope protocol.

Single source of truth for schema, parser, transport guards, ACK validation,
and the canonical SmartAgent planner protocol prompt.
"""
import json
import re

def _repair_llm_json(candidate: str) -> str:
    """Best-effort repair for common LLM JSON mistakes on Windows paths.

    Valid JSON is always attempted first.  The compatibility pass only doubles
    isolated backslashes so visually normal C:/Users/... paths can still decode.
    """
    return re.sub(r'(?<!\\)\\(?!\\)', r'\\\\', candidate)


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
        "required": {"command": str},
        "optional": {
            "timeout": int,
            "verify": (list, dict),
            "success_criteria": str,
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
        "optional": {"workspace": str, "base_snapshot_id": str, "max_bytes": int, "max_files": int},
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
        "optional": {},
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

    A valid original payload wins.  If it fails, the legacy Windows single-
    backslash repair is attempted.  When both fail, the original error location
    is preserved and the repair-pass error is included as secondary evidence.
    """
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as original_error:
        repaired = _repair_llm_json(candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired), None
            except json.JSONDecodeError as repaired_error:
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
                    repair_error={
                        "message": repaired_error.msg,
                        "line": repaired_error.lineno,
                        "column": repaired_error.colno,
                        "position": repaired_error.pos,
                    },
                )
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
                "Tool Envelope 只承載 control-plane command；source/script/payload 請改走附件、"
                "artifact 或既有檔案，再用短命令執行。"
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
        "calls": [],
        "diagnostics": [],
    }
    if not stripped:
        return report

    payloads, exclusive, transport_kind = _extract_tool_transport(stripped)
    report["transport_kind"] = transport_kind

    if payloads:
        report["intended"] = True
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

        if decoded not in valid_calls:
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
    single_backslash_json = (
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
            "text": '```smartagent_tool\n{"tool":"upload_file","path":"C:/Users/user/a.py"}\n```',
            "calls": 1,
        },
        "windows_escaped_backslash": {
            "text": escaped_backslash_json,
            "calls": 1,
        },
        "windows_single_backslash_repair": {
            "text": single_backslash_json,
            "calls": 1,
        },
    }

    results = {}
    all_passed = True

    for name, spec in cases.items():
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
- If trusted route says ContextSync=AUTO, WebGPT is the sole component that selects NONE, DIRECT, DELTA, or FULL_BUNDLE from task scope and available project evidence.
- LocalAgent must never semantically choose sync strategy from file count, repository size, or task wording. It only executes the explicit strategy/tool calls selected by WebGPT.
- Explicit /bundle, /project-sync, or 完整 project sync means FULL_BUNDLE and must be honored.
- Prefer NONE/DIRECT for lightweight isolated work, DELTA when a known persistent base snapshot exists and freshness requires changed source only, FULL_BUNDLE when project-level engineering work needs complete current source ground truth.
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
- action envelope 必須保持短小；禁止把完整 source、patch 或複雜 shell script 塞進 JSON。final_response 可承載正常人類回覆。大型全新文字/source 使用 chunk protocol；既有檔案優先 web_edit_file，已有網頁產物優先 download_artifact。

【可用工具格式】：

1. 執行指令＋驗證：{{"tool": "run_command", "command": "PowerShell指令", "timeout": 30, "success_criteria": "什麼條件代表這次動作真的生效", "verify": [{{"action":"run_command","command":"驗證指令","expect_exit_code":0,"expect_contains":"可選關鍵字"}}, {{"action":"file_exists","path":"檔案路徑","expect":true}}, {{"action":"file_contains","path":"檔案路徑","text":"應存在內容","expect":true}}]}}
2. 讀取檔案（僅 Local/Cloud Planner 使用；Web Planner 禁止使用）：{{"tool": "read_file", "path": "絕對路徑"}}
3. 寫入小型檔案（content 最多 4096 字元）：{{"tool": "write_file", "path": "絕對路徑", "content": "完整檔案內容"}}
3a. 開始大型新檔交易：{{"tool":"begin_file_write","action_id":"A-BEGIN-唯一值","write_id":"WRITE-唯一值","path":"絕對路徑","encoding":"utf-8","overwrite":true}}
3b. 依序寫入一段：{{"tool":"write_file_chunk","action_id":"A-CHUNK-唯一值","write_id":"WRITE-同上","content":"本段內容"}}
3c. 提交大型檔案：{{"tool":"commit_file_write","action_id":"A-COMMIT-唯一值","write_id":"WRITE-同上"}}
3d. 取消大型檔案：{{"tool":"abort_file_write","action_id":"A-ABORT-唯一值","write_id":"WRITE-同上"}}
4. 列出目錄第一層：{{"tool": "list_directory", "path": "目錄路徑"}}
5. 一次統計一或多個目錄：{{"tool":"inspect_directory","paths":["C:/path1","D:/path2"],"recursive":true,"sample_limit":20}}
6. 尋找檔案：{{"tool": "find_file", "name": "檔名", "root": "可省略；預設 Workspace Root"}}
7. 上傳單一附件到目前網頁對話：{{"tool": "upload_file", "path": "絕對路徑"}}
8. 上傳多個附件到目前網頁對話：{{"tool": "upload_files", "paths": ["絕對路徑1", "絕對路徑2"]}}
9. 網路搜尋：{{"tool": "web_search", "query": "關鍵字"}}
9. Local AI 最後手段：{{"tool": "ask_executor", "instruction": "明確且封閉的局部工作", "context": "必要資訊", "allow_local_ai_fallback": true, "run_id": "必須等於目前 RUN_ID"}}
10. 保存本次 project session summary：{{"tool":"save_session_summary","summary":"本次已理解/完成內容","decisions":["重要架構決策"],"modified_files":["路徑"],"verification":["驗證與結果"],"pending":["未完成/風險"],"next_steps":["下次可直接接續的事項"]}}
11. Web 直接修改單一檔案：{{"tool":"web_edit_file","path":"絕對路徑","instruction":"要對此檔案做的修改","output_path":"可省略；預設覆寫原檔"}}
12. 下載本輪 WebGPT 已生成完成的檔案/圖片：{{"tool":"download_artifact","output_path":"本機目的路徑或目錄","expected_filename":"可省略；已知檔名時填入","timeout":45}}
13. 完成並回覆使用者：{{"tool":"final_response","action_id":"A-唯一值","content":"顯示在 CMD 的最終回覆"}}
14. 回覆提交 ACK（每輪最後必須有）：{{"tool":"turn_commit","run_id":"目前 RUN_ID","turn_id":1,"ack_local_nonce":"Local Commit nonce","ack_result_id":"上一輪 result_id 或空字串","ack_web_ack_id":"Local Commit 的 ack_web_ack_id","web_ack_id":"本輪新唯一值","action_count":1}}

【JSON / Windows 路徑規則】：
- JSON 中的 Windows 路徑優先使用正斜線，例如 C:/Users/user/Desktop/tool/file.py。
- 若使用反斜線，必須依 JSON 規則寫成雙反斜線，例如 C:\\Users\\user\\Desktop\\tool\\file.py。
- 絕對不要輸出 JSON 內的單反斜線 Windows path，例如 C:\\Users\\user，否則 JSON parser 會失敗。

【Workspace / 附件規則】：
- 使用者提供本機目錄時，該目錄視為本次 Workspace Root。
- 只要需求是統計、摘要或比較一個或多個目錄，優先用單一 inspect_directory action 一次取得結構化證據；不要使用 ask_executor，也不要拆成多個 list_directory 或 run_command 回合。此工具只能讀取使用者在本輪明確提供的絕對路徑或目前 Workspace Root。
- 你看不到某檔案，不代表檔案不存在；禁止要求使用者自行補檔。
- 若 trace 時遇到 import/include、其他 module、config、resource、圖片、PDF、Word、PPT 或其他相依檔案，先使用 list_directory / find_file 找到它。
- 【Web Planner 強制附件優先】只要你需要查看、理解、分析任何完整本機檔案，不論是 source code、txt/log、圖片、PDF、Word、PowerPoint、Excel 或其他格式，一律使用 upload_file / upload_files，把真實檔案附加到目前同一個網頁對話。
- Web Planner 不得使用 read_file 取得完整檔案內容，也不得要求 Agent 把完整檔案轉成文字貼回對話。
- 若你只知道檔名或相依模組名稱，先使用 find_file 找到實際路徑，再使用 upload_file。
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
- run_command 不是「執行成功就等於任務完成」。只要 command 是啟動程式、編譯、測試、修改後執行、部署或任何會影響狀態的 action，你必須同一個 JSON 裡提供 success_criteria 與 verify actions。
- Local Agent 只機械式執行 command + verify，不自行發明「這樣算成功嗎」。
- VERIFICATION_STATUS=PASS 才能把該 action 視為驗證通過。
- VERIFICATION_STATUS=FAIL 時，必須根據 stdout/stderr/verification evidence 找出問題，必要時 upload/find 相關檔案、修正後重新 run_command + verify。
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
- **Web Planner 極度重要**：如果用戶要求「畫流程圖」、「修改大量程式碼」、「深度分析原始碼」或任何需要查看完整檔案的工作，先使用 upload_file / upload_files 將必要檔案附加到目前網頁對話，由你直接閱讀附件並決定下一步；不要先 read_file 把原始內容貼成文字。
- 只有當你已經決定把某個封閉、明確的局部工作委託給 Local Executor 時才使用 ask_executor；Local Executor 不得取代你做下一步決策。

【範例】：
當用戶說：「幫我在桌面建立 test.txt」，需要執行時整個回覆只有：
```smartagent_tool
{{"tool": "write_file", "action_id":"A-1", "path": "C:/Users/user/Desktop/test.txt", "content": "Hello"}}
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
