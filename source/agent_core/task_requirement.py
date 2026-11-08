"""PR-0 request requirements: conservative, side-effect-free routing metadata.

Never grants permission, executes a tool, verifies a prerequisite or issues
a terminal receipt. Unknown intent remains AMBIGUOUS for normal V9 planning.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_TASK_REQUIREMENT_V1"
FEATURE_FLAG = "SMARTAGENT_PR0_V1"
_TEXT = re.compile(
    r"^\s*(?:(?:please\s+)?(?:explain|define|describe|rewrite|rephrase|"
    r"translate|paraphrase|compare|summarize|summarise|list|outline|draft|"
    r"write|review|what\s+(?:is|are)|how\s+(?:to|does))\b|"
    r"(?:請|幫我|請你|請幫我|可以幫我)?\s*(?:解釋|說明|介紹|翻譯|改寫|"
    r"潤飾|比較|解讀|整理|總結|摘要|列出|撰寫|寫(?:一段|一個|出)?|"
    r"提供|分析|review|Review|檢視以下|檢查以下)|什麼是|何謂)",
    re.I,
)
# Creative text generated entirely in the reply (not a file or action).
# Keep this request-local, never use it to authorize a tool or external source.
_CREATIVE_TEXT = re.compile(
    r"^\s*(?:(?:請|請你|幫我|請幫我|麻煩你)\s*)?"
    r"(?:說|講)\s*(?:一個|個|一段|段)?"
    r"(?:[^。；;\n]{0,100}?)?(?:笑話|故事)"
    r"(?:[。！!\s]|$)"
    r"|^\s*(?:(?:please)\s+)?(?:tell\s+me|make\s+up|write|compose)"
    r"\s+(?:a|an|one)?\s*(?:.{0,80}?\s)?(?:joke|story)\b",
    re.I,
)

_CONCEPT = re.compile(
    r"^\s*(?:(?:please\s+)?(?:explain|define|describe|"
    r"what\s+is|how\s+(?:to|does)|compare)\b|"
    r"(?:請|幫我|請你|請幫我)?\s*(?:解釋|說明|介紹|比較|什麼是|何謂))", re.I,
)
_CHAIN = re.compile(
    r"\b(?:and\s+(?:then\s+)?|then\s+)(?:run|read|write|edit|save|"
    r"download|upload|send|post|check|fetch|execute|test|clone|push|deploy|commit)\b|"
    r"(?:然後|接著|之後|並且|並|再|順便|但|不過|另外|也要|還要|仍要|仍需|還是要|卻要|依然|但是|but)[^。！？\n]{0,40}?"
    r"(?:讀取|閱讀|檢查|修改|編輯|寫入|執行|測試|查詢|"
    r"搜尋|下載|上傳|推送|發送|寄送|建立|刪除|部署|同步|貼到|push|run|test|write|read|send|upload|download|commit)", re.I,
)
_FOLLOWUP_ACTION = re.compile(
    r"(?:[，,。；;！!？?\n])\s*(?:再|另外|同時|接著|然後)?\s*"
    r"(?:請|請你|幫我|請幫我|可以幫我|務必|需要)?"
    r"[^。！？!?\n]{0,40}?(?:讀取|閱讀|檢查|修改|編輯|寫入|執行|測試|"
    r"查詢|搜尋|下載|上傳|推送|發送|寄送|建立|刪除|部署|"
    r"同步|貼到|傳給|寄給|寄到|發給|分享至|通知|安裝|開啟|儲存|驗證|run\b|read\b|"
    r"modify\b|push\b|send\b|upload\b|download\b|commit\b)",
    re.I,
)
_ACTIONS = {
    "READ": re.compile(
        r"\b(?:read|fetch|clone|checkout|inspect|open\s+(?:the\s+)?file|"
        r"review\s+(?:my|the|this)?\s*(?:file|repo|repository|PR))\b|"
        r"(?:讀取|閱讀|查看|檢視|開啟|拉取|下載|同步|查詢|搜尋|"
        r"檢查(?:這個|該|本地|遠端)?(?:檔案|專案|repo|PR))", re.I),
    "WRITE": re.compile(
        r"\b(?:edit|modify|update|delete|overwrite|save|push|"
        r"upload|install|deploy|publish|create\s+(?:a\s+)?(?:file|folder|repo))\b|"
        r"\b(?:git\s+commit|commit\s+(?:changes?|files?|code|this|these|my))\b|"
        r"(?:幫我|請|再|需要)\s*commit\b|"
        r"(?:修改|編輯|寫入|刪除|儲存|上傳|安裝|部署|推送|提交|"
        r"建立(?:檔案|資料夾|專案|repo)|更新(?:檔案|專案|repo))", re.I),
    "EXECUTE": re.compile(r"\b(?:run|execute|compile|build|test|launch)\b|"
                          r"(?:執行|編譯|運行|跑(?:測試|程式|命令|IT)|啟動(?:程式|測試|指令))", re.I),
    "VERIFY": re.compile(r"\b(?:verify|validate)\b|(?:驗證|確認(?:結果|檔案|修改|執行|測試))", re.I),
    "DELIVER": re.compile(r"\b(?:send|email|message|post|share|schedule|remind)\b|"
                           r"(?:寄送|發送|傳送|通知|貼到|寄給|發給|分享至|傳給|排程|提醒我|提醒)", re.I),
}
_CURRENT = re.compile(
    r"\b(?:today|tonight|yesterday|tomorrow|latest|current|recent|"
    r"right\s+now|real[\s-]?time|weather|exchange\s+rate|stock\s+price)\b|"
    r"(?:今天|明天|昨天|最新|近期|即時|目前(?:的|股價|天氣|版本)|"
    r"現在(?:的|有|是)|天氣|匯率|股價)", re.I,
)
_PATH = re.compile(
    r"(?:https?://|www\.|[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|"
    r"(?:^|[\s(])(?:\.{1,2}[/\\]|~/))", re.I,
)
_GITHUB = re.compile(
    r"(?:github(?:\.com)?|gitlab|遠端(?:git|repo|repository|儲存庫)|"
    r"remote\s+(?:git|repo|repository)|pull\s+request|\bPR\s*#\d+)", re.I,
)
_LOCAL = re.compile(
    r"(?:本地|本機|工作目錄|工作區|資料夾|檔案|專案|原始碼|"
    r"local|workspace|file|directory|folder|codebase)", re.I,
)
_EXTERNAL = re.compile(r"(?:\b(?:slack|telegram|email|gmail|calendar|discord|drive)\b|"
                       r"(?:寄給|郵件|行事曆|提醒))", re.I)
_REMOTE_REVIEW = re.compile(
    r"^\s*(?:(?:please\s+)?(?:review|inspect|check|fetch)\b|"
    r"(?:請|幫我|請幫我)?\s*(?:review|檢查|審查|查看|讀取|閱讀|檢視|分析|查詢))", re.I,
)
_INLINE = re.compile(r"(?:以下(?:文字|程式碼|內容)|這段(?:文字|程式碼|內容)|"
                     r"貼上(?:的|來的)?(?:文字|程式碼)|this\s+(?:text|snippet)|"
                     r"the\s+following|\x60{3})", re.I)
_ARTIFACT = re.compile(r"\b(?:powerpoint|presentation|pptx|pdf|spreadsheet|xlsx|"
                       r"image|picture|video)\b|(?:簡報|投影片|試算表|圖片|影片|"
                       r"產生(?:檔案|文件|圖|報表))", re.I)
# Interpret only complete explicit prohibitions as restrictions, never as
# requested actions. Mixed positive/negative clauses remain untouched.
_PROHIBITED_VERB = (
    r"(?:修改|推送|提交|寫入|編輯|刪除|上傳|儲存|部署|安裝|"
    r"執行|讀取|閱讀|下載|測試|編譯|開啟|建立|"
    r"(?:git\s+)?(?:push|commit|modify|edit|write|delete|upload|"
    r"save|execute|run|read|fetch|download|install|deploy)\b)"
)
_PROHIBITED_TARGET = (
    r"(?:任何)?(?:資料|檔案|文件|內容|程式碼|程式|命令|"
    r"分支|repository|repo|data|files?)?"
)
_PROHIBITED_CLAUSE = re.compile(
    r"^\s*(?:不需要|不用|不必|無須|無需|不要|請勿|禁止|不得|不可|不允許)"
    r"\s*(?:實際\s*|真的\s*|自行\s*|額外\s*)?"
    + _PROHIBITED_VERB + _PROHIBITED_TARGET
    + r"(?:\s*(?:或|、|及|和|與|以及|/|／|or|and)\s*"
    + _PROHIBITED_VERB + _PROHIBITED_TARGET + r")*\s*$",
    re.I,
)
_PROHIBITION_DELIMITER = re.compile(r"(?<=[，,。；;！!？?\n])")


def _without_prohibited_action_clauses(request: str) -> str:
    """Mask only complete negative clauses, preserving positive operations."""
    segments = _PROHIBITION_DELIMITER.split(request)
    retained = []
    for segment in segments:
        clause = segment.strip().rstrip("，,。；;！!？?").strip()
        retained.append(" " if _PROHIBITED_CLAUSE.fullmatch(clause) else segment)
    return "".join(retained)


_NO_ACTION = re.compile(r"(?:不需要|不用|不必|無須|不要|請勿|禁止)\s*"
                        r"(?:實際\s*|自行\s*|額外\s*)?"
                        r"(?:執行|讀取|修改|測試|編譯|下載|上傳|安裝|部署|"
                        r"開啟|建立|儲存|刪除)", re.I)
_UNRESOLVED = re.compile(
    r"(?:\b(?:my|our|private|attached|attachment|workspace|"
    r"this\s+(?:file|project|repo))\b|"
    r"(?:我的|我們的|附件|附檔|這個(?:檔案|專案)|該(?:檔案|專案)|"
    r"工作區|剛才|上次|之前討論))", re.I,
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get(FEATURE_FLAG, "1")).strip().lower() not in (
        "0", "off", "false", "no",
    )


def _result(original: str, route: str, sources: set[str],
            ops: set[str], reason: str, restrictions: list[str]) -> dict:
    caps = set()
    if "LOCAL" in sources:
        caps.add("LOCAL_WORKSPACE")
    if "GITHUB" in sources:
        caps.add("GITHUB_REPOSITORY_ACCESS")
    if "EXTERNAL" in sources:
        caps.add("EXTERNAL_ACCESS")
    if "EXECUTE" in ops:
        caps.add("COMMAND_EXECUTION")
    if "DELIVER" in ops:
        caps.add("EXTERNAL_DELIVERY")
    return {
        "schema": SCHEMA,
        "route": route,
        "resource_sources": sorted(sources) or ["NONE"],
        "required_operations": sorted(ops),
        "required_capabilities": sorted(caps),
        "restrictions": sorted(set(restrictions)),
        "external_information_required": bool(sources.intersection({"GITHUB", "EXTERNAL"})),
        "side_effect_required": bool(ops.intersection({"WRITE", "EXECUTE", "DELIVER"})),
        "intent_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "decision_owner": "RUNTIME_POLICY",
        "reason": reason,
        "grants_authorization": False,
        "verification_complete": False,
    }


def assess_task_requirement(request: str, *, attachments: bool = False,
                            authorized_paths: bool = False,
                            image_or_artifact_request: bool = False,
                            continuation: bool = False) -> dict:
    original = str(request or "").strip()
    if not original or len(original) > 12000:
        return _result(original, "AMBIGUOUS", set(), set(),
                       "empty_or_unbounded_input", [])
    restrictions = (["EXPLICIT_NO_OPERATION_CONSTRAINTS"]
                    if _NO_ACTION.search(original) else [])
    # Keep the existing narrowly scoped negative clause parser, never
    # delete positive operations hidden in mixed instructions.
    from .tri_one_answer_only import _without_explicit_noop_clauses
    cleaned = _without_prohibited_action_clauses(
        _without_explicit_noop_clauses(original)
    )
    if attachments or authorized_paths or image_or_artifact_request or continuation:
        return _result(
            original, "AMBIGUOUS" if continuation else "RESOURCE_REQUIRED",
            {"LOCAL"} if attachments or authorized_paths else
            {"EXTERNAL"} if image_or_artifact_request else set(),
            {"READ"} if attachments else {"WRITE"} if image_or_artifact_request else set(),
            "structured_tool_or_continuation_required", restrictions,
        )
    inline = bool(_INLINE.search(cleaned))
    github = bool(_GITHUB.search(cleaned))
    sources: set[str] = set()
    ops: set[str] = set()
    concept = (bool(_CONCEPT.search(cleaned)) and not _CHAIN.search(cleaned)
               and not _FOLLOWUP_ACTION.search(cleaned))
    # The payload of an explicit inline translation is input DATA, not
    # instructions. URLs and paths still require the non-fast-path route.
    transform = bool(re.match(
        r"^\s*(?:(?:please\s+)?(?:translate|rewrite|paraphrase)\b|"
        r"(?:請|幫我|請你|請幫我)?\s*(?:翻譯|改寫|潤飾))", cleaned, re.I,
    ) and inline and re.search(r"[:：]", cleaned))
    scan = re.split(r"[:：]", cleaned, maxsplit=1)[0] if transform else cleaned
    match = _PATH.search(cleaned)
    if match:
        sources.add("GITHUB" if github else "EXTERNAL"
                    if re.search(r"https?://|www\.", cleaned, re.I) else "LOCAL")
        ops.add("READ")
    if _CURRENT.search(cleaned):
        # Fresh repository state belongs to GITHUB, not an unrelated
        # external source. Explicit weather/rates still need EXTERNAL.
        general_current = bool(re.search(
            r"\b(?:weather|exchange\s+rate|stock\s+price)\b|(?:天氣|匯率|股價)",
            cleaned, re.I,
        ))
        sources.add("GITHUB" if github and not general_current else "EXTERNAL")
        ops.add("READ")
    if _ARTIFACT.search(cleaned) and not concept:
        sources.add("EXTERNAL")
        ops.add("WRITE")
    if not concept:
        for name, regex in _ACTIONS.items():
            if regex.search(scan):
                ops.add(name)
    if github and not concept and (
        _REMOTE_REVIEW.search(cleaned) or ops.intersection({"READ", "WRITE"})
    ):
        sources.add("GITHUB")
        ops.add("READ")
    if _LOCAL.search(cleaned) and ops:
        sources.add("LOCAL")
    if _EXTERNAL.search(cleaned) and ops:
        sources.add("EXTERNAL")
    if ops and not sources:
        sources.add("EXTERNAL" if "DELIVER" in ops else "LOCAL")
    if _UNRESOLVED.search(cleaned) and not inline and not ops:
        return _result(original, "AMBIGUOUS", sources, ops,
                       "unresolved_private_or_prior_context", restrictions)
    if ops or sources:
        return _result(original, "RESOURCE_REQUIRED", sources, ops,
                       "external_resource_or_action_required", restrictions)
    if not (_TEXT.search(cleaned) or _CREATIVE_TEXT.search(cleaned)):
        return _result(original, "AMBIGUOUS", sources, ops,
                       "text_intent_not_unambiguously_classified", restrictions)
    return _result(original, "ANSWER_ONLY", {"INLINE"} if inline else set(), set(),
                   "bounded_text_only_intent", restrictions)


def render_task_requirement(assessment: Mapping[str, object]) -> str:
    safe = {key: assessment[key] for key in (
        "schema", "route", "resource_sources", "required_operations",
        "required_capabilities", "restrictions", "reason",
        "grants_authorization", "verification_complete",
    ) if key in assessment}
    return "[SMARTAGENT_TASK_REQUIREMENT_V1]\n" + json.dumps(
        safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n[/SMARTAGENT_TASK_REQUIREMENT_V1]"


__all__ = ["SCHEMA", "FEATURE_FLAG", "enabled", "assess_task_requirement",
           "render_task_requirement"]
