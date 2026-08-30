#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal local-tool context owned by WebAgent, not SmartAgent."""
from __future__ import annotations

from pathlib import Path

from agent_core.tools import execute_tool

from .protocol import SUPPORTED_ACTION_TOOLS


class WebAgentToolContext:
    planner_key = "webagent_direct"

    def __init__(self, workspace: str | Path):
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Workspace 不存在或不是目錄: {root}")
        self.workspace_root = root
        self._models_registry = {
            self.planner_key: {"provider": "web_scraper", "model": "chatgpt"}
        }
        self._authorized_local_paths: list[str] = []
        self._pending_attachments: list[str] = []
        self._chunked_write_manager = None
        self.current_run_id = ""
        self.current_request_id = ""
        self._protocol_turn_seq = 0
        self.last_verification_status: str | None = None

    def begin_run(self, run_id: str, request: str, authorized_paths: list[str]) -> None:
        self.current_run_id = str(run_id)
        self.current_request_id = str(run_id)
        self._protocol_turn_seq = 0
        self.last_verification_status = None
        self._authorized_local_paths = list(dict.fromkeys(str(x) for x in authorized_paths if str(x)))
        self._pending_attachments.clear()

    def queue_attachments(self, paths: list[str]) -> str:
        queued = []
        errors = []
        for raw in paths or []:
            candidate = Path(str(raw)).expanduser().resolve()
            if not candidate.exists() or not candidate.is_file():
                errors.append(f"附件不存在或不是檔案: {candidate}")
                continue
            value = str(candidate)
            if value not in self._pending_attachments:
                self._pending_attachments.append(value)
            queued.append(value)
        lines = [f"[WEBAGENT_ATTACHMENT_QUEUED] {value}" for value in queued]
        lines.extend(f"[WEBAGENT_ATTACHMENT_ERROR] {value}" for value in errors)
        return "\n".join(lines) if lines else "[WEBAGENT_ATTACHMENT_ERROR] 沒有可排程的檔案"

    def take_pending_attachments(self) -> list[str]:
        values = list(self._pending_attachments)
        self._pending_attachments.clear()
        return values

    def web_download_artifact(
        self,
        output_path: str,
        expected_filename: str = "",
        timeout: int = 45,
    ) -> str:
        if not str(output_path or "").strip():
            return "[ARTIFACT_DOWNLOAD_FAILED] download_artifact 需要 output_path"
        from agent_core.web_runtime import get_manager

        try:
            result = get_manager().download_latest_artifact(
                "chatgpt",
                output_path,
                expected_filename=expected_filename,
                timeout_sec=float(timeout),
            )
        except Exception as exc:
            return f"[ARTIFACT_DOWNLOAD_FAILED] {type(exc).__name__}: {exc}"
        status = str((result or {}).get("status", "") or "")
        marker = "ARTIFACT_DOWNLOAD_SUCCESS" if status.upper() in {"SUCCESS", "DOWNLOADED"} else "ARTIFACT_DOWNLOAD_RESULT"
        return f"[{marker}] {result}"

    def execute(self, action: dict) -> str:
        tool = str(action.get("tool", "") or "")
        if tool not in SUPPORTED_ACTION_TOOLS:
            return f"[WEBAGENT_TOOL_REJECTED] unsupported_tool={tool}"
        return execute_tool(action, agent=self, models=self._models_registry)
