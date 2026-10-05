"""Adaptive inline/JSON-attachment transport for local tool results."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .payload_budget import (
    INLINE_RESULT_MAX_BYTES,
    RESULT_ATTACHMENT_MAX_BYTES,
    bounded_preview,
    contains_sensitive_text,
    is_low_value_bulk,
    utf8_size,
)
from .result_store import ResultStore


def _workspace(agent) -> Path:
    return Path(getattr(agent, "workspace_root", None) or Path.cwd()).expanduser().resolve()


def _verification_status(text: str) -> str:
    for value in ("PASS", "FAIL", "UNVERIFIED"):
        if f"VERIFICATION_STATUS: {value}" in text:
            return value
    return ""


def _compact_project_sync_result(text: str, workspace: Path) -> str:
    """Keep the full sync evidence locally and return the completion barrier inline."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if not isinstance(payload, dict) or payload.get("schema") != "PROJECT_SYNC_MESSAGE_V1":
        return text

    raw_bytes = text.encode("utf-8")
    ref = ResultStore(workspace / ".agents" / "results").put_text(text)
    runtime = payload.get("project_sync_runtime") or {}
    acknowledged = runtime.get("acknowledged_batches") or {}
    ack_rows = list(acknowledged.values()) if isinstance(acknowledged, dict) else []
    bundle_indexes = sorted({
        int(index)
        for row in ack_rows if isinstance(row, dict)
        for index in (row.get("bundle_indexes") or [])
    })
    summary = {
        "schema": "PROJECT_SYNC_RESULT_SUMMARY_V1",
        "status": payload.get("status", ""),
        "local_status": payload.get("local_status", ""),
        "sync_status": payload.get("sync_status", ""),
        "strategy": payload.get("strategy", ""),
        "snapshot_id": payload.get("snapshot_id", ""),
        "sync_id": runtime.get("sync_id", ""),
        "bundle_count": payload.get("bundle_count", 0),
        "batch_count": payload.get("batch_count", 0),
        "acknowledged_batch_count": len(ack_rows),
        "acknowledged_bundle_count": len(bundle_indexes),
        "acknowledged_bundle_indexes": bundle_indexes,
        "web_batch_ack_ids": [
            str(row.get("web_batch_ack_id", ""))
            for row in ack_rows if isinstance(row, dict) and row.get("web_batch_ack_id")
        ],
        "hashes_valid": bool(payload.get("hashes_valid")),
        "manifest_complete": bool(payload.get("manifest_complete")),
        "atomic_complete": bool(payload.get("atomic_complete")),
        "full_result_ref": ref["result_ref"],
        "full_result_bytes": len(raw_bytes),
        "full_result_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    return (
        "[PROJECT_SYNC_RESULT_SUMMARY]\n"
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n[/PROJECT_SYNC_RESULT_SUMMARY]"
    )


def _safe_id(value: object, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return text[:80] or fallback


def _write_attachment(workspace: Path, tool_call: dict, text: str, ref: dict, agent) -> dict:
    run_id = _safe_id(getattr(agent, "current_run_id", ""), "RUN")
    action_id = _safe_id(tool_call.get("action_id", ""), "ACTION")
    transfer_id = "XFER-" + uuid.uuid4().hex[:16].upper()
    folder = workspace / ".agents" / "result_exchange" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{action_id}-{ref['sha256'][:12]}.json"
    payload = {
        "schema": "SMARTAGENT_RESULT_ATTACHMENT_V1",
        "transfer_id": transfer_id,
        "request_id": str(getattr(agent, "current_request_id", "") or run_id),
        "result_id": ref["result_ref"],
        "action_id": str(tool_call.get("action_id", "") or ""),
        "tool": str(tool_call.get("tool", "") or ""),
        "content_type": "tool_result",
        "content_chars": len(text),
        "content_bytes": ref["result_bytes"],
        "content_sha256": ref["sha256"],
        "verification_status": _verification_status(text),
        "result_purpose": str(tool_call.get("result_purpose", "") or "")[:500],
        "payload": text,
    }
    admitted = getattr(agent, "_v8_admitted_actions", {}).get(
        str(tool_call.get("action_id", "") or ""), {}
    )
    for field in ("request_id", "task_id", "task_epoch", "intent_digest", "action_digest"):
        value = admitted.get(field) or getattr(agent, f"current_{field}", None)
        if value:
            payload[field] = str(value)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temp = path.with_name(path.name + f".{os.getpid()}-{uuid.uuid4().hex}.tmp")
    if len(raw) > RESULT_ATTACHMENT_MAX_BYTES:
        raise ValueError(f"result_attachment_too_large:{len(raw)}")
    temp.write_bytes(raw); temp.replace(path)
    file_sha = hashlib.sha256(raw).hexdigest()
    queue_status = str(agent.queue_attachments([str(path)]) or "")
    if "ERROR" in queue_status.upper() or "失敗" in queue_status:
        raise ValueError(f"result_attachment_staging_failed:{queue_status[:300]}")
    return {"path": str(path), "filename": path.name, "file_sha256": file_sha,
            "file_bytes": len(raw), "transfer_id": transfer_id}


def prepare_tool_result(tool_call: dict, result: object, agent, *, force_attachment: bool = False) -> str:
    """Return inline text or a compact, ACK-correlated attachment reference."""
    text = str(result or "")
    workspace = _workspace(agent)
    if str(tool_call.get("tool", "")) == "project_sync":
        text = _compact_project_sync_result(text, workspace)
    size = utf8_size(text)
    if size <= INLINE_RESULT_MAX_BYTES and not force_attachment:
        return text

    store = ResultStore(workspace / ".agents" / "results")
    ref = store.put_text(text)
    status = _verification_status(text)
    status_line = f"\nVERIFICATION_STATUS: {status}" if status else ""
    common = (
        f"tool={tool_call.get('tool', '')}\n"
        f"action_id={tool_call.get('action_id', '')}\n"
        f"result_ref={ref['result_ref']}\n"
        f"content_bytes={ref['result_bytes']}\n"
        f"content_sha256={ref['sha256']}"
    )

    requested = str(tool_call.get("result_transport", "AUTO") or "AUTO").upper()
    full_result_required = bool(tool_call.get("full_result_required", False))
    result_purpose = str(tool_call.get("result_purpose", "") or "").strip()
    explicit_attachment = bool(
        requested == "ATTACHMENT" and full_result_required and result_purpose
    )
    attachment_allowed = bool(force_attachment or explicit_attachment)
    sensitive = contains_sensitive_text(text)
    low_value_bulk = is_low_value_bulk(tool_call, text)
    local_only = bool(
        sensitive
        or low_value_bulk
        or size > RESULT_ATTACHMENT_MAX_BYTES
        or requested in {"INLINE", "SUMMARY_ONLY"}
        or not attachment_allowed
    )

    if local_only:
        if sensitive:
            reason = "SENSITIVE_CONTENT"
        elif size > RESULT_ATTACHMENT_MAX_BYTES:
            reason = "ATTACHMENT_TOO_LARGE"
        elif low_value_bulk:
            reason = "LOW_VALUE_BULK_OUTPUT"
        elif requested == "SUMMARY_ONLY":
            reason = "SUMMARY_ONLY_REQUESTED"
        elif requested == "INLINE":
            reason = "INLINE_BUDGET_EXCEEDED_LOCAL_ONLY"
        elif requested == "ATTACHMENT" and not explicit_attachment:
            reason = "ATTACHMENT_JUSTIFICATION_REQUIRED"
        elif str(tool_call.get("tool", "")) == "run_command":
            reason = "RUN_COMMAND_OUTPUT_LOCAL_ONLY"
        else:
            reason = "AUTO_LOCAL_ONLY"
        print(
            f"[PayloadGuard] {tool_call.get('tool', '')} 結果 {size} bytes 超過文字上限；"
            f"改用 LOCAL_REF ({reason})。",
            flush=True,
        )
        return (
            "[SMARTAGENT_RESULT_LOCAL_REF]\n"
            + common + f"\nreason={reason}\n"
            + "完整結果已安全保存在本地且未上傳；請依 preview 決策，必要時縮小查詢範圍。\n"
            + "preview:\n" + bounded_preview(text) + status_line
            + "\n[/SMARTAGENT_RESULT_LOCAL_REF]"
        )

    try:
        attachment = _write_attachment(workspace, tool_call, text, ref, agent)
    except ValueError as exc:
        print(
            f"[PayloadGuard] JSON 附件建立/排程失敗，改用 LOCAL_REF：{str(exc)[:160]}",
            flush=True,
        )
        return (
            "[SMARTAGENT_RESULT_LOCAL_REF]\n"
            + common + "\nreason=ATTACHMENT_TOO_LARGE\n"
            + "JSON 封裝後超過附件安全上限；完整結果保留本地，請分頁或縮小範圍。\n"
            + "preview:\n" + bounded_preview(text) + status_line
            + "\n[/SMARTAGENT_RESULT_LOCAL_REF]"
        )
    print(
        f"[PayloadGuard] {tool_call.get('tool', '')} 結果 {size} bytes 超過文字上限；"
        f"已改用 JSON 附件 {attachment['filename']}。",
        flush=True,
    )
    attachment_policy = "RUNTIME_FORCE" if force_attachment else "EXPLICIT_FULL_RESULT"
    purpose_line = result_purpose.replace("\r", " ").replace("\n", " ")[:300]
    return (
        "[SMARTAGENT_RESULT_ATTACHMENT]\n"
        + common
        + f"\ntransfer_id={attachment['transfer_id']}"
        + f"\nfilename={attachment['filename']}"
        + f"\nattachment_sha256={attachment['file_sha256']}"
        + f"\nattachment_bytes={attachment['file_bytes']}"
        + f"\nattachment_policy={attachment_policy}"
        + (f"\nresult_purpose={purpose_line}" if purpose_line else "")
        + "\nreason=INLINE_BUDGET_EXCEEDED"
        + "\n完整結果已改由同一回合的 JSON 附件傳送；讀取並核對後，下一輪 turn_commit 必須 ACK 本輪 RESULT_ID。"
        + status_line
        + "\n[/SMARTAGENT_RESULT_ATTACHMENT]"
    )


__all__ = ["prepare_tool_result"]
