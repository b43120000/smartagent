"""Tri-One Phase 1 / 5A: fail-closed answer-only eligibility.

This module has no execution authority. It only rejects cases that cannot
safely use an answer-only terminal. Final admission is owned by the V9
protocol, Progress, Action Ledger and terminal receipt gates.

The allowlist is intentionally narrow: a text-only classifier cannot prove
arbitrary natural-language requests need no external information.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

FEATURE_FLAG = "TRI_ONE_TEXT_ONLY_V1"
_PHASE1_ORIGINS = frozenset({"TELEGRAM", "LOCAL_TEST", "WEB_COPILOT", "WEB_DIRECT"})

_TEXT_INTENT = re.compile(
    r"^\s*(?:(?:please\s+)?(?:explain|define|describe|rewrite|rephrase|"
    r"translate|paraphrase|compare)\b|(?:what\s+(?:is|are)\b)|"
    r"(?:how\s+does\b)|(?:請|幫我|請你)?\s*(?:解釋|說明|介紹|翻譯|改寫|"
    r"潤飾|比較|解讀|將以下文字|把以下文字)|(?:什麼是|何謂))",
    re.IGNORECASE,
)
# Fail closed on current/remote information, actions, project sources, or
# mixed operational intent. No substring here *grants* a privilege.
_EXTERNAL_OR_ACTION = re.compile(
    r"(?:\b(?:today|tonight|yesterday|tomorrow|latest|current|recent|"
    r"now|live|real[\s-]?time|weather|exchange[\s-]?rate|stock[\s-]?price|"
    r"search|browse|google|web|internet|website|github|repo(?:sitory)?|"
    r"workspace|filesystem|folder|directory|download|upload|install|"
    r"project|codebase|attached|attachment|account|profile|session|"
    r"logs?|files?|my|our|private|"
    r"deploy|execute|run|compile|test|commit|push|pull|open\s+file|"
    r"read\s+file|save\s+file|send\s+email|send\s+message|"
    r"modify\s+file|write\s+file|create\s+file|delete\s+file)\b|"
    r"今天|明天|昨天|現在|最新|近期|即時|目前|搜尋|上網|網路|"
    r"我的|我們的|帳戶|帳號|個人資料|附件|日誌|紀錄|"
    r"查詢|匯率|天氣|股價|檔案|資料夾|專案|原始碼|程式庫|"
    r"工作區|讀取|開啟|下載|上傳|安裝|編譯|執行|測試|"
    r"修改|建立|儲存|刪除|寄送|發送|部署|推送|提交|"
    r"真的|實際確認|驗證過)",
    re.IGNORECASE,
)
# Only remove self-contained tool-prohibition clauses from eligibility text.
# Never remove a positive operational requirement such as "read README".
# This is not evidence of correctness and grants no tool execution privilege.
_NO_TOOL_CLAUSE = re.compile(
    r"(?:本任務\s*)?(?:不需要|無需|不必|不要|不得|禁止)\s*"
    r"(?:操作(?:本機)?檔案或執行任何命令|使用任何工具|使用工具|"
    r"執行任何命令|執行本機命令|執行命令|操作本機檔案|操作檔案)"
    r"|(?:do\s+not|don't|no\s+need\s+to)\s+"
    r"(?:execute\s+(?:any\s+)?(?:local\s+)?commands?\s+or\s+"
    r"(?:modify|access)\s+files?|use\s+(?:any\s+)?tools?|"
    r"execute\s+(?:any\s+)?(?:local\s+)?commands?|"
    r"modify\s+(?:any\s+)?files?)",
    re.IGNORECASE,
)
# Unlike a request merely prohibiting a shell command, this explicitly
# denies all operational tools. The entire operational scope is authoritative
# even when answer-only feature flags are OFF.
_NO_ANY_TOOL = re.compile(
    r"(?:本任務\s*)?(?:不需要|無需|不必|不要|不得|禁止)\s*"
    r"(?:使用(?:任何)?工具|操作(?:本機)?檔案或執行任何命令)"
    r"|(?:do\s+not|don't)\s+(?:use\s+(?:any\s+)?tools?|"
    r"execute\s+(?:any\s+)?(?:local\s+)?commands?\s+or\s+"
    r"(?:modify|access)\s+files?)",
    re.IGNORECASE,
)
_NO_COMMAND = re.compile(
    r"(?:不需要|無需|不必|不要|不得|禁止)\s*執行(?:任何|本機)?命令"
    r"|(?:do\s+not|don't)\s+execute\s+(?:any\s+)?(?:local\s+)?commands?",
    re.IGNORECASE,
)


def explicit_tool_restrictions(request: str) -> tuple[bool, bool]:
    """Trusted original request restrictions: (no_all_tools, no_commands)."""
    text = str(request or "")
    no_tools = bool(_NO_ANY_TOOL.search(text))
    no_commands = bool(no_tools or _NO_COMMAND.search(text))
    return no_tools, no_commands


def tool_restriction_diagnostic(request: str, actions) -> dict | None:
    """No model action may override explicit, original user tool restrictions."""
    no_tools, no_commands = explicit_tool_restrictions(request)
    for action in actions:
        tool = str(action.get("tool", "") or "") if isinstance(action, Mapping) else ""
        if (no_tools and tool not in {"report_progress", "final_response", "turn_commit"}
                or no_commands and tool == "run_command"):
            return {
                "marker": "[WEBAGENT_USER_TOOL_RESTRICTION]",
                "reason": "text_only_tool_forbidden",
                "detail": "original request prohibits the proposed operational tool",
                "suggestion": (
                    "不得執行該工具，也不得為 Terminal Verification 補用 run_command。"
                    "純文字任務應提供 canonical report_progress + final_response + turn_commit；"
                    "若確實需要外部資料，僅能說明無法驗證，不得虛構結果。"
                ),
            }
    return None


_URI_OR_PATH = re.compile(
    r"(?:https?://|www\.|[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|"
    r"(?:^|[\s(])(?:\.{1,2}[/\\]|~/))", re.IGNORECASE,
)
_UNSUPPORTED_CLAIM = re.compile(
    r"\b(?:I\s+(?:checked|verified|ran|executed|saved|sent|downloaded|"
    r"uploaded|read\s+the\s+file|opened)|"
    r"(?:has|have)\s+been\s+(?:saved|sent|uploaded|downloaded|executed))\b|"
    r"已(?:查詢|確認|執行|儲存|寄送|發送|下載|上傳|修改|驗證)",
    re.IGNORECASE,
)


def enabled(env: Mapping[str, str] | None = None, *, source: str = "") -> bool:
    flags = os.environ if env is None else env
    if str(flags.get(FEATURE_FLAG, "")).strip().lower() not in {"1", "true", "yes"}:
        return False
    normalized = str(source or "").strip().upper().replace("-", "_")
    if normalized not in _PHASE1_ORIGINS:
        return False
    override = str(flags.get("TRI_ONE_TEXT_ONLY_" + normalized, "")).strip().lower()
    return override not in {"0", "off", "false", "no"}


def classify_original_request(
    request: str,
    *,
    attachments: bool = False,
    authorized_paths: bool = False,
    image_or_artifact_request: bool = False,
    continuation: bool = False,
) -> tuple[bool, str]:
    """Conservatively preclassify trusted original input; default deny.

    This is one input to terminal admission, not an LLM or tool verification.
    """
    if attachments or authorized_paths or image_or_artifact_request or continuation:
        return False, "structured_tool_or_continuation_required"
    text = str(request or "").strip()
    if not text or len(text) > 12000:
        return False, "empty_or_unbounded_input"
    if _URI_OR_PATH.search(text):
        return False, "external_resource_or_path"
    # The *prohibition itself* is not a positive request to access files or
    # run commands. Positive tool/fresh-information intent remains untouched.
    intent_text = _NO_TOOL_CLAUSE.sub(" ", text)
    if _EXTERNAL_OR_ACTION.search(intent_text):
        return False, "external_information_or_action_indicated"
    if not _TEXT_INTENT.search(intent_text):
        return False, "text_intent_not_unambiguously_classified"
    return True, "bounded_text_only_intent"


def accept_terminal_candidate(loop: object, progress: Mapping[str, object],
                              final_action: Mapping[str, object]) -> tuple[bool, str]:
    """Read Runtime-owned state only; never create evidence or a receipt."""
    if str(progress.get("decision") or "").upper() != "COMPLETE" or (
        str(progress.get("outcome") or "").upper() != "SUCCESS"
    ):
        return False, "not_successful_terminal"
    if str(final_action.get("tool") or "") != "final_response" or not str(
        final_action.get("content") or ""
    ).strip():
        return False, "missing_final_text"
    if list(progress.get("evidence_refs") or []):
        return False, "action_evidence_cannot_be_answer_only"
    if any((
        getattr(loop, "action_ledger", None),
        getattr(loop, "action_result_ledger", None),
        getattr(loop, "condition_result_ledger", None),
        getattr(loop, "pending_verification_requirement", None),
        getattr(loop, "pending_result_recovery", None),
        getattr(loop, "deferred_action_repairs", None),
        getattr(loop, "pending_progress_repair_actions", None),
        getattr(loop, "pending_action_repair", None),
        getattr(loop, "active_stage_manifest", None),
    )):
        return False, "runtime_actions_or_recovery_pending"
    if any(not item.get("satisfied") for item in (
        getattr(loop, "action_loop_state", {}) or {}
    ).get("blockers", []) if isinstance(item, Mapping)):
        return False, "runtime_blocker_unresolved"
    if getattr(loop, "continuation_source", None):
        return False, "continuation_requires_normal_route"
    if getattr(loop, "_tri_one_has_attached_context", False):
        return False, "attachment_or_skill_requires_normal_route"
    eligible, reason = classify_original_request(
        getattr(loop, "task_request", ""),
        authorized_paths=bool(getattr(loop, "authorized_paths", [])),
        image_or_artifact_request=bool(getattr(loop, "image_delivery_plan", {})),
    )
    if not eligible:
        return False, reason
    if _UNSUPPORTED_CLAIM.search(str(final_action.get("content") or "")):
        return False, "final_text_claims_unverified_external_action"
    return True, "ANSWER_ONLY_RESPONSE_ADMITTED"


__all__ = ["FEATURE_FLAG", "enabled", "classify_original_request",
           "accept_terminal_candidate", "explicit_tool_restrictions",
           "tool_restriction_diagnostic"]
