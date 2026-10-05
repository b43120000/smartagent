#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared progress/event primitives used by LocalAgent and RemoteAgent."""
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Optional

PROGRESS_EVENTS = (
    "TASK_ACCEPTED", "PLANNING", "READING", "EDITING",
    "TOOL_START", "TOOL_COMPLETE", "VERIFYING", "WAITING_WEB",
    "COMPLETED", "FAILED",
)

_PROGRESS_VOLATILE_PATTERNS = (
    (re.compile(r'(?i)(upload_cache[\\/][^\\/\r\n]+?)_\d{8}_\d{6}(?:_\d+)?(?=\.[A-Za-z0-9]+)'), r'\1'),
    (re.compile(r'(?i)\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b'), '<elapsed>'),
)


def _normalize_progress_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    for pattern, replacement in _PROGRESS_VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _stable_progress_value(value: object):
    """Recursively normalize volatile presentation text without hiding real evidence."""
    if isinstance(value, dict):
        return {str(k): _stable_progress_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable_progress_value(v) for v in value]
    if isinstance(value, str):
        return _normalize_progress_text(value)
    return value


def _fingerprint_progress_value(value: object) -> str:
    stable = _stable_progress_value(value)
    try:
        payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = repr(stable)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _compact_path_name(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return "(未指定)"
    try:
        return Path(value).name or value
    except Exception:
        return value


def _tool_progress_descriptor(call: dict) -> dict:
    tool = str(call.get("tool", "") or "(unknown)")
    desc = {"tool": tool}

    if tool in ("upload_file", "read_file", "write_file", "web_edit_file"):
        desc["path"] = str(call.get("path", ""))
    elif tool == "download_artifact":
        desc["output_path"] = str(call.get("output_path", ""))
        desc["expected_filename"] = str(call.get("expected_filename", ""))
    elif tool == "upload_files":
        desc["paths"] = [str(x) for x in (call.get("paths") or [])]
    elif tool == "run_command":
        desc["command"] = str(call.get("command", ""))
        if "verify" in call:
            desc["verify"] = call.get("verify")
        if call.get("success_criteria"):
            desc["success_criteria"] = str(call.get("success_criteria"))
    elif tool == "find_file":
        desc["name"] = str(call.get("name", ""))
        desc["root"] = str(call.get("root", ""))
    elif tool == "list_directory":
        desc["path"] = str(call.get("path", "."))
    elif tool == "web_search":
        desc["query"] = str(call.get("query", ""))
    elif tool == "save_session_summary":
        desc["summary_target"] = "project_session_summary"
    elif tool == "final_response":
        desc["content_sha256"] = _fingerprint_progress_value(call.get("content", ""))
    elif tool == "ask_executor":
        desc["instruction_sha256"] = _fingerprint_progress_value(call.get("instruction", ""))
        desc["context_sha256"] = _fingerprint_progress_value(call.get("context", ""))
    else:
        for key in ("path", "paths", "command", "name", "root", "query", "output_path"):
            if key in call:
                desc[key] = call.get(key)

    if tool == "write_file":
        desc["content_sha256"] = _fingerprint_progress_value(call.get("content", ""))
    if tool == "web_edit_file":
        desc["instruction_sha256"] = _fingerprint_progress_value(call.get("instruction", ""))
        if call.get("output_path") is not None:
            desc["output_path"] = str(call.get("output_path"))
    return desc


def _derive_action_title(tool_calls: list | None = None, branch: str = "") -> str:
    branch_titles = {
        "WAIT_PLANNER": "等待 Planner 決策",
        "MALFORMED_TOOL_RETRY": "修正 Tool Envelope JSON",
        "REJECTED_TOOL_RETRY": "修正 Tool Envelope",
        "VERIFICATION_GATE": "補做驗證",
        "FINAL_RESPONSE_PROTOCOL_RETRY": "將一般回覆封裝為 final_response",
        "FINAL_RESPONSE": "完成回覆",
    }
    if branch in branch_titles:
        return branch_titles[branch]

    calls = list(tool_calls or [])
    if not calls:
        return "等待下一步"

    def one(call: dict) -> str:
        tool = str(call.get("tool", "") or "(unknown)")
        if tool == "web_edit_file":
            return f"修改 {_compact_path_name(call.get('path'))}"
        if tool == "download_artifact":
            return f"下載 WebGPT artifact 到 {_compact_path_name(call.get('output_path'))}"
        if tool == "write_file":
            return f"寫入 {_compact_path_name(call.get('path'))}"
        if tool == "read_file":
            return f"讀取 {_compact_path_name(call.get('path'))}"
        if tool == "upload_file":
            return f"上傳 {_compact_path_name(call.get('path'))}"
        if tool == "upload_files":
            return f"上傳 {len(call.get('paths') or [])} 個附件"
        if tool == "run_command":
            command = " ".join(str(call.get("command", "")).split())
            prefix = "執行驗證" if call.get("verify") else "執行指令"
            if len(command) > 58:
                command = command[:55] + "..."
            return f"{prefix}: {command or '(空指令)'}"
        if tool == "find_file":
            return f"尋找 {call.get('name', '(未指定)')}"
        if tool == "list_directory":
            return f"檢查目錄 {call.get('path', '.')}"
        if tool == "web_search":
            query = " ".join(str(call.get("query", "")).split())
            return f"搜尋: {query[:60]}"
        if tool == "save_session_summary":
            return "保存工作階段摘要"
        if tool == "final_response":
            return "完成回覆"
        if tool == "ask_executor":
            return "Local AI fallback"
        return f"執行 {tool}"

    titles = [one(call) for call in calls]
    if len(titles) == 1:
        return titles[0]
    return " + ".join(titles[:2]) + (f" +{len(titles)-2}" if len(titles) > 2 else "")


def _progress_signature(
    action_title: str,
    tool_calls: list | None = None,
    result_evidence: object = None,
    *,
    branch: str = "",
    diagnostics: object = None,
) -> str:
    canonical = {
        "action_title": str(action_title or "").strip(),
        "branch": str(branch or ""),
        "tools": [_tool_progress_descriptor(c) for c in (tool_calls or [])],
        "result_evidence_sha256": _fingerprint_progress_value(result_evidence),
        "diagnostics_sha256": _fingerprint_progress_value(diagnostics),
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


class _ProgressLoopGuard:
    def __init__(self, threshold: int = 3):
        self.threshold = max(2, int(threshold))
        self.last_signature: Optional[str] = None
        self.repeat_count = 0

    def record(self, signature: str) -> bool:
        if signature == self.last_signature:
            self.repeat_count += 1
        else:
            self.last_signature = signature
            self.repeat_count = 1
        return self.repeat_count >= self.threshold


def _set_event_state(event: threading.Event, active: bool) -> None:
    if active:
        event.set()
    else:
        event.clear()


def run_progress_control_self_tests() -> dict:
    results: dict[str, bool] = {}

    guard = _ProgressLoopGuard(threshold=3)
    sig_a = _progress_signature(
        "寫入 a.py",
        [{"tool": "write_file", "path": "C:/tmp/a.py", "content": "one"}],
        "[成功] 寫入 3 bytes 到 C:/tmp/a.py",
    )
    results["same_signature_1_not_stalled"] = guard.record(sig_a) is False
    results["same_signature_2_not_stalled"] = guard.record(sig_a) is False
    results["same_signature_3_stalled"] = guard.record(sig_a) is True

    guard = _ProgressLoopGuard(threshold=3)
    guard.record(sig_a)
    guard.record(sig_a)
    sig_changed_evidence = _progress_signature(
        "寫入 a.py",
        [{"tool": "write_file", "path": "C:/tmp/a.py", "content": "one"}],
        "[成功] 寫入 4 bytes 到 C:/tmp/a.py",
    )
    results["changed_evidence_resets"] = (
        guard.record(sig_changed_evidence) is False and guard.repeat_count == 1
    )

    guard = _ProgressLoopGuard(threshold=3)
    guard.record(sig_a)
    guard.record(sig_a)
    sig_changed_target = _progress_signature(
        "寫入 b.py",
        [{"tool": "write_file", "path": "C:/tmp/b.py", "content": "one"}],
        "[成功] 寫入 3 bytes 到 C:/tmp/b.py",
    )
    results["changed_target_action_resets"] = (
        guard.record(sig_changed_target) is False and guard.repeat_count == 1
    )

    pause_event = threading.Event()
    _set_event_state(pause_event, True)
    pause_set = pause_event.is_set()
    _set_event_state(pause_event, False)
    results["pause_event_set_clear"] = pause_set and not pause_event.is_set()

    sig_command_a = _progress_signature(
        "執行指令: echo A",
        [{"tool": "run_command", "command": "echo A"}],
        "[COMMAND_RESULT]\\nstdout: A",
    )
    sig_command_b = _progress_signature(
        "執行指令: echo B",
        [{"tool": "run_command", "command": "echo B"}],
        "[COMMAND_RESULT]\\nstdout: B",
    )
    results["changed_command_changes_signature"] = sig_command_a != sig_command_b

    sig_diag_a = _progress_signature(
        "修正 Tool Envelope JSON",
        branch="MALFORMED_TOOL_RETRY",
        result_evidence="json_decode_error line=1 column=20",
        diagnostics=[{"reason": "json_decode_error", "line": 1, "column": 20}],
    )
    sig_diag_b = _progress_signature(
        "修正 Tool Envelope JSON",
        branch="MALFORMED_TOOL_RETRY",
        result_evidence="json_decode_error line=1 column=21",
        diagnostics=[{"reason": "json_decode_error", "line": 1, "column": 21}],
    )
    results["changed_parser_diagnostic_changes_signature"] = sig_diag_a != sig_diag_b

    volatile_a = _fingerprint_progress_value([
        "C:/x/.agents/upload_cache/a_20260818_210101.py",
        "AGENT 完成 1.2s",
    ])
    volatile_b = _fingerprint_progress_value([
        "C:/x/.agents/upload_cache/a_20260818_210202.py",
        "AGENT 完成 9.8s",
    ])
    results["volatile_timestamp_elapsed_ignored"] = volatile_a == volatile_b

    results["all_passed"] = all(results.values())
    return results
