#!/usr/bin/env python3
"""Crash-safe, one-time human approval ledger for destructive operations."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock
from .paths import secure_root


class SecurityApprovalError(RuntimeError):
    pass


class SecurityApprovalLedger:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        state_root = secure_root(self.workspace)
        from .windows_security import restricted_executor_required
        required = restricted_executor_required()
        if required:
            try:
                from .windows_security import load_security_profile

                configured = str(
                    load_security_profile().get("controller_state_root", "") or ""
                ).strip()
                if not configured:
                    raise SecurityApprovalError("controller_state_root_missing")
                state_root = Path(configured).expanduser().resolve()
            except SecurityApprovalError:
                raise
            except Exception as exc:
                raise SecurityApprovalError(
                    f"controller_security_state_unavailable:{exc}"
                ) from exc
        self.path = state_root / "approvals.json"
        self.lock_path = self.path.with_name("approvals.lock")

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 2, "approvals": {}, "permanent_delete_workspaces": {}}
        value = read_json_retry(self.path)
        return value if isinstance(value, dict) else {"version": 2, "approvals": {}, "permanent_delete_workspaces": {}}

    def _save(self, value: dict) -> None:
        write_json_atomic(self.path, value)

    @staticmethod
    def _identity(binding: dict) -> str:
        raw = "|".join(str(binding.get(key, "")) for key in (
            "request_id", "task_id", "action_id", "action_digest",
            "target", "manifest_digest", "chat_id", "permanent_scope", "workspace_root", "approval_kind",
        ))
        return "APR-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20].upper()

    def request(self, binding: dict, *, ttl_sec: float = 300.0) -> dict:
        now = time.time()
        record = {
            key: str(binding.get(key, "") or "")
            for key in ("request_id", "task_id", "action_id", "action_digest", "target", "manifest_digest", "chat_id", "permanent_scope", "workspace_root", "approval_kind")
        }
        if not all(record.get(key) for key in ("request_id", "task_id", "action_id", "action_digest", "target", "manifest_digest")):
            raise SecurityApprovalError("approval_binding_incomplete")
        approval_id = self._identity(record)
        with exclusive_process_lock(self.lock_path, label="security approval"):
            state = self._load()
            approvals = state.setdefault("approvals", {})
            existing = approvals.get(approval_id)
            if isinstance(existing, dict) and existing.get("state") in {"PENDING", "APPROVED"} and float(existing.get("expires_at", 0)) > now:
                return dict(existing)
            record.update(
                approval_id=approval_id, state="PENDING", created_at=now,
                expires_at=now + max(30.0, float(ttl_sec)), approved_at=0.0,
                consumed_at=0.0, actor="",
            )
            approvals[approval_id] = record
            self._save(state)
            return dict(record)

    def decide(self, approval_id: str, *, approve: bool, chat_id: str, actor: str) -> dict:
        now = time.time()
        with exclusive_process_lock(self.lock_path, label="security approval"):
            state = self._load(); record = state.setdefault("approvals", {}).get(str(approval_id))
            if not isinstance(record, dict):
                raise SecurityApprovalError("approval_not_found")
            if record.get("state") != "PENDING":
                raise SecurityApprovalError(f"approval_not_pending:{record.get('state')}")
            if float(record.get("expires_at", 0)) <= now:
                record["state"] = "EXPIRED"; self._save(state)
                raise SecurityApprovalError("approval_expired")
            expected_chat = str(record.get("chat_id", "") or "")
            if expected_chat and expected_chat != str(chat_id or ""):
                raise SecurityApprovalError("approval_chat_mismatch")
            record.update(
                state="APPROVED" if approve else "REJECTED",
                approved_at=now if approve else 0.0,
                rejected_at=now if not approve else 0.0,
                actor=str(actor or "human"),
            )
            self._save(state); return dict(record)

    def get(self, approval_id: str) -> dict | None:
        with exclusive_process_lock(self.lock_path, label="security approval"):
            record = self._load().get("approvals", {}).get(str(approval_id))
            return dict(record) if isinstance(record, dict) else None

    def consume(self, approval_id: str, binding: dict) -> dict:
        now = time.time()
        with exclusive_process_lock(self.lock_path, label="security approval"):
            state = self._load(); record = state.setdefault("approvals", {}).get(str(approval_id))
            if not isinstance(record, dict) or record.get("state") != "APPROVED":
                raise SecurityApprovalError("approval_not_approved")
            if float(record.get("expires_at", 0)) <= now:
                record["state"] = "EXPIRED"; self._save(state)
                raise SecurityApprovalError("approval_expired")
            for key in ("request_id", "task_id", "action_id", "action_digest", "target", "manifest_digest", "chat_id", "permanent_scope", "workspace_root", "approval_kind"):
                if str(record.get(key, "")) != str(binding.get(key, "") or ""):
                    raise SecurityApprovalError(f"approval_binding_mismatch:{key}")
            record.update(state="CONSUMED", consumed_at=now)
            self._save(state); return dict(record)

    @staticmethod
    def permanent_scope_for_target(workspace_root: str | Path, target: str | Path) -> str:
        root = Path(workspace_root).expanduser().resolve()
        candidate = Path(target).expanduser().resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return ""
        parts = relative.parts
        if len(parts) < 2 or parts[0].casefold() == ".agents":
            return ""
        scope = (root / parts[0]).resolve()
        if not scope.is_dir():
            return ""
        try:
            nested = candidate.relative_to(scope)
        except ValueError:
            return ""
        if not nested.parts:
            return ""
        return str(scope)

    def authorized_workspace_for_target(self, workspace_root: str | Path, target: str | Path) -> str:
        scope = self.permanent_scope_for_target(workspace_root, target)
        if not scope:
            return ""
        key = str(Path(scope).resolve()).casefold()
        with exclusive_process_lock(self.lock_path, label="security approval"):
            grants = self._load().get("permanent_delete_workspaces", {})
            record = grants.get(key) if isinstance(grants, dict) else None
            return scope if isinstance(record, dict) and record.get("scope") else ""

    def grant_workspace_for_approval(self, approval_id: str, *, chat_id: str, actor: str) -> dict:
        now = time.time()
        with exclusive_process_lock(self.lock_path, label="security approval"):
            state = self._load()
            record = state.setdefault("approvals", {}).get(str(approval_id))
            if not isinstance(record, dict):
                raise SecurityApprovalError("approval_not_found")
            if record.get("state") != "PENDING":
                raise SecurityApprovalError(f"approval_not_pending:{record.get('state')}")
            if float(record.get("expires_at", 0)) <= now:
                record["state"] = "EXPIRED"
                self._save(state)
                raise SecurityApprovalError("approval_expired")
            expected_chat = str(record.get("chat_id", "") or "")
            if expected_chat and expected_chat != str(chat_id or ""):
                raise SecurityApprovalError("approval_chat_mismatch")
            scope = str(record.get("permanent_scope", "") or "").strip()
            workspace_root = str(record.get("workspace_root", "") or "").strip()
            target = Path(str(record.get("target", "") or "")).resolve()
            if not scope or not workspace_root:
                raise SecurityApprovalError("permanent_workspace_scope_unavailable")
            expected_scope = self.permanent_scope_for_target(workspace_root, target)
            if not expected_scope or Path(expected_scope).resolve() != Path(scope).resolve():
                raise SecurityApprovalError("permanent_workspace_scope_not_second_level")
            scope_path = Path(scope).resolve()
            try:
                nested = target.relative_to(scope_path)
            except ValueError as exc:
                raise SecurityApprovalError("permanent_workspace_scope_mismatch") from exc
            if not nested.parts or not scope_path.is_dir():
                raise SecurityApprovalError("permanent_workspace_scope_invalid")
            grants = state.setdefault("permanent_delete_workspaces", {})
            grants[str(scope_path).casefold()] = {
                "scope": str(scope_path), "granted_at": now,
                "actor": str(actor or "human"), "chat_id": str(chat_id or ""),
            }
            record.update(state="APPROVED", approved_at=now, actor=str(actor or "human"), permanent_workspace_granted=True)
            self._save(state)
            return dict(record)

    def clear_pending(self, *, reason: str) -> int:
        changed = 0
        with exclusive_process_lock(self.lock_path, label="security approval"):
            state = self._load()
            for record in state.setdefault("approvals", {}).values():
                if isinstance(record, dict) and record.get("state") in {"PENDING", "APPROVED"}:
                    record.update(state="CANCELLED", cancelled_at=time.time(), cancel_reason=str(reason)); changed += 1
            if changed: self._save(state)
        return changed


__all__ = ["SecurityApprovalError", "SecurityApprovalLedger"]
