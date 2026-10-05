"""Telegram-facing software manager for RemoteAgent binding and skill root."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock
from .remote_binding import load, normalize_skill_path, validate
from .remote_skill_manager import RemoteSkillManager
from .workspace_access import access_snapshot, require_writable_workspace
from .paths import remote_binding_transactions_path


MANAGER_COMMAND = "WebGPT 與 workspace 管理器"
LIST_SKILLS_COMMAND = "列出skill"
UPDATE_OPEN = "[WEBGPT_WORKSPACE_UPDATE]"
UPDATE_CLOSE = "[/WEBGPT_WORKSPACE_UPDATE]"
TRANSACTION_TTL_SEC = 600


class RemoteBindingManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.state_path = remote_binding_transactions_path(self.root)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")

    @staticmethod
    def is_update(text: str) -> bool:
        value = str(text or "").strip()
        return value.startswith(UPDATE_OPEN) and value.endswith(UPDATE_CLOSE)

    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {"version": 1, "transactions": {}}
        value = read_json_retry(self.state_path)
        if not isinstance(value, dict):
            raise ValueError("remote_binding_transaction_state_invalid")
        transactions = value.get("transactions", {})
        if not isinstance(transactions, dict):
            raise ValueError("remote_binding_transactions_invalid")
        return {"version": 1, "transactions": dict(transactions)}

    def begin(self, *, chat_id: int) -> str:
        binding = load(self.root)
        access = access_snapshot()
        transaction_id = "WWM-" + uuid.uuid4().hex[:12].upper()
        now = time.time()
        with exclusive_process_lock(
            self.lock_path, timeout_sec=5.0, label="remote binding manager"
        ):
            state = self._load_state()
            transactions = {
                key: value for key, value in state["transactions"].items()
                if float((value or {}).get("expires_at", 0.0) or 0.0) > now
            }
            transactions[transaction_id] = {
                "chat_id": int(chat_id),
                "created_at": now,
                "expires_at": now + TRANSACTION_TTL_SEC,
            }
            state["transactions"] = transactions
            write_json_atomic(self.state_path, state)
        workspaces = "\n".join(f"  {index}. {value}" for index, value in enumerate(access["writable_workspaces"], 1)) or "  （尚未設定）"
        read_only = "\n".join(f"  {index}. {value}" for index, value in enumerate(access["read_only_roots"], 1)) or "  （尚未設定）"
        return "\n".join((
            "目前允許讀寫的 Workspaces：", workspaces, "",
            "目前唯讀路徑：", read_only, "",
            "請修改以下三個欄位後，將整段訊息直接送出：",
            "",
            UPDATE_OPEN,
            f"transaction_id={transaction_id}",
            f"webgpt_url={binding['gpt_url']}",
            f"workspace={binding['workspace']}",
            f"skill_path={binding['skill_path']}",
            UPDATE_CLOSE,
            "",
            "有效時間：10 分鐘。更新期間若有任務執行中，設定不會被切換。",
        ))

    @staticmethod
    def _parse_update(text: str) -> dict:
        value = str(text or "").strip()
        if not RemoteBindingManager.is_update(value):
            raise ValueError("remote_binding_update_block_invalid")
        body = value[len(UPDATE_OPEN):-len(UPDATE_CLOSE)].strip()
        fields = {}
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"remote_binding_update_line_invalid:{line[:80]}")
            key, raw = line.split("=", 1)
            key = key.strip()
            if key in fields:
                raise ValueError(f"remote_binding_update_duplicate_field:{key}")
            fields[key] = raw.strip()
        expected = {"transaction_id", "webgpt_url", "workspace", "skill_path"}
        if set(fields) != expected:
            missing = sorted(expected - set(fields))
            extra = sorted(set(fields) - expected)
            raise ValueError(
                "remote_binding_update_fields_invalid:"
                f"missing={','.join(missing)};extra={','.join(extra)}"
            )
        return fields

    def apply(
        self,
        text: str,
        *,
        chat_id: int,
        runtime_apply: Callable[[dict], None],
    ) -> str:
        fields = self._parse_update(text)
        candidate = validate({
            "workspace": fields["workspace"],
            "gpt_url": fields["webgpt_url"],
            "skill_path": normalize_skill_path(
                fields["skill_path"], require_exists=True
            ),
        })
        candidate["workspace"] = require_writable_workspace(candidate["workspace"])
        transaction_id = fields["transaction_id"]
        now = time.time()
        with exclusive_process_lock(
            self.lock_path, timeout_sec=5.0, label="remote binding manager"
        ):
            state = self._load_state()
            transaction = state["transactions"].get(transaction_id)
            if not isinstance(transaction, dict):
                raise ValueError("remote_binding_transaction_unknown")
            if int(transaction.get("chat_id", 0) or 0) != int(chat_id):
                raise ValueError("remote_binding_transaction_chat_mismatch")
            if float(transaction.get("expires_at", 0.0) or 0.0) <= now:
                raise ValueError("remote_binding_transaction_expired")

            # Persist consumption before invoking the side effect.  A process
            # crash or response-delivery retry can therefore never replay an
            # already-started binding transaction.  A failed apply requires a
            # fresh manager transaction, which is safer than ambiguous replay.
            state["transactions"].pop(transaction_id, None)
            write_json_atomic(self.state_path, state)

            # The resident host owns the complete binding commit.  Keeping
            # persistence, registry mutation, security attestation and listener
            # hot-apply in one callback prevents a half-published binding.
            runtime_apply(candidate)

        return "\n".join((
            "✅ WebGPT、workspace 與 skill path 已更新。",
            "",
            f"WebGPT URL：{candidate['gpt_url']}",
            f"Workspace：{candidate['workspace']}",
            f"Skill Path：{candidate['skill_path']}",
            "",
            "Telegram listener 持續在線；OS 安全預檢已同步更新，下一個遠端任務會使用新設定。",
        ))

    def render_skills(self) -> str:
        binding = load(self.root)
        manager = RemoteSkillManager(binding["skill_path"])
        names = manager.list_skills()
        lines = [f"目前 Skill Path：{binding['skill_path']}", "", "可用 Skills："]
        if names:
            lines.extend(f"{index}. {name}" for index, name in enumerate(names, 1))
        else:
            lines.append("（沒有找到包含 SKILL.md 的 skill）")
        lines.extend(("", f"共 {len(names)} 個有效 skill。"))
        return "\n".join(lines)


__all__ = [
    "LIST_SKILLS_COMMAND", "MANAGER_COMMAND", "RemoteBindingManager",
    "TRANSACTION_TTL_SEC", "UPDATE_CLOSE", "UPDATE_OPEN",
]
