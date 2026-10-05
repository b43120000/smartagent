#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal local-tool context owned by WebAgent, not SmartAgent."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import uuid

from agent_core.attachment_staging import stage_attachments
from agent_core.tools import execute_tool, preview_tool_scope

from .protocol import SUPPORTED_ACTION_TOOLS


class WebAgentToolContext:
    planner_key = "webagent_direct"

    def __init__(self, workspace: str | Path, *, interface_name: str = "webdirect"):
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Workspace 不存在或不是目錄: {root}")
        self.workspace_root = root
        self.interface_name = str(interface_name or "webdirect")
        self.conversation_id = self.interface_name
        self._attachment_session_id = uuid.uuid4().hex
        self._models_registry = {
            self.planner_key: {"provider": "web_scraper", "model": "chatgpt"}
        }
        self._authorized_local_paths: list[str] = []
        self._pending_attachments: list[str] = []
        self._attachment_sha_ledger: set[str] = set()
        self._outbound_artifacts: list[dict] = []
        self._outbound_artifact_sha_ledger: set[str] = set()
        self._chunked_write_manager = None
        self.current_run_id = ""
        self.current_request_id = ""
        self.current_task_id = ""
        self.current_task_epoch = ""
        self.current_intent_digest = ""
        self.current_request_text = ""
        self._v8_admitted_actions: dict[str, dict] = {}
        self._protocol_turn_seq = 0
        self.last_verification_status: str | None = None
        self._project_sync_transport = None
        self._project_sync_receiver = None
        self._project_sync_event_sink = None
        self._security_approval_notifier = None
        self.security_approval_chat_id = ""
        self.security_approval_timeout_sec = 300.0
        self.security_approval_ledger_root = root
        self.artifact_delivery_mode = ""
        self._bootstrap_install_root: Path | None = None

    def enable_bootstrap_install_policy(self, install_root: str | Path) -> None:
        root = Path(install_root).expanduser().resolve()
        if root != self.workspace_root:
            raise ValueError("bootstrap_install_root_must_equal_workspace")
        self._bootstrap_install_root = root

    def _bootstrap_command_argv(self, command: str) -> list[str] | None:
        root = self._bootstrap_install_root
        if root is None:
            return None
        text = str(command or "").strip()
        if re.fullmatch(r"(?i)(?:call\s+)?(?:[.]\\)?InstallCheckList[.]bat\s+--json", text):
            return ["cmd.exe", "/d", "/s", "/c", "call", str(root / "InstallCheckList.bat"), "--json"]
        match = re.fullmatch(
            r"(?i)powershell(?:[.]exe)?\s+-NoLogo\s+-NoProfile\s+-ExecutionPolicy\s+Bypass\s+-File\s+install_smart_agent[\\/]install_milestones[.]ps1\s+-Action\s+Install\s+-ProjectRoot\s+[.]\s+-Milestone\s+(M[1-5])\s+-NonInteractive",
            text,
        )
        if match:
            return [
                "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(root / "install_smart_agent" / "install_milestones.ps1"),
                "-Action", "Install", "-ProjectRoot", str(root),
                "-Milestone", match.group(1).upper(), "-NonInteractive",
            ]
        return []

    def _execute_bootstrap_command(self, action: dict) -> str:
        argv = self._bootstrap_command_argv(str(action.get("command", "") or ""))
        if not argv:
            self.last_verification_status = "FAIL"
            return (
                "[SMARTAGENT_BOOTSTRAP_TOOL_REJECTED] Only these exact commands are allowed: "
                "InstallCheckList.bat --json; "
                "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone M1-M5 -NonInteractive"
            )
        timeout = min(max(int(action.get("timeout", 600) or 600), 30), 1800)
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self._bootstrap_install_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            self.last_verification_status = "PASS" if completed.returncode == 0 else "FAIL"
            return "\n".join(
                (
                    "[COMMAND_RESULT]",
                    f"command: {action.get('command', '')}",
                    f"exit_code: {completed.returncode}",
                    f"stdout:\n{completed.stdout}",
                    f"stderr:\n{completed.stderr}",
                    f"VERIFICATION_STATUS: {self.last_verification_status}",
                )
            )
        except subprocess.TimeoutExpired as exc:
            self.last_verification_status = "FAIL"
            return (
                "[COMMAND_RESULT]\n"
                f"command: {action.get('command', '')}\n"
                f"error: timeout after {timeout}s: {exc}\n"
                "VERIFICATION_STATUS: FAIL"
            )

    def begin_run(self, run_id: str, request: str, authorized_paths: list[str]) -> None:
        self.current_run_id = str(run_id)
        self.current_request_id = str(run_id)
        self.current_request_text = str(request or "")
        self.current_task_id = str(getattr(self, "current_task_id", "") or run_id)
        self.current_task_epoch = str(getattr(self, "current_task_epoch", "") or run_id)
        self._protocol_turn_seq = 0
        self._v8_admitted_actions.clear()
        self.last_verification_status = None
        self._authorized_local_paths = list(dict.fromkeys(str(x) for x in authorized_paths if str(x)))
        self._pending_attachments.clear()
        self._attachment_sha_ledger.clear()
        self._outbound_artifacts.clear()
        self._outbound_artifact_sha_ledger.clear()

    def preview_tool_scope(self, action: dict) -> dict:
        return preview_tool_scope(action, self)

    def queue_attachments(self, paths: list[str], trace_id: str = "") -> str:
        queued = []
        duplicates = []
        errors = []
        request_trace = str(trace_id or self.current_request_id or uuid.uuid4().hex)
        for ordinal, raw in enumerate(paths or [], start=1):
            try:
                item = stage_attachments(
                    self.workspace_root,
                    self.conversation_id,
                    self._attachment_session_id,
                    request_trace,
                    [raw],
                    ordinal_start=ordinal,
                )[0]
            except Exception as exc:
                errors.append(f"{raw}: {type(exc).__name__}: {exc}")
                continue
            if item.sha256 in self._attachment_sha_ledger:
                duplicates.append((item.source_path, item.sha256))
                continue
            self._attachment_sha_ledger.add(item.sha256)
            self._pending_attachments.append(item.staged_path)
            queued.append(item)
        lines = [
            f"[WEBAGENT_ATTACHMENT_QUEUED] original={item.source_path} upload={item.upload_name} sha256={item.sha256}"
            for item in queued
        ]
        lines.extend(
            f"[WEBAGENT_ATTACHMENT_DEDUPED] original={source} sha256={digest}"
            for source, digest in duplicates
        )
        lines.extend(f"[WEBAGENT_ATTACHMENT_ERROR] {value}" for value in errors)
        return "\n".join(lines) if lines else "[WEBAGENT_ATTACHMENT_ERROR] 沒有可排程的檔案"

    def take_pending_attachments(self) -> list[str]:
        values = list(self._pending_attachments)
        self._pending_attachments.clear()
        return values

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def queue_outbound_artifact(
        self,
        path: str,
        *,
        kind: str = "",
        caption: str = "",
    ) -> str:
        """Queue a file for the originating Telegram transport, not WebGPT."""
        if self.interface_name != "remote":
            return "[TELEGRAM_ARTIFACT_REJECTED] source transport is not RemoteAgent"
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.workspace_root / target
        target = target.resolve()
        try:
            target.relative_to(self.workspace_root)
        except ValueError:
            return f"[TELEGRAM_ARTIFACT_REJECTED] path escapes workspace: {target}"
        if not target.is_file():
            return f"[TELEGRAM_ARTIFACT_REJECTED] file not found: {target}"
        digest = self._file_sha256(target)
        if digest in self._outbound_artifact_sha_ledger:
            return f"[TELEGRAM_ARTIFACT_DEDUPED] path={target} sha256={digest}"
        self._outbound_artifact_sha_ledger.add(digest)
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"photo", "image", "document"}:
            normalized_kind = (
                "photo" if target.suffix.lower() in {".jpg", ".jpeg", ".png"}
                else "document"
            )
        self._outbound_artifacts.append({
            "path": str(target),
            "kind": normalized_kind,
            "name": target.name,
            "caption": str(caption or ""),
            "workspace": str(self.workspace_root),
            "size_bytes": target.stat().st_size,
            "sha256": digest,
        })
        return (
            f"[TELEGRAM_ARTIFACT_QUEUED] name={target.name} "
            f"size_bytes={target.stat().st_size} sha256={digest}"
        )

    def take_outbound_artifacts(self) -> list[dict]:
        values = [dict(row) for row in self._outbound_artifacts]
        self._outbound_artifacts.clear()
        return values

    def configure_project_sync_transport(self, transport) -> None:
        self._project_sync_transport = transport

    def configure_project_sync_receiver(self, identity_provider) -> None:
        self._project_sync_receiver = identity_provider

    def configure_security_approval(
        self, notifier, *, chat_id: str = "", timeout_sec: float = 300.0
    ) -> None:
        self._security_approval_notifier = notifier
        self.security_approval_chat_id = str(chat_id or "")
        self.security_approval_timeout_sec = max(30.0, float(timeout_sec))

    def notify_security_approval(self, record: dict, manifest: dict) -> None:
        if self._security_approval_notifier is None:
            return
        self._security_approval_notifier(dict(record), dict(manifest))

    def enable_console_security_approval(self) -> None:
        def prompt(record: dict, manifest: dict) -> None:
            from agent_core.security_approval import SecurityApprovalLedger
            print("\n[WebAgent 安全確認]", flush=True)
            print(f"路徑：{manifest.get('target', '')}", flush=True)
            print(f"項目：{manifest.get('entry_count', 0)}，大小：{manifest.get('total_bytes', 0)} bytes", flush=True)
            answer = input(f"輸入 {record.get('approval_id')} 才會執行；其他輸入視為拒絕：").strip()
            SecurityApprovalLedger(self.security_approval_ledger_root).decide(
                str(record.get("approval_id", "")),
                approve=answer == str(record.get("approval_id", "")),
                chat_id="", actor="webdirect-console",
            )
        self.configure_security_approval(prompt)

    def run_project_sync_transaction(self, plan: dict, *, project_root=None) -> dict:
        if self._project_sync_transport is None:
            raise RuntimeError("project_sync_transport_not_configured")
        from agent_core.project_sync_runner import run_project_sync_transaction

        from agent_core.project_sync_receiver import bind_receiver_transport

        identity, transport = bind_receiver_transport(
            self._project_sync_transport,
            self._project_sync_receiver or (lambda: self.conversation_id),
        )
        return run_project_sync_transaction(
            project_root or self.workspace_root,
            plan,
            transport,
            interface_name=self.interface_name,
            conversation_id=identity,
            session_id=self._attachment_session_id,
            request_id=self.current_request_id,
            event_sink=self._project_sync_event_sink,
        )

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
                request_id=self.current_request_id,
            )
        except Exception as exc:
            return f"[ARTIFACT_DOWNLOAD_FAILED] {type(exc).__name__}: {exc}"
        status = str((result or {}).get("status", "") or "")
        succeeded = status.upper() in {"SUCCESS", "DOWNLOADED", "ARTIFACT_DOWNLOAD_SUCCESS"}
        if succeeded and self.interface_name == "remote" and self.artifact_delivery_mode == "telegram":
            downloaded_path = str((result or {}).get("path", "") or "")
            if downloaded_path:
                result = dict(result)
                result["telegram_queue"] = self.queue_outbound_artifact(
                    downloaded_path, kind="photo"
                )
        marker = "ARTIFACT_DOWNLOAD_SUCCESS" if succeeded else "ARTIFACT_DOWNLOAD_RESULT"
        return f"[{marker}] {result}"

    def execute(self, action: dict) -> str:
        tool = str(action.get("tool", "") or "")
        if tool not in SUPPORTED_ACTION_TOOLS:
            return f"[WEBAGENT_TOOL_REJECTED] unsupported_tool={tool}"
        if self._bootstrap_install_root is not None:
            if tool == "run_command":
                return self._execute_bootstrap_command(action)
            if tool == "read_file":
                requested = Path(str(action.get("path", "") or "")).expanduser().resolve()
                allowed = self._bootstrap_install_root / "install_smart_agent" / "POST_BOOTSTRAP_SETUP.md"
                if requested == allowed.resolve():
                    return execute_tool(action, agent=self, models=self._models_registry)
            return (
                f"[SMARTAGENT_BOOTSTRAP_TOOL_REJECTED] tool={tool}; "
                "allowed_tools=read_file(manifest only),run_command(fixed installer commands only)"
            )
        return execute_tool(action, agent=self, models=self._models_registry)
