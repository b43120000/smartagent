"""Track objective progress while recovering an invalid edit plan.

Action ids, prose and refreshed snapshot ids are not source evidence progress.
This tracker does not synthesize edits or relax tool authorization.
"""
from __future__ import annotations

import hashlib
import json


class BlockerRecovery:
    def __init__(self):
        self.seen = set()
        self.blocker = None
        self.hinted = False
        self.evidence_refs = {}

    def observe(self, context, results):
        if not context.get("active") or context.get("reason") != "edit_plan_requires_canonical_replan":
            self.blocker = None
            self.seen.clear()
            self.evidence_refs.clear()
            self.hinted = False
            return "inactive"
        missing = tuple(sorted(context.get("missing_fields", context.get("required_plan_fields", []))))
        blocker = (context.get("reason"), missing,
                   json.dumps(context.get("validator_diagnostic", {}), sort_keys=True))
        changed = blocker != self.blocker
        self.blocker = blocker
        fresh = False
        for item in results:
            if item.get("tool") != "query_project":
                continue
            payload = item.get("result", "")
            try:
                payload = json.loads(payload) if isinstance(payload, str) else payload
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            for row in payload.get("results", []):
                if not isinstance(row, dict) or row.get("status") != "OK":
                    continue
                if row.get("operation") != "read_range" or not row.get("file_sha256"):
                    continue
                key = (payload.get("project_root", ""), row.get("path"),
                       row.get("file_sha256"), row.get("start_line"), row.get("end_line"),
                       hashlib.sha256(str(row.get("content", "")).encode()).hexdigest())
                if key not in self.seen:
                    self.seen.add(key)
                    self.evidence_refs[key] = {
                        "action_id": item.get("action_id", ""),
                        "project_root": key[0], "path": key[1],
                        "file_sha256": key[2], "start_line": key[3], "end_line": key[4],
                    }
                    fresh = True
        if changed or fresh:
            self.hinted = False
            return "progress"
        if not self.hinted:
            self.hinted = True
            return "repair_required"
        return "stalled"

    def guidance(self, context):
        return (
            "\n[WEBAGENT_BLOCKER_RECOVERY]\n"
            "目前阻塞是 INVALID_PLAN，而不是尚未重新讀取專案。一次補齊完整 plan 並提交 "
            "validate_edit_plan；不要重做整個任務，也不要重讀相同 SHA256 與相同範圍。"
            "若確實缺少內容，明確指出缺少的路徑／範圍、現有 evidence 不足的原因與取得它的 action。"
            "Runtime 不會替你決定修改內容。缺少欄位："
            + json.dumps(context.get("missing_fields", context.get("required_plan_fields", [])), ensure_ascii=False)
            + "\n已取得來源 evidence（重用既有工具結果，不重新同步）："
            + json.dumps(list(self.evidence_refs.values()), ensure_ascii=False)
            + "\n[/WEBAGENT_BLOCKER_RECOVERY]"
        )
