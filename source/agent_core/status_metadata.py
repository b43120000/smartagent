#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Carrier-neutral local status metadata produced from software/UI evidence."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .paths import status_metadata_path

STATUS_STORE_VERSION = 1
DEFAULT_STATUS_PATH = status_metadata_path()
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}

STAGE_LABELS = {
    "IDLE": "等待任務", "ROUTING": "判定任務路由",
    "PROCESS_START": "Agent1 worker 已啟動", "TASK_ADOPTED": "已接手 RemoteAgent 任務",
    "CDP_CONNECTING": "準備 Agent1 瀏覽器", "PAGE_START_WAIT": "等待配置獨立網頁",
    "PAGE_STARTING": "建立獨立網頁", "PAGE_READY": "獨立網頁已就緒",
    "LOCAL_PREPARING": "LocalAgent 準備任務中",
    "PREPARING_PROMPT": "準備傳送訊息", "PREPARING_ATTACHMENTS": "準備附件",
    "UPLOADING_ATTACHMENT": "上傳附件中", "ATTACHMENT_READY": "附件已就緒",
    "SUBMITTING": "傳送訊息中", "WAITING_BRAIN": "等待 ChatGPT 開始回覆",
    "BRAIN_THINKING": "ChatGPT 思考中", "BRAIN_RESPONDING": "ChatGPT 回覆生成中",
    "BRAIN_GENERATING_IMAGE": "ChatGPT 圖片生成中",
    "RESPONSE_RECEIVED": "已收到 ChatGPT 回覆",
    "VALIDATING_PROTOCOL": "驗證 SmartAgent 協議",
    "EXECUTING_TOOL": "執行本機工具", "TOOL_COMPLETED": "本機工具執行完成",
    "UI_ESCALATION_REQUIRED": "Agent 2 檢查網頁狀態",
    "UI_ESCALATION_RESULT": "Agent 2 網頁裁決完成",
    "ISSUE_DETECTED": "已記錄 LocalAgent issue",
    "RETURNING_RESULT": "回傳任務結果", "COMPLETED": "任務完成",
    "FAILED": "任務失敗", "CANCELLED": "任務已取消",
}


class StatusPublisher:
    def __init__(self, path: str | Path = DEFAULT_STATUS_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._context: dict[str, Any] = {}
        self._snapshot: dict[str, Any] = {
            "version": STATUS_STORE_VERSION, "state": "IDLE", "stage": "IDLE",
            "message": STAGE_LABELS["IDLE"], "updated_at": time.time(),
        }

    def configure(self, **context: Any) -> None:
        with self._lock:
            self._context.update({k: v for k, v in context.items() if v is not None})

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def publish(self, stage: str, *, state: str = "RUNNING", actor: str = "LOCAL_AGENT",
                message: str = "", progress: dict[str, Any] | None = None,
                detail: str = "", error: str = "", task_phase: str = "",
                observed_state: str = "", ui_confidence: str = "",
                primary_method: str = "", error_code: str = "",
                retryable: bool | None = None, retry_budget: int | None = None) -> dict[str, Any]:
        now = time.time()
        stage = str(stage or "IDLE").upper()
        state = str(state or "RUNNING").upper()
        with self._lock:
            snapshot = {
                "version": STATUS_STORE_VERSION,
                "event_id": "STATUS-" + uuid.uuid4().hex[:12].upper(),
                **self._context,
                "state": state, "stage": stage, "actor": str(actor or "LOCAL_AGENT"),
                "message": str(message or STAGE_LABELS.get(stage, stage)),
                "progress": dict(progress or {}), "detail": str(detail or "")[:1000],
                "error": str(error or "")[:2000], "host_pid": os.getpid(),
                "task_phase": str(task_phase or ""),
                "observed_state": str(observed_state or ""),
                "ui_confidence": str(ui_confidence or ""),
                "primary_method": str(primary_method or ""),
                "error_code": str(error_code or ""),
                "retryable": retryable, "retry_budget": retry_budget,
                "updated_at": now, "heartbeat_at": now,
                "started_at": now if stage == "ROUTING" else self._snapshot.get("started_at", now),
            }
            if state in TERMINAL_STATES:
                snapshot["completed_at"] = now
            self._snapshot = snapshot
            self._save(snapshot)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(dict(snapshot))
            except Exception:
                pass
        return dict(snapshot)

    def _save(self, snapshot: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def display_text(self) -> str:
        snapshot = self.snapshot()
        message = str(snapshot.get("message") or snapshot.get("stage") or "處理中")
        progress = dict(snapshot.get("progress") or {})
        current, total = progress.get("current"), progress.get("total")
        item = str(progress.get("item") or "")
        suffix = f" {current}/{total}" if current is not None and total is not None else ""
        if item:
            suffix += f"：{Path(item).name}"
        return message + suffix


def load_status_snapshot(path: str | Path = DEFAULT_STATUS_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
