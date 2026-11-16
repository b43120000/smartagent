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
from .workspace_turbo_binding import (
    turbo_mode_for_workspace, prepare_turbo_mode_update,
)
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
                "workspace_policy_revision": int((access.get("policy") or {}).get("revision", 0) or 0),
            }
            state["transactions"] = transactions
            write_json_atomic(self.state_path, state)
        workspaces = "\n".join(f"  {index}. {value}" for index, value in enumerate(access["writable_workspaces"], 1)) or "  （尚未設定）"
        read_only = "\n".join(f"  {index}. {value}" for index, value in enumerate(access["read_only_roots"], 1)) or "  （尚未設定）"
        from .turbo_sandbox import global_turbo_snapshot
        sandbox_state = global_turbo_snapshot(access=access)
        global_roots = ";".join(sandbox_state["configured_roots"]) or "off"
        from .auto_approve_mode import auto_approve_snapshot
        auto_state = auto_approve_snapshot(binding["workspace"])
        root_lines = "\n".join(
            "  - " + record["path"] + " [" + record["native_enrollment"] + "]"
            for record in sandbox_state["roots"]
        ) or "  （尚未設定）"
        return "\n".join((
            "目前允許讀寫的 Workspaces：", workspaces, "",
            "目前唯讀路徑：", read_only, "",
            "目前 Turbo Mode roots（全域，與 Edit Workspace 共用 Policy）：", root_lines, "",
            "支援 ChatGPT、Gemini、Claude、M365 與 Other HTTP(S) URL。Other 網站需先完成 DOM 校準。\n"
            "下方 workspace 僅是 WebGPT 的預設工作目錄，不會停用其他 Turbo Root。"
            "global_turbo_roots 是全域 Root 登錄清單（唯讀），不限定 Telegram 的執行範圍。"
            "修改 Root 請使用 Edit Workspace；OS Enrollment 仍由管理員程序完成。",
            "",
            UPDATE_OPEN,
            f"transaction_id={transaction_id}",
            f"webgpt_url={binding['gpt_url']}",
            f"workspace={binding['workspace']}",
            f"global_turbo_roots={global_roots}",
            f"skill_path={binding['skill_path']}",
            f"auto_approve={auto_state['auto_approve_requested'].lower()}",
            UPDATE_CLOSE,
            "",
            "ACL: " + sandbox_state["acl_mode"] + " | Requested: " + sandbox_state["turbo_requested"] + " | Effective: " + sandbox_state["turbo_effective"],
            "Sandbox: " + sandbox_state["sandbox_status"] + " | Reason: " + sandbox_state["reason_code"],
            "Auto Approve Requested: " + auto_state["auto_approve_requested"]
              + " | Effective: " + auto_state["auto_approve_effective"]
              + " | Executor: " + auto_state["executor_backend"],
            "Native enrollment: " + str(sandbox_state["enrolled_root_count"]) + "/" + str(sandbox_state["configured_root_count"]) + " (advisory)",
            "有效時間：10 分鐘。更新期間若有任務執行中，設定不會被切換。",
        ))

    @staticmethod
    def _parse_update(text: str) -> dict:
        value = str(text or "").strip()
        if not RemoteBindingManager.is_update(value):
            raise ValueError("remote_binding_update_block_invalid")
        body = value[len(UPDATE_OPEN):-len(UPDATE_CLOSE)].strip()
        fields = {}
        # Telegram may paste a Markdown fenced single turbo field. Treat only
        # exact backtick-only fence lines as presentation; never eval Markdown.
        fence_open = False
        fenced_fields = []
        for line in body.splitlines():
            line = line.strip()
            if line == "```":
                fence_open = not fence_open
                continue
            if not line:
                continue
            if fence_open:
                fenced_fields.append(line)
            if "=" not in line:
                raise ValueError(f"remote_binding_update_line_invalid:{line[:80]}")
            key, raw = line.split("=", 1)
            key = key.strip()
            if key in fields:
                raise ValueError(f"remote_binding_update_duplicate_field:{key}")
            fields[key] = raw.strip()
        if fence_open or (fenced_fields and
                          (len(fenced_fields) != 1 or
                           not fenced_fields[0].startswith("turbomode="))):
            raise ValueError("remote_binding_update_turbo_fence_invalid")
        expected = {"transaction_id", "webgpt_url", "workspace", "skill_path"}
        allowed = expected | {"turbomode", "global_turbo_roots", "auto_approve"}
        if "turbomode" in fields and "global_turbo_roots" in fields:
            raise ValueError("remote_binding_turbo_fields_mutually_exclusive")
        if not expected.issubset(fields) or set(fields) - allowed:
            missing = sorted(expected - set(fields))
            extra = sorted(set(fields) - allowed)
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
        global_roots = fields.get("global_turbo_roots")
        auto_raw = fields.get("auto_approve")
        auto_requested = None
        if auto_raw is not None:
            if auto_raw.strip().lower() not in {"on", "off"}:
                raise ValueError("remote_binding_auto_approve_value_invalid")
            auto_requested = auto_raw.strip().lower() == "on"
        # A global inventory is not an authorization form. It cannot edit the
        # policy or implicitly remove other roots when the WebGPT binding moves.
        # Old 'turbomode=' transactions remain supported for compatibility,
        # using their original per-Workspace mutation rules.
        if global_roots is not None:
            from .turbo_sandbox import global_turbo_snapshot
            latest = global_turbo_snapshot(access=access_snapshot())
            expected_roots = ";".join(latest["configured_roots"]) or "off"
            if global_roots.casefold() != expected_roots.casefold():
                raise ValueError("remote_binding_global_turbo_roots_read_only")
        turbo_prepared = prepare_turbo_mode_update(
            fields.get("turbomode"), candidate["workspace"])
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
            # Both global-inventory and legacy local-root forms require a
            # fresh policy snapshot; do not accept a stale or forged list.
            revision = int((access_snapshot().get("policy") or {}).get("revision", 0) or 0)
            if (global_roots is not None or turbo_prepared is not None
                    or auto_requested is not None) and (
                int(transaction.get("workspace_policy_revision", -1)) != revision
                or (turbo_prepared is not None
                    and int(turbo_prepared["expected_revision"]) != revision)
            ):
                raise ValueError("remote_binding_workspace_policy_stale_retry_manager")

            # Persist consumption before invoking the side effect.  A process
            # crash or response-delivery retry can therefore never replay an
            # already-started binding transaction.  A failed apply requires a
            # fresh manager transaction, which is safer than ambiguous replay.
            state["transactions"].pop(transaction_id, None)
            write_json_atomic(self.state_path, state)

            # The resident host owns the complete binding commit.  Keeping
            # persistence, registry mutation, security attestation and listener
            # hot-apply in one callback prevents a half-published binding.
            if turbo_prepared is not None:
                candidate["_turbo_prepared"] = turbo_prepared
            runtime_apply(candidate)
            if auto_requested is not None:
                from .auto_approve_mode import set_auto_approve_requested
                set_auto_approve_requested(auto_requested)
        actual_turbo = turbo_mode_for_workspace(candidate["workspace"])
        from .auto_approve_mode import auto_approve_snapshot
        auto_state = auto_approve_snapshot(candidate["workspace"])
        if (auto_requested is not None and
                (auto_state["auto_approve_requested"] == "ON") != auto_requested):
            raise RuntimeError("remote_binding_auto_approve_postcondition_failed")
        from .turbo_sandbox import global_turbo_snapshot
        sandbox_state = global_turbo_snapshot(access=access_snapshot())
        if turbo_prepared is not None and actual_turbo.casefold() != turbo_prepared["mode"].casefold():
            raise RuntimeError("remote_binding_turbo_postcondition_failed")
        return "\n".join((
            ("✅ WebGPT、Workspace、Turbo Mode 與 Skill Path 已更新。"
             if turbo_prepared is not None else
             "✅ WebGPT、Workspace 與 Skill Path 已更新；Turbo Roots 未修改。"),
            "",
            f"WebGPT URL：{candidate['gpt_url']}",
            f"Workspace：{candidate['workspace']}",
            f"Turbo Mode Requested Path：{actual_turbo}",
            f"Turbo Mode：{actual_turbo}",
            "global_turbo_roots=" + (";".join(sandbox_state["configured_roots"]) or "off"),
            f"ACL Mode：{sandbox_state['acl_mode']}",
            f"Turbo Requested：{sandbox_state['turbo_requested']}",
            f"Turbo Effective：{sandbox_state['turbo_effective']}",
            f"Auto Approve Requested：{auto_state['auto_approve_requested']}",
            f"Auto Approve Effective：{auto_state['auto_approve_effective']}",
            f"Executor Backend：{auto_state['executor_backend']}",
            f"Sandbox Status：{sandbox_state['sandbox_status']}",
            f"Reason Code：{sandbox_state['reason_code']}",
            f"Skill Path：{candidate['skill_path']}",
            "",
            "Telegram listener 持續在線；Turbo Root 登錄不等於 OS 授權。"
            "未完成管理員 Native AppContainer Enrollment 的 Root，"
            "寫入任務將被阻擋，不會退回一般 Executor。",
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
