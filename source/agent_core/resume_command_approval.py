"""Transport-neutral, software-owned pre-resume confirmation for pending commands.

User confirmation is bound to an exact paused command, task, owner and
workspace. It is *not* Tool admission; new child actions still pass all normal
schema, safety, scope and postcondition checks before consuming one grant.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Mapping

from .json_state_io import read_json_retry, write_json_atomic
from .paths import secure_root, remote_events_path
from .process_file_lock import exclusive_process_lock
from .security_approval import SecurityApprovalLedger, SecurityApprovalError
from .task_continuation import TaskContinuationStore, parse_continuation, _same_path

TTL_SECONDS = 600
SCHEMA = "SMARTAGENT_RESUME_COMMAND_GATE_V1"


class ResumeApprovalError(ValueError):
    pass


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _cmd_sha(command) -> str:
    return hashlib.sha256(str(command).encode("utf-8")).hexdigest()


def _action_key(action):
    return {
        "command": str(action.get("command") or ""),
        "operation": str(action.get("operation") or ""),
        "working_directory": str(action.get("working_directory") or ""),
    }


class ResumeCommandGate:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = secure_root(self.root) / "resume_command_gate.json"
        self.lock = self.path.with_suffix(".lock")
        self.approvals = SecurityApprovalLedger(self.root)
        self.continuations = TaskContinuationStore(self.root)

    def _load(self):
        if not self.path.is_file():
            return {"schema": SCHEMA, "commands": {}, "challenges": {}}
        obj = read_json_retry(self.path)
        if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
            raise ResumeApprovalError("resume_approval_store_invalid")
        return obj

    def _save(self, obj):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, obj)

    def remember(self, *, loop, action: Mapping, old_approval_id: str):
        """Save *unexecuted* proposal in protected state, not public Blackboard."""
        if not str(action.get("command") or ""):
            raise ResumeApprovalError("resume_command_missing")
        task_id = str(loop.task_id or "")
        owner = str(loop.continuation_owner or "")
        workspace = str(Path(loop.workspace).resolve())
        row = {
            "source_task_id": task_id, "source_request_id": str(loop.run_id),
            "source_epoch": str(loop.task_epoch), "owner": owner,
            "workspace": workspace, "action": _action_key(action),
            "command_sha256": _cmd_sha(action["command"]),
            "original_action_id": str(action.get("action_id") or ""),
            "old_approval_id": str(old_approval_id or ""),
            "executed": False, "recorded_at": time.time(),
        }
        if not (task_id and owner and row["command_sha256"]):
            raise ResumeApprovalError("resume_command_identity_missing")
        with exclusive_process_lock(self.lock, label="resume-command"):
            obj = self._load()
            obj["commands"][task_id] = row
            self._save(obj)
        return {"source_task_id": task_id,
                "command_sha256": row["command_sha256"]}

    def _legacy_command(self, source, pending):
        """Recover an older Git39 event only when task, approval and digest match."""
        if not pending:
            return None
        old_id = str(pending.get("approval_id") or "")
        expected_sha = str(pending.get("command_sha256") or "")
        if not old_id or not expected_sha:
            return None
        p = remote_events_path(self.root)
        if not p.is_file():
            return None
        events = read_json_retry(p).get("events", {})
        for event in events.values():
            if (not isinstance(event, dict)
                    or event.get("event_type") != "SECURITY_CONFIRMATION_REQUIRED"
                    or str(event.get("task_id")) != source["task_id"]):
                continue
            payload = event.get("payload") or {}
            if payload.get("approval_kind") != "UNKNOWN_COMMAND":
                continue
            cmd = str(payload.get("command") or "")
            if (str(payload.get("approval_id")) != old_id or not cmd
                    or _cmd_sha(cmd) != expected_sha):
                continue
            # Recovered command is an unexecuted proposal, never authority.
            return {
                "source_task_id": source["task_id"],
                "source_request_id": source["request_id"],
                "source_epoch": source["epoch"],
                "owner": source["owner"],
                "workspace": source["workspace"],
                "action": {"command": cmd,
                           "operation": str(pending.get("operation") or ""),
                           "working_directory": str(payload.get("cwd") or "")},
                "command_sha256": expected_sha,
                "original_action_id": str(pending.get("action_id") or ""),
                "old_approval_id": old_id, "executed": False,
                "recorded_at": 0,
            }
        return None

    def begin(self, *, request: str, owner: str, workspace: str,
              channel_id: str, transport: str, incoming: Mapping):
        """No task enqueue or model request happens until software decision."""
        parsed = parse_continuation(request)
        if not parsed or parsed.get("mode") != "RESUME" or len(parsed["ids"]) != 1:
            return None
        source_id = str(parsed["ids"][0]).upper()
        if not source_id.startswith("TASK-"):
            return None
        source = self.continuations.get(source_id)
        if not source or not source.get("pending_actions"):
            return None
        if (source.get("owner") != owner or not _same_path(source.get("workspace"), workspace)
                or source.get("state") in {"RUNNING", "REGISTERED", "QUEUED", "DISPATCHED"}
                or source.get("lease") or
                (source.get("state") == "COMPLETED" and source.get("task_outcome") == "SUCCESS")):
            raise ResumeApprovalError("resume_approval_source_not_eligible")
        candidate = next((
            item for item in source.get("pending_actions", [])[:16]
            if isinstance(item, dict) and item.get("approval_required")
            and item.get("approval_kind") == "UNKNOWN_COMMAND"
            and item.get("executed") is False
        ), None)
        if not candidate:
            return None
        with exclusive_process_lock(self.lock, label="resume-command"):
            obj = self._load()
            exact = obj["commands"].get(source_id)
            if not isinstance(exact, dict) or exact.get("command_sha256") != candidate.get("command_sha256"):
                exact = self._legacy_command(source, candidate)
            if not exact:
                raise ResumeApprovalError("resume_command_unavailable_exact_command_not_recorded")
            if (exact["owner"] != owner or
                    not _same_path(exact["workspace"], workspace) or
                    exact["source_epoch"] != source["epoch"] or
                    exact["command_sha256"] != _cmd_sha(exact["action"]["command"]) or
                    exact.get("executed") is not False):
                raise ResumeApprovalError("resume_command_evidence_mismatch")
            # New challenge is unique, never the expired APR from old worker.
            nonce = uuid.uuid4().hex
            manifest = {
                "task_id": source_id, "source_epoch": source["epoch"],
                "command_sha256": exact["command_sha256"],
                "operation": exact["action"]["operation"],
                "working_directory": exact["action"]["working_directory"],
                "owner": owner, "workspace": str(Path(workspace).resolve()),
                "nonce": nonce,
            }
            bind = {
                "request_id": source["request_id"], "task_id": source_id,
                "action_id": "RESUME-" + nonce[:18].upper(),
                "action_digest": _hash(manifest),
                "target": exact["action"].get("working_directory") or workspace,
                "manifest_digest": _hash(manifest),
                "chat_id": str(channel_id),
                "permanent_scope": "", "workspace_root": str(Path(workspace).resolve()),
                "approval_kind": "RESUME_UNKNOWN_COMMAND",
            }
            record = self.approvals.request(bind, ttl_sec=TTL_SECONDS)
            challenge = {
                "approval_id": record["approval_id"],
                "source_task_id": source_id,
                "source_request_id": source["request_id"],
                "source_epoch": source["epoch"],
                "owner": owner, "workspace": str(Path(workspace).resolve()),
                "channel_id": str(channel_id), "transport": str(transport),
                "command_sha256": exact["command_sha256"],
                "action": copy.deepcopy(exact["action"]),
                "resume_text": str(request),
                "incoming": dict(incoming),
                "binding": bind,
                "state": "PENDING", "child_task_id": "",
                "expires_at": record["expires_at"],
            }
            obj["challenges"][record["approval_id"]] = challenge
            self._save(obj)
        return self.public(record["approval_id"])

    def public(self, approval_id):
        with exclusive_process_lock(self.lock, label="resume-command"):
            challenge = self._load()["challenges"].get(str(approval_id))
        if not challenge:
            return None
        return {k: copy.deepcopy(challenge[k]) for k in (
            "approval_id", "source_task_id", "command_sha256", "action",
            "state", "expires_at", "transport",
        ) if k in challenge}

    def decide(self, approval_id, *, allowed: bool, owner: str, channel_id: str):
        with exclusive_process_lock(self.lock, label="resume-command"):
            obj = self._load()
            ch = obj["challenges"].get(approval_id)
            if not ch:
                raise ResumeApprovalError("resume_approval_not_found")
            if ch["owner"] != owner or ch["channel_id"] != str(channel_id):
                raise ResumeApprovalError("resume_approval_owner_mismatch")
            if ch["state"] != "PENDING":
                raise ResumeApprovalError("resume_approval_already_decided")
            # Original request must still be paused and owned by this requester.
            source = self.continuations.get(ch["source_task_id"])
            if (not source or source["owner"] != owner or source.get("lease")
                    or source.get("state") in {"RUNNING", "REGISTERED", "QUEUED", "DISPATCHED"}):
                raise ResumeApprovalError("resume_source_busy_or_missing")
            record = self.approvals.decide(
                approval_id, approve=allowed,
                chat_id=str(channel_id), actor=str(owner),
            )
            ch["state"] = record["state"]
            ch["decided_at"] = time.time()
            self._save(obj)
            return {"state": ch["state"], "incoming": copy.deepcopy(ch["incoming"]),
                    "resume_text": ch["resume_text"],
                    "source_task_id": ch["source_task_id"],
                    "approval_id": approval_id}

    def bind_child(self, approval_id: str, *, child_task_id: str, owner: str):
        with exclusive_process_lock(self.lock, label="resume-command"):
            obj = self._load()
            ch = obj["challenges"].get(approval_id)
            if (not ch or ch["owner"] != owner or
                    ch["state"] not in {"APPROVED", "REJECTED"}):
                raise ResumeApprovalError("resume_approval_not_decided")
            if ch["child_task_id"] and ch["child_task_id"] != child_task_id:
                raise ResumeApprovalError("resume_approval_already_bound")
            ch["child_task_id"] = str(child_task_id)
            self._save(obj)

    def consume_for_action(self, *, loop, action: Mapping):
        """Return matching one-time decision, or None; never bypass Admission."""
        source = getattr(loop, "continuation_source", None) or {}
        parent_id = str(source.get("task_id") or "")
        if not parent_id:
            return None
        candidate = _action_key(action)
        if not candidate["command"]:
            return None
        owner = str(loop.continuation_owner or "")
        workspace = str(Path(loop.workspace).resolve())
        with exclusive_process_lock(self.lock, label="resume-command"):
            obj = self._load()
            for approval_id, ch in obj["challenges"].items():
                if (ch.get("source_task_id") != parent_id or
                        ch.get("child_task_id") != loop.task_id or
                        ch.get("owner") != owner or
                        not _same_path(ch["workspace"], workspace) or
                        ch.get("state") not in {"APPROVED", "REJECTED"} or
                        ch["action"] != candidate or
                        ch["command_sha256"] != _cmd_sha(candidate["command"])):
                    continue
                if ch["state"] == "REJECTED":
                    ch["state"] = "CONSUMED_DENIAL"
                    self._save(obj)
                    return {"state": "DENIED", "reason": "resume_user_rejected",
                            "approval_id": approval_id,
                            "command_sha256": ch["command_sha256"]}
                try:
                    self.approvals.consume(approval_id, ch["binding"])
                except SecurityApprovalError as exc:
                    ch["state"] = "EXPIRED"
                    self._save(obj)
                    return {"state": "DENIED", "reason": str(exc),
                            "approval_id": approval_id,
                            "command_sha256": ch["command_sha256"]}
                ch["state"] = "CONSUMED"
                self._save(obj)
                return {"state": "APPROVED", "reason": "resume_preapproval_consumed_once",
                        "approval_id": approval_id,
                        "command_sha256": ch["command_sha256"]}
        return None


def format_resume_approval_prompt(ch: Mapping) -> str:
    return (
        "🔐 接續任務：請先選擇這條未執行指令\n"
        f"原任務：{ch.get('source_task_id', '')}\n"
        f"操作分類：{ch.get('action', {}).get('operation', '')}\n"
        f"工作目錄：{ch.get('action', {}).get('working_directory', '')}\n"
        f"指令：{ch.get('action', {}).get('command', '')}\n"
        f"Command SHA256：{ch.get('command_sha256', '')}\n"
        "請選擇「允許執行一次」或「拒絕」。選擇會先保存，再開始接續。\n"
        "授權只對相同命令與本次接續有效，不會跳過原有安全、Workspace、ACL 或驗證檢查。\n"
        f"approval_id: {ch.get('approval_id', '')}"
    )
