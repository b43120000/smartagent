"""Conservative, request-local interpretation of conditional Goal mutation intent.

This is an advisory semantic *policy* for prerequisite planning, never a
write authorization. A scoped prohibition on overwriting existing data must
not be promoted to a global prohibition on creating missing data. Likewise,
"inspect read-only first" must not erase an explicitly conditional later
repair step. Ambiguity defaults to DISCOVERY_ONLY, not mutation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_CONDITIONAL_GOAL_POLICY_V1"

# Independent prohibitions applying to the *whole* task, not to named files,
# an inspection phase, overwrites, deletions, or unnecessary modifications.
_GLOBAL_NO_MUTATION = re.compile(
    r"(?:全程|整個任務|本次任務|只允許|僅允許|僅限|只做|只進行|只能)"
    r"[^。；;\n]{0,36}(?:唯讀|只讀|read[ -]?only|禁止修改|禁止寫入)"
    r"|(?:禁止|不得|請勿|不允許|不要)\s*(?:任何|所有|一切)?\s*"
    r"(?:實際)?(?:寫入|新增|建立|創立|創建|新建|安裝|修改|部署|補救|變更)"
    r"(?![^。；;\n]{0,18}(?:原始|既有|特定|指定|不必要|未經))"
    r"|\b(?:never|must not|do not|don't)\s+"
    r"(?:create|write|mutate|install|modify|deploy)\b",
    re.I,
)
_POSITIVE_REMEDIATION = re.compile(
    r"(?<!不)(?:建立|新增|創建|補齊|修復|安裝|部署|建置|補上|補足)"
    r"|\b(?:deploy|create|setup|repair|install|provision|restore)\b",
    re.I,
)
_CONDITIONAL_REMEDIATION = re.compile(
    r"(?:如果|若|倘若|當|發現|確認).{0,90}?"
    r"(?:缺少|缺失|不足|不存在|沒有|未安裝|異常|失敗)"
    r"[^。；;\n]{0,150}?"
    r"(?:再|才|則|就|需|應|請|予以)?.{0,40}?"
    r"(?:補齊|補上|補足|修復|建立|部署|新增|安裝)"
    r"|\b(?:if|when)\b.{0,100}?"
    r"\b(?:missing|absent|not found|fails?)\b.{0,100}?"
    r"\b(?:create|repair|deploy|install|restore)\b",
    re.I,
)
_READ_ONLY_STAGE = re.compile(
    r"(?:先|首先|第一階段|初期|開始時|起初|初始).{0,32}?"
    r"(?:唯讀|只讀|read[ -]?only)"
    r"|(?:唯讀|只讀|read[ -]?only).{0,26}?"
    r"(?:檢查|確認|查詢|探索|觀測)"
    r"|\b(?:read[ -]?only|inspect)\b.{0,40}\b(?:first|initially)\b",
    re.I,
)

def _clauses(goal: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。；;\n]+", goal) if s.strip()]


def conditional_goal_policy(goal: str) -> dict:
    """Return non-authoritative intent and explicit scoped restrictions.

    A sentence about "do not overwrite/delete existing data" is retained as
    a restriction, not promoted to a global no-mutation decision.
    """
    text = str(goal or "")
    clauses = _clauses(text)
    # Directory-specific language is owned by existing SCR translator.
    # No second, drifting synonym list in Goal Policy.
    from .semantic_capability_resolution import translate_request_action
    semantic_action = translate_request_action(text)
    directory_creation = semantic_action["selected_capability"] == "directory.create"
    positive = bool(_POSITIVE_REMEDIATION.search(text)) or directory_creation
    conditional = bool(_CONDITIONAL_REMEDIATION.search(text)) or bool(
        directory_creation and re.search(r"(?:如果|若|當|確認|if|when)", text, re.I)
        and re.search(r"(?:缺少|缺失|不存在|沒有|missing|absent)", text, re.I)
    )
    first_read = bool(_READ_ONLY_STAGE.search(text))
    no_mutation = bool(_GLOBAL_NO_MUTATION.search(text))
    # Explicit no-write clauses about specific named files or avoiding
    # *unnecessary* changes do not imply a global ban.
    for clause in clauses:
        if not _GLOBAL_NO_MUTATION.search(clause):
            continue
        if re.search(
            r"(?:不得|禁止|請勿).{0,16}?"
            r"(?:覆蓋|刪除|移動|不必要|原始|既有)"
            r"|(?:不得|禁止).{0,16}?(?:修改|寫入).{0,16}?"
            r"(?:原始|既有|指定|特定|\\.csv|\\.json)",
            clause, re.I,
        ) and not re.search(r"(?:禁止|不得)\s*(?:任何|所有|一切)?\s*寫入", clause):
            # This is a restriction on a subclass of operations.
            # It cannot override an explicitly absolute prohibition elsewhere.
            no_mutation = any(
                bool(_GLOBAL_NO_MUTATION.search(other))
                for other in clauses if other is not clause and other != clause
            )
    restrictions = []
    for code, pattern in [
        ("NO_OVERWRITE", r"不覆蓋|不得覆蓋|禁止覆蓋|do not overwrite"),
        ("NO_DELETE", r"不刪除|不得刪除|禁止刪除|do not delete"),
        ("NO_MOVE", r"不移動|不得移動|禁止移動|do not move"),
        ("NO_UNNECESSARY_MUTATION", r"不必要.{0,10}(?:異動|修改|寫入)|unnecessary changes"),
    ]:
        if re.search(pattern, text, re.I):
            restrictions.append(code)
    if no_mutation:
        intent = "READ_ONLY_ONLY"
    elif conditional and positive:
        intent = "CONDITIONAL_REMEDIATION"
    elif positive:
        intent = "REMEDIATION_CANDIDATE"
    else:
        intent = "DISCOVERY_OR_OBSERVATION"
    return {
        "schema": SCHEMA,
        "intent": intent,
        "read_only_first": first_read,
        "conditional_remediation": conditional and not no_mutation,
        "restrictions": restrictions,
        "mutation_is_authorized": False,
        "only_runtime_scope_and_permission_can_authorize": True,
        "unresolved_authorization_is_not_denial": True,
        "semantic_translation": semantic_action,
    }


__all__ = ["SCHEMA", "conditional_goal_policy"]
