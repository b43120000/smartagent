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
    r"deploy|execute|run|compile|test|commit|push|pull|open\s+file|"
    r"read\s+file|save\s+file|send\s+email|send\s+message|"
    r"modify\s+file|write\s+file|create\s+file|delete\s+file)\b|"
    r"今天|明天|昨天|現在|最新|近期|即時|目前|搜尋|上網|網路|"
    r"查詢|匯率|天氣|股價|檔案|資料夾|專案|原始碼|程式庫|"
    r"工作區|讀取|開啟|下載|上傳|安裝|編譯|執行|測試|"
    r"修改|建立|儲存|刪除|寄送|發送|部署|推送|提交|"
    r"真的|實際確認|驗證過)",
    re.IGNORECASE,
)
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
    if _EXTERNAL_OR_ACTION.search(text):
        return False, "external_information_or_action_indicated"
    if not _TEXT_INTENT.search(text):
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
           "accept_terminal_candidate"]
