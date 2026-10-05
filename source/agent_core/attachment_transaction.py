"""Request-scoped attachment transaction state for SmartAgent v8."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterable


ATTACHMENT_STATES = (
    "INTENT", "STAGED", "UPLOADING", "VISIBLE", "READY", "STABLE",
    "SUBMITTED", "CONSUMED", "RECOVERY_REQUIRED",
)
_TRANSITIONS = {
    "INTENT": {"STAGED", "RECOVERY_REQUIRED"},
    "STAGED": {"UPLOADING", "RECOVERY_REQUIRED"},
    "UPLOADING": {"VISIBLE", "READY", "RECOVERY_REQUIRED"},
    "VISIBLE": {"READY", "RECOVERY_REQUIRED"},
    "READY": {"STABLE", "RECOVERY_REQUIRED"},
    "STABLE": {"SUBMITTED", "RECOVERY_REQUIRED"},
    "SUBMITTED": {"CONSUMED", "RECOVERY_REQUIRED"},
    "CONSUMED": set(),
    "RECOVERY_REQUIRED": set(),
}


class AttachmentTransactionError(RuntimeError):
    pass


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class AttachmentRecord:
    attachment_id: str
    request_id: str
    task_epoch: str
    conversation_id: str
    source_path: str
    logical_filename: str
    size_bytes: int
    sha256: str
    state: str = "INTENT"
    created_at: float = 0.0
    updated_at: float = 0.0
    recovery_reason: str = ""


class AttachmentTransaction:
    """Small durable state machine; it never infers UI readiness."""

    def __init__(self, root: str | Path, *, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / ".agents" / "attachment_transactions.json"
        self.clock = clock
        self.records: dict[str, AttachmentRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for key, raw in dict(payload.get("attachments", {})).items():
            self.records[str(key)] = AttachmentRecord(**raw)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "schema": "SMARTAGENT_ATTACHMENT_TRANSACTION_V1",
            "attachments": {key: asdict(value) for key, value in self.records.items()},
        }, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def begin(self, *, request_id: str, task_epoch: str, conversation_id: str,
              source_path: str | Path, attachment_id: str = "") -> AttachmentRecord:
        path = Path(source_path).expanduser().resolve()
        if not path.is_file():
            raise AttachmentTransactionError(f"attachment_not_file:{path}")
        now = float(self.clock())
        record = AttachmentRecord(
            attachment_id=str(attachment_id or "ATT-" + uuid.uuid4().hex[:16].upper()),
            request_id=str(request_id), task_epoch=str(task_epoch),
            conversation_id=str(conversation_id), source_path=str(path),
            logical_filename=path.name, size_bytes=path.stat().st_size,
            sha256=file_sha256(path), created_at=now, updated_at=now,
        )
        self.records[record.attachment_id] = record
        self._save()
        return record

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        return self.records.get(str(attachment_id))

    def transition(self, attachment_id: str, state: str, *, request_id: str = "",
                   task_epoch: str = "", conversation_id: str = "",
                   reason: str = "") -> AttachmentRecord:
        record = self.records.get(str(attachment_id))
        if record is None:
            raise AttachmentTransactionError("unknown_attachment_id")
        state = str(state).upper()
        if state not in ATTACHMENT_STATES or state not in _TRANSITIONS[record.state]:
            raise AttachmentTransactionError(f"invalid_attachment_transition:{record.state}->{state}")
        if request_id and str(request_id) != record.request_id:
            raise AttachmentTransactionError("attachment_request_scope_mismatch")
        if task_epoch and str(task_epoch) != record.task_epoch:
            raise AttachmentTransactionError("attachment_task_epoch_mismatch")
        if conversation_id and str(conversation_id) != record.conversation_id:
            raise AttachmentTransactionError("attachment_conversation_scope_mismatch")
        record.state = state
        record.updated_at = float(self.clock())
        record.recovery_reason = str(reason or "")
        self._save()
        return record

    def verify_file(self, attachment_id: str, path: str | Path | None = None) -> bool:
        record = self.records.get(str(attachment_id))
        if record is None:
            raise AttachmentTransactionError("unknown_attachment_id")
        target = Path(path or record.source_path).expanduser().resolve()
        return target.is_file() and target.stat().st_size == record.size_bytes and file_sha256(target) == record.sha256

    def validate_submit(self, attachment_ids: Iterable[str], *, request_id: str,
                        task_epoch: str, conversation_id: str) -> list[AttachmentRecord]:
        result = []
        for attachment_id in attachment_ids:
            record = self.get(attachment_id)
            if record is None:
                raise AttachmentTransactionError("unknown_attachment_id")
            if record.request_id != str(request_id) or record.task_epoch != str(task_epoch) or record.conversation_id != str(conversation_id):
                raise AttachmentTransactionError("attachment_request_scope_mismatch")
            if record.state != "STABLE":
                raise AttachmentTransactionError(f"attachment_not_stable:{record.state}")
            if not self.verify_file(record.attachment_id):
                raise AttachmentTransactionError("attachment_identity_mismatch")
            result.append(record)
        return result

    def mark_recovery(self, attachment_id: str, reason: str) -> AttachmentRecord:
        record = self.records.get(str(attachment_id))
        if record is None:
            raise AttachmentTransactionError("unknown_attachment_id")
        if "RECOVERY_REQUIRED" not in _TRANSITIONS[record.state]:
            raise AttachmentTransactionError(f"attachment_terminal:{record.state}")
        record.state = "RECOVERY_REQUIRED"
        record.recovery_reason = str(reason)
        record.updated_at = float(self.clock())
        self._save()
        return record


__all__ = ["ATTACHMENT_STATES", "AttachmentRecord", "AttachmentTransaction", "AttachmentTransactionError", "file_sha256"]
