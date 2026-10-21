"""Shared model-output budgets; character counts are distinct from UTF-8 bytes."""
from __future__ import annotations

import json

WRITE_FILE_CONTENT_MAX_CHARS = 4096
# 暫停新增的單塊／整輪預算：保留程式方便恢復，不影響既有 write_file 4096 上限。
WRITE_BUDGET_LIMITS_ENABLED = False
# 以下數值目前停用；改為 True 才同時啟用提示與 Runtime 檢查。
CHUNK_CONTENT_MAX_CHARS = 1000
CHUNK_CONTENT_TARGET_CHARS = 800
ROUND_WRITE_CONTENT_MAX_CHARS = 2000
WRITE_CONTENT_TOOLS = frozenset({"write_file", "write_file_chunk"})


def write_budget_limits_enabled() -> bool:
    return WRITE_BUDGET_LIMITS_ENABLED


def write_budget_contract() -> dict:
    return {
        "schema": "SMARTAGENT_WRITE_BUDGET_V1", "unit": "unicode_code_points",
        "write_file_max_chars": WRITE_FILE_CONTENT_MAX_CHARS,
        "extra_limits_enabled": write_budget_limits_enabled(),
        "chunk_max_chars": CHUNK_CONTENT_MAX_CHARS if write_budget_limits_enabled() else None,
        "chunk_target_chars": CHUNK_CONTENT_TARGET_CHARS if write_budget_limits_enabled() else None,
        "round_write_max_chars": ROUND_WRITE_CONTENT_MAX_CHARS if write_budget_limits_enabled() else None,
        "counted_tools": sorted(WRITE_CONTENT_TOOLS),
    }


def render_write_budget() -> str:
    if not write_budget_limits_enabled():
        return (
            "[SMARTAGENT_WRITE_BUDGET]\n"
            + json.dumps(write_budget_contract(), sort_keys=True, separators=(",", ":"))
            + f"\n僅保留 write_file.content 最多 {WRITE_FILE_CONTENT_MAX_CHARS} 字元的原有工具上限；"
            "新增的單塊與整輪字元預算目前停用，不得沿用先前對話中的停用數值。"
            "大型內容仍可分塊跨輪接續，省略 chunk_index、offset、chunk_size、chunk_sha256，"
            "由 Runtime 計算實際 UTF-8 bytes。依 OPEN 交易與 next_offset 接續，不重送已接受分塊；"
            "正式 commit 後仍須滿足任務驗收。\n[/SMARTAGENT_WRITE_BUDGET]"
        )
    return (
        "[SMARTAGENT_WRITE_BUDGET]\n"
        + json.dumps(write_budget_contract(), sort_keys=True, separators=(",", ":"))
        + "\n只計算 write_file/write_file_chunk 的 content 字串，不計協議 JSON、Progress 或回覆文字。"
        "整輪累計不得超標；同輪完成要求不能覆蓋此預算。超過時開啟分塊交易、分輪接續，"
        "保持 CONTINUE，直到全部內容送完才能 commit。一般分塊省略 chunk_index、offset、"
        "chunk_size、chunk_sha256，由 Runtime 計算實際 UTF-8 bytes；不要猜測大小或雜湊。"
        "依 Runtime 的 OPEN 交易與 next_offset 接續，不重送已接受分塊；正式 commit 後仍須滿足任務驗收。"
        "\n[/SMARTAGENT_WRITE_BUDGET]"
    )


def validate_round_write_budget(actions: list[dict]) -> dict | None:
    if not write_budget_limits_enabled():
        return None  # 暫停單塊與整輪限制；write_file 上限仍由工具 schema 驗證。
    writes = []
    total = 0
    for index, action in enumerate(actions, 1):
        if action.get("tool") not in WRITE_CONTENT_TOOLS or not isinstance(action.get("content"), str):
            continue
        size = len(action["content"])
        total += size
        writes.append({"action_index": index, "action_id": action.get("action_id", ""),
                       "tool": action["tool"], "content_chars": size})
    oversized = [item for item in writes if item["tool"] == "write_file_chunk"
                 and item["content_chars"] > CHUNK_CONTENT_MAX_CHARS]
    if not oversized and total <= ROUND_WRITE_CONTENT_MAX_CHARS:
        return None
    return {
        "marker": "[WEBAGENT_WRITE_BUDGET_REJECTED]",
        "reason": "chunk_content_too_large" if oversized else "round_write_budget_exceeded",
        "detail": json.dumps({"actual_chars": total, "budget": write_budget_contract(),
                              "write_actions": writes, "oversized_chunks": oversized,
                              "action_executed": False}, ensure_ascii=False, sort_keys=True),
        "suggestion": "整批尚未執行。重新提交符合預算的完整本輪 batch；大型內容分塊跨輪接續，"
                      "剩餘內容留到後續輪次，Progress 維持 CONTINUE；不要把截短內容當成完整檔案。",
        "action_executed": False,
    }
