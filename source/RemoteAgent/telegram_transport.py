#!/usr/bin/env python3
from __future__ import annotations

"""Telegram Bot API transport. Disabled until explicit local configuration."""
from agent_core import path_integrity_trace as PIT

import json
import mimetypes
import hashlib
import shutil
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from agent_core.transport_message import InboundAttachment, NormalizedInboundMessage
from RemoteAgent.telegram_pairing import TelegramPairingStore
from RemoteAgent.remote_feature_catalog import (
    FEATURE_QUERY_COMMAND, canonical_remote_control, command_for_remote_callback,
    remote_feature_inline_keyboard, remote_feature_reply_keyboard,
    render_remote_feature_list, INTERRUPT_COMMAND, LIST_SKILLS_COMMAND,
    MANAGER_COMMAND, STATUS_COMMAND, CMD_CONTROL_LABEL,
)
from RemoteAgent.telegram_direct_cmd import is_direct_cmd_request
from RemoteAgent.remote_restart import RECONNECT_COMMAND
from agent_core.remote_binding_manager import RemoteBindingManager
from agent_core.paths import workspace_telegram_inbox_root
from agent_core.remote_skill_manager import RemoteSkillError, RemoteSkillManager


def _parse_ids(value: str) -> tuple[int, ...]:
    output = []
    for item in str(value or "").replace(";", ",").split(","):
        item = item.strip()
        if item:
            output.append(int(item))
    return tuple(sorted(set(output)))


@dataclass(frozen=True)
class TelegramReceiverConfig:
    enabled: bool = False
    bot_token: str = field(default="", repr=False)
    allowed_user_ids: tuple[int, ...] = ()
    allowed_chat_ids: tuple[int, ...] = ()
    workspace: str = ""
    poll_timeout_sec: int = 25
    retry_delay_sec: float = 2.0
    api_base: str = "https://api.telegram.org"
    pairing_enabled: bool = False
    pairing_ttl_sec: int = 300

    @classmethod
    def from_env(cls) -> "TelegramReceiverConfig":
        enabled = os.environ.get("SMARTAGENT_TELEGRAM_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            bot_token=os.environ.get("SMARTAGENT_TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=_parse_ids(os.environ.get("SMARTAGENT_TELEGRAM_ALLOWED_USER_IDS", "")),
            allowed_chat_ids=_parse_ids(os.environ.get("SMARTAGENT_TELEGRAM_ALLOWED_CHAT_IDS", "")),
            workspace=os.environ.get("SMARTAGENT_TELEGRAM_WORKSPACE", "").strip(),
            poll_timeout_sec=max(1, min(50, int(os.environ.get("SMARTAGENT_TELEGRAM_POLL_TIMEOUT_SEC", "25")))),
            pairing_enabled=os.environ.get("SMARTAGENT_TELEGRAM_PAIRING_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
            pairing_ttl_sec=max(60, min(900, int(os.environ.get("SMARTAGENT_TELEGRAM_PAIR_TTL_SEC", "300")))),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.bot_token:
            raise ValueError("telegram_bot_token_missing")
        static_allowlist_complete = bool(self.allowed_user_ids and self.allowed_chat_ids)
        static_allowlist_partial = bool(self.allowed_user_ids) != bool(self.allowed_chat_ids)
        if static_allowlist_partial:
            raise ValueError("telegram_allowlist_incomplete")
        if not static_allowlist_complete and not self.pairing_enabled:
            raise ValueError("telegram_allowlist_missing")
        if not self.workspace or not Path(self.workspace).is_dir():
            raise ValueError("telegram_workspace_unavailable")


class TelegramBotClient:
    def __init__(self, config: TelegramReceiverConfig, *, opener: Callable[..., Any] = urllib.request.urlopen):
        self.config = config
        self._opener = opener

    def _call(self, method: str, payload: dict[str, Any], *, timeout: float) -> Any:
        url = f"{self.config.api_base.rstrip('/')}/bot{self.config.bot_token}/{method}"
        encoded = urllib.parse.urlencode({
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple)) else value
            for key, value in payload.items()
            if value is not None
        }).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            description = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    description = str(parsed.get("description", "") or "")
            except Exception:
                description = ""
            detail = description[:300] or str(getattr(exc, "reason", "") or "HTTP error")[:300]
            raise RuntimeError(
                f"telegram_api_http_error:{int(exc.code)}:{detail}"
            ) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError(f"telegram_api_error:{str(body.get('description', 'unknown'))[:300] if isinstance(body, dict) else 'invalid_response'}")
        return body.get("result")

    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {
                "offset": int(offset),
                "timeout": int(timeout),
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=float(timeout) + 10.0,
        )
        return list(result or [])

    def get_me(self) -> dict[str, Any]:
        return dict(self._call("getMe", {}, timeout=20.0) or {})

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "reply_parameters": {"message_id": int(reply_to_message_id)} if reply_to_message_id else None,
                "reply_markup": reply_markup,
            },
            timeout=20.0,
        )
        return dict(result or {})

    def answer_callback_query(
        self, callback_query_id: str, *, text: str = ""
    ) -> bool:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": str(callback_query_id), "text": str(text) or None},
            timeout=20.0,
        )
        return True

    def get_file(self,file_id:str)->dict[str,Any]:
        return dict(self._call("getFile",{"file_id":str(file_id)},timeout=20.0) or {})

    def download_file(self,file_id:str,destination:str|Path)->Path:
        info=self.get_file(file_id); remote=str(info.get("file_path","") or "").strip()
        if not remote: raise RuntimeError("telegram_file_path_missing")
        target=Path(destination); target.parent.mkdir(parents=True,exist_ok=True)
        url=f"{self.config.api_base.rstrip('/')}/file/bot{self.config.bot_token}/{remote.lstrip('/')}"
        with self._opener(urllib.request.Request(url,method="GET"),timeout=60.0) as response: data=response.read()
        target.write_bytes(data); return target

    def _send_multipart(self,method:str,*,chat_id:int,field_name:str,file_path:str|Path,caption:str="",reply_to_message_id:int|None=None)->dict[str,Any]:
        path=Path(file_path)
        if not path.is_file(): raise FileNotFoundError(str(path))
        boundary="----SmartAgent"+uuid.uuid4().hex; parts=[]; fields={"chat_id":str(int(chat_id))}
        if caption: fields["caption"]=str(caption)
        if reply_to_message_id: fields["reply_parameters"]=json.dumps({"message_id":int(reply_to_message_id)},ensure_ascii=False)
        for k,v in fields.items(): parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head=f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        parts += [head+path.read_bytes()+b"\r\n",f"--{boundary}--\r\n".encode()]
        url=f"{self.config.api_base.rstrip('/')}/bot{self.config.bot_token}/{method}"; req=urllib.request.Request(url,data=b''.join(parts),method='POST'); req.add_header('Content-Type',f'multipart/form-data; boundary={boundary}')
        upload_timeout=max(60.0,float(os.environ.get("SMARTAGENT_TELEGRAM_UPLOAD_TIMEOUT_SEC","300")))
        with self._opener(req,timeout=upload_timeout) as response: body=json.loads(response.read().decode())
        if not isinstance(body,dict) or not body.get('ok'): raise RuntimeError('telegram_api_error:'+str(body.get('description','unknown')))
        return dict(body.get('result') or {})

    def send_document(self,chat_id:int,file_path:str|Path,*,caption:str="",reply_to_message_id:int|None=None)->dict[str,Any]:
        return self._send_multipart('sendDocument',chat_id=chat_id,field_name='document',file_path=file_path,caption=caption,reply_to_message_id=reply_to_message_id)

    def send_photo(self,chat_id:int,file_path:str|Path,*,caption:str="",reply_to_message_id:int|None=None)->dict[str,Any]:
        return self._send_multipart('sendPhoto',chat_id=chat_id,field_name='photo',file_path=file_path,caption=caption,reply_to_message_id=reply_to_message_id)


class TelegramOffsetStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.next_offset = 0
        self.load()

    def load(self) -> int:
        with self._lock:
            if not self.path.exists():
                self.next_offset = 0
                return 0
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.next_offset = max(0, int(payload.get("next_offset", 0) or 0))
            return self.next_offset

    def commit(self, update_id: int) -> int:
        with self._lock:
            proposed = max(self.next_offset, int(update_id) + 1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            temp.write_text(
                json.dumps({"version": 1, "next_offset": proposed}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self.path)
            # Memory cursor advances ONLY after the durable replace succeeds.
            # A transient disk error must not skip a Telegram update in-process.
            self.next_offset = proposed
            return self.next_offset


class TelegramUpdateAdapter:
    def __init__(self, config: TelegramReceiverConfig, *, pairing_store: TelegramPairingStore | None = None):
        self.config = config
        self.pairing_store = pairing_store

    @staticmethod
    def _private_non_bot(message: dict[str, Any]) -> tuple[int, int] | None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = int(chat.get("id", 0) or 0)
        user_id = int(sender.get("id", 0) or 0)
        if sender.get("is_bot") or str(chat.get("type", "")) != "private" or user_id <= 0 or chat_id <= 0:
            return None
        return user_id, chat_id

    def is_authorized(self, *, user_id: int, chat_id: int) -> bool:
        static = user_id in self.config.allowed_user_ids and chat_id in self.config.allowed_chat_ids
        paired = self.pairing_store is not None and self.pairing_store.is_paired(user_id=user_id, chat_id=chat_id)
        return bool(static or paired)

    @staticmethod
    def _attachments(message: dict[str, Any]) -> tuple[InboundAttachment, ...]:
        rows: list[InboundAttachment] = []
        document = message.get("document")
        if isinstance(document, dict):
            rows.append(InboundAttachment(
                kind="document",
                transport_id=str(document.get("file_id", "")),
                file_name=str(document.get("file_name", "")),
                mime_type=str(document.get("mime_type", "")),
                size=int(document.get("file_size", 0) or 0),
                file_unique_id=str(document.get("file_unique_id", "")),
            ))
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = photos[-1] if isinstance(photos[-1], dict) else {}
            rows.append(InboundAttachment(
                kind="photo",
                transport_id=str(photo.get("file_id", "")),
                size=int(photo.get("file_size", 0) or 0),
                file_unique_id=str(photo.get("file_unique_id", "")),
            ))
        return tuple(rows)

    def normalize(self, update: dict[str, Any]) -> tuple[NormalizedInboundMessage | None, str]:
        try:
            update_id = int(update["update_id"])
        except Exception:
            return None, "malformed_update"
        message = update.get("message")
        if not isinstance(message, dict):
            return None, "ignored_update"
        identity = self._private_non_bot(message)
        if identity is None:
            sender = message.get("from") if isinstance(message.get("from"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            return None, "bot_sender_rejected" if sender.get("is_bot") else "non_private_chat_rejected"
        user_id, chat_id = identity
        if not self.is_authorized(user_id=user_id, chat_id=chat_id):
            return None, "sender_not_allowed"
        attachments = self._attachments(message)
        raw_text = str(message.get("text") or message.get("caption") or "")
        is_admin_cmd = is_direct_cmd_request(raw_text)
        visible_trace = "[TG_DIRECT_CMD_REDACTED]" if is_admin_cmd else raw_text
        PIT.begin(visible_trace, transport="LOCAL_TEST" if self.config.bot_token == "LOCAL-TEST-NO-NETWORK" else "TELEGRAM")
        PIT.record("02_telegram_message_text", visible_trace, extra={
            "update_id": update_id, "message_id": message.get("message_id"),
            "direct_cmd_redacted": is_admin_cmd,
            "entities": [] if is_admin_cmd else message.get("entities", []),
            "caption_entities": [] if is_admin_cmd else message.get("caption_entities", []),
        })
        text = raw_text.strip()
        PIT.record("03_receiver_normalized",
                   "[TG_DIRECT_CMD_REDACTED]" if is_admin_cmd else text,
                   evidence_status="NORMALIZED_INPUT")
        if not text and not attachments:
            return None, "empty_message"
        if not text: text = "請處理附件"
        message_id = int(message.get("message_id", 0) or 0)
        reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        normalized = NormalizedInboundMessage(
            transport="TELEGRAM",
            endpoint="api.telegram.org",
            conversation_key=f"telegram://chat/{chat_id}",
            sender_id=str(user_id),
            source_message_id=str(message_id),
            text=text,
            received_at=float(message.get("date", time.time()) or time.time()),
            idempotency_key=f"TELEGRAM|api.telegram.org|{chat_id}|{update_id}",
            reply_context={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_to_message_id": int(reply.get("message_id", 0) or 0),
            },
            attachments=attachments,
            metadata={"update_id": update_id},
        )
        return normalized, ""


class TelegramReceiver:
    def __init__(
        self,
        *,
        config: TelegramReceiverConfig,
        client: TelegramBotClient,
        offset_store: TelegramOffsetStore,
        ingress,
        runtime_log=None,
        pairing_store: TelegramPairingStore | None = None,
        control_handler=None,
        accepted_handler=None,
        task_admission_guard=None,
        install_root: str | Path | None = None,
    ):
        config.validate()
        self.config = config
        self.client = client
        self.offset_store = offset_store
        self.ingress = ingress
        self.runtime_log = runtime_log
        self.pairing_store = pairing_store
        self.control_handler = control_handler
        self.accepted_handler = accepted_handler
        self.task_admission_guard = task_admission_guard
        # Must match the WebAgent worker's root, not the task workspace.
        self.install_root = Path(install_root or Path(__file__).resolve().parents[1]).resolve()
        # Serializes one normalized Telegram update through durable enqueue
        # against a workspace binding hot-swap.  HostSupervisor acquires this
        # before the task-store lock, matching this receiver's lock order.
        self.binding_lock = threading.RLock()
        self.adapter = TelegramUpdateAdapter(config, pairing_store=pairing_store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control_threads: set[threading.Thread] = set()
        self._control_threads_lock = threading.Lock()
        self.last_poll_started_at = 0.0
        self.last_poll_success_at = 0.0
        self.last_poll_error_at = 0.0
        self.last_poll_error = ""

    def _log(self, event: str, **detail) -> None:
        if self.runtime_log is not None:
            self.runtime_log.write(event, component="telegram_receiver", **detail)

    def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        """Send enhanced UI while tolerating legacy injected test clients."""
        try:
            return self.client.send_message(
                chat_id,
                text,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
        except TypeError as exc:
            if "reply_markup" not in str(exc):
                raise
            return self.client.send_message(
                chat_id, text, reply_to_message_id=reply_to_message_id
            )

    def _resume_approval_gate(self):
        from agent_core.resume_command_approval import ResumeCommandGate
        return ResumeCommandGate(self.install_root)

    def _resume_owner(self, message: NormalizedInboundMessage) -> str:
        chat = str((message.reply_context or {}).get("chat_id") or "")
        return "telegram:" + str(message.sender_id) + ":" + chat

    def _offer_pre_resume_decision(self, message: NormalizedInboundMessage,
                                    *, update_id: int) -> bool:
        """Only software is allowed to decide whether to show this prompt."""
        from agent_core.resume_command_approval import (
            ResumeApprovalError, format_resume_approval_prompt,
        )
        from RemoteAgent.remote_feature_catalog import security_approval_inline_keyboard
        from agent_core.task_continuation import parse_continuation
        try:
            request = parse_continuation(message.text)
            if not request or request.get("mode") != "RESUME":
                return False
            channel_id = str(message.reply_context.get("chat_id") or "")
            original = {
                "transport": message.transport, "endpoint": message.endpoint,
                "conversation_key": message.conversation_key,
                "sender_id": message.sender_id,
                "source_message_id": message.source_message_id,
                "text": message.text, "received_at": message.received_at,
                "idempotency_key": message.idempotency_key,
                "reply_context": dict(message.reply_context),
                "metadata": dict(message.metadata),
            }
            prompt = self._resume_approval_gate().begin(
                request=message.text,
                owner=self._resume_owner(message),
                workspace=self.config.workspace,
                channel_id=channel_id, transport="TELEGRAM", incoming=original,
            )
            if prompt is None:
                return False
            self._send_message(
                int(channel_id), format_resume_approval_prompt(prompt),
                reply_to_message_id=int(message.reply_context.get("message_id") or 0) or None,
                reply_markup=security_approval_inline_keyboard(
                    prompt["approval_id"], approval_kind="RESUME_UNKNOWN_COMMAND",
                ),
            )
            self._log("CONNECT", stage="PRE_RESUME_APPROVAL_PROMPT",
                      source_task_id=prompt["source_task_id"],
                      approval_id=prompt["approval_id"], update_id=update_id)
            return True
        except (ResumeApprovalError, ValueError) as exc:
            self._send_message(
                int(message.reply_context["chat_id"]),
                "🔴 無法預先顯示接續授權：" + str(exc)[:400]
                + "\n不會自動授權或下發該指令。",
                reply_to_message_id=int(message.reply_context["message_id"]),
            )
            self._log("ERROR", stage="PRE_RESUME_APPROVAL_BLOCKED",
                      reason=str(exc)[:150], update_id=update_id)
            return True

    def _process_pre_resume_choice(self, message: NormalizedInboundMessage,
                                    *, update_id: int) -> bool:
        """Consume an already authenticated Telegram callback/choice."""
        match = re.fullmatch(
            r"(security_approve_once|security_reject_once)\s+(APR-[A-F0-9]{20})",
            str(message.text or ""), re.I,
        )
        if not match:
            return False
        approval_id = match.group(2).upper()
        gate = self._resume_approval_gate()
        challenge = gate.public(approval_id)
        if not challenge or challenge.get("transport") != "TELEGRAM":
            return False  # Existing non-resume security action.
        from agent_core.resume_command_approval import ResumeApprovalError
        try:
            outcome = gate.decide(
                approval_id, allowed=match.group(1).lower() == "security_approve_once",
                owner=self._resume_owner(message),
                channel_id=str(message.reply_context["chat_id"]),
            )
            fields = outcome["incoming"]
            original = NormalizedInboundMessage(
                transport=str(fields["transport"]),
                endpoint=str(fields["endpoint"]),
                conversation_key=str(fields["conversation_key"]),
                sender_id=str(fields["sender_id"]),
                source_message_id=str(fields["source_message_id"]),
                text=str(fields["text"]),
                received_at=float(fields["received_at"]),
                idempotency_key=str(fields["idempotency_key"]),
                reply_context=dict(fields["reply_context"]),
                metadata={**dict(fields.get("metadata") or {}),
                          "resume_approval_id": approval_id},
            )
            # Task admission is checked again at click time. Approval is not a
            # replacement for Software Guard or execution authorization.
            if self.task_admission_guard is not None:
                check = self.task_admission_guard(original)
                granted = bool(check[0]) if isinstance(check, tuple) else bool(check)
                if not granted:
                    raise ResumeApprovalError("resume_task_admission_denied")
            accepted = self.ingress.accept(original, workspace=self.config.workspace)
            task = accepted.task
            if task is None:
                raise ResumeApprovalError("resume_ingress_task_missing")
            gate.bind_child(approval_id, child_task_id=task.task_id,
                            owner=self._resume_owner(message))
            self._send_message(
                int(message.reply_context["chat_id"]),
                ("✅ 已記錄允許一次" if outcome["state"] == "APPROVED" else "⛔ 已記錄拒絕")
                + f"，開始接續任務 {task.task_id}。\n"
                "後續仍會重新檢查 Command Admission；不會執行超出授權範圍的操作。",
                reply_to_message_id=int(message.reply_context["message_id"]),
            )
            if bool(getattr(accepted, "created", True)) and self.accepted_handler:
                self.accepted_handler(task)
            self._log("CONNECT", stage="PRE_RESUME_DECISION_ENQUEUED",
                      approval_id=approval_id, source_task_id=outcome["source_task_id"],
                      task_id=task.task_id, decision=outcome["state"],
                      update_id=update_id)
        except (ResumeApprovalError, ValueError, KeyError) as exc:
            self._send_message(
                int(message.reply_context["chat_id"]),
                "🔴 接續授權無法啟動：" + str(exc)[:500]
                + "\n原命令未執行；可重新送出接續工作指令。",
                reply_to_message_id=int(message.reply_context["message_id"]),
            )
            self._log("ERROR", stage="PRE_RESUME_DECISION_REJECTED",
                      approval_id=approval_id, reason=str(exc)[:120], update_id=update_id)
        return True

    def discard_pending_updates_for_clean_start(self) -> int:
        """Advance the Telegram cursor past the pre-start backlog.

        A runtime clean start must not replay messages that were already in
        Telegram's unconfirmed queue before this listener generation existed.
        ``offset=-1`` asks Bot API for the newest pending update; committing
        that id confirms the whole older backlog while the next normal poll
        starts at a clean boundary.
        """
        updates = self.client.get_updates(offset=-1, timeout=0)
        latest = max(
            (int(row.get("update_id", -1)) for row in updates if isinstance(row, dict)),
            default=-1,
        )
        if latest >= 0:
            self.offset_store.commit(latest)
        self._log(
            "CONNECT",
            stage="CLEAN_START_CURSOR_PRIMED",
            latest_update_id=latest,
            discarded_update_count=len(updates),
        )
        return latest

    def _send_control_response(
        self,
        response,
        *,
        chat_id: int,
        reply_id: int,
        update_id: int,
    ) -> None:
        if isinstance(response, dict):
            photo = str(response.get("photo_path", "") or "")
            text = str(response.get("message", "") or "")
            if photo:
                try:
                    self.client.send_photo(
                        chat_id, photo, caption=text,
                        reply_to_message_id=reply_id,
                    )
                except Exception as exc:
                    self._log(
                        "ERROR", stage="CONTROL_SNAPSHOT_SENDPHOTO_FAILED",
                        update_id=update_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.client.send_document(
                        chat_id, photo, caption=text,
                        reply_to_message_id=reply_id,
                    )
            elif text:
                from RemoteAgent.telegram_delivery import split_telegram_text
                for chunk in split_telegram_text(text):
                    self.client.send_message(
                        chat_id, chunk, reply_to_message_id=reply_id,
                    )
        elif response:
            from RemoteAgent.telegram_delivery import split_telegram_text
            for chunk in split_telegram_text(str(response)):
                self.client.send_message(
                    chat_id, chunk, reply_to_message_id=reply_id,
                )

    def _dispatch_control_async(
        self,
        message: NormalizedInboundMessage,
        *,
        control_key: str,
        control_stage: str,
        update_id: int,
    ) -> None:
        """Run priority software controls without blocking Telegram polling."""
        chat_id = int(message.reply_context["chat_id"])
        reply_id = int(message.reply_context["message_id"])

        def run_control() -> None:
            try:
                response = self.control_handler(message)
                self._send_control_response(
                    response, chat_id=chat_id, reply_id=reply_id,
                    update_id=update_id,
                )
                self._log("CONNECT", stage=control_stage, update_id=update_id)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._log(
                    "ERROR", stage="CONTROL_ASYNC", update_id=update_id,
                    control=control_key, error=error,
                )
                try:
                    self.client.send_message(
                        chat_id,
                        f"RemoteAgent control failed: {error[:500]}",
                        reply_to_message_id=reply_id,
                    )
                except Exception as send_exc:
                    self._log(
                        "ERROR", stage="CONTROL_ASYNC_REPLY", update_id=update_id,
                        error=f"{type(send_exc).__name__}: {send_exc}",
                    )
            finally:
                current = threading.current_thread()
                with self._control_threads_lock:
                    self._control_threads.discard(current)

        worker = threading.Thread(
            target=run_control,
            name=f"telegram-control-{update_id}",
            daemon=True,
        )
        with self._control_threads_lock:
            self._control_threads.add(worker)
        worker.start()

    def _dispatch_local_response_async(
        self,
        message: NormalizedInboundMessage,
        *,
        text: str,
        stage: str,
        update_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chat_id = int(message.reply_context["chat_id"])
        reply_id = int(message.reply_context["message_id"])

        def run_response() -> None:
            try:
                self._send_message(
                    chat_id,
                    str(text),
                    reply_to_message_id=reply_id,
                    reply_markup=reply_markup,
                )
                self._log("CONNECT", stage=stage, update_id=update_id)
            except Exception as exc:
                self._log(
                    "ERROR", stage="LOCAL_RESPONSE_ASYNC", update_id=update_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                current = threading.current_thread()
                with self._control_threads_lock:
                    self._control_threads.discard(current)

        worker = threading.Thread(
            target=run_response,
            name=f"telegram-local-response-{update_id}",
            daemon=True,
        )
        with self._control_threads_lock:
            self._control_threads.add(worker)
        worker.start()

    def announce_remote_feature_keyboard(self) -> None:
        """Install the persistent bottom keyboard without blocking polling."""
        keyboard = remote_feature_reply_keyboard()

        def announce() -> None:
            try:
                for _workspace, conversation in self.allowed_bindings():
                    chat_id = int(conversation.rsplit("/", 1)[-1])
                    self._send_message(
                        chat_id,
                        "RemoteAgent 遠端控制已就緒。",
                        reply_markup=keyboard,
                    )
                self._log("CONNECT", stage="REMOTE_FEATURE_KEYBOARD_READY")
            except Exception as exc:
                self._log(
                    "ERROR",
                    stage="REMOTE_FEATURE_KEYBOARD",
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                current = threading.current_thread()
                with self._control_threads_lock:
                    self._control_threads.discard(current)

        worker = threading.Thread(
            target=announce,
            name="telegram-remote-feature-keyboard",
            daemon=True,
        )
        with self._control_threads_lock:
            self._control_threads.add(worker)
        worker.start()

    def _callback_control(
        self, update: dict[str, Any]
    ) -> tuple[NormalizedInboundMessage | None, str, str]:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return None, "", ""
        callback_id = str(callback.get("id", "") or "")
        command = command_for_remote_callback(str(callback.get("data", "") or ""))
        source = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        chat = source.get("chat") if isinstance(source.get("chat"), dict) else {}
        user_id = int(sender.get("id", 0) or 0)
        chat_id = int(chat.get("id", 0) or 0)
        if (
            not callback_id
            or not command
            or sender.get("is_bot")
            or str(chat.get("type", "")) != "private"
            or not self.adapter.is_authorized(user_id=user_id, chat_id=chat_id)
        ):
            return None, callback_id, "callback_rejected"
        message_id = int(source.get("message_id", 0) or 0)
        update_id = int(update.get("update_id", -1))
        return NormalizedInboundMessage(
            transport="TELEGRAM",
            endpoint="api.telegram.org",
            conversation_key=f"telegram://chat/{chat_id}",
            sender_id=str(user_id),
            source_message_id=f"callback:{callback_id}",
            text=command,
            received_at=float(time.time()),
            idempotency_key=f"TELEGRAM|api.telegram.org|{chat_id}|{update_id}",
            reply_context={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_to_message_id": 0,
            },
            metadata={"update_id": update_id, "callback_query_id": callback_id},
        ), callback_id, ""

    def wait_for_controls(self, timeout_sec: float = 1.0) -> bool:
        """Wait briefly for in-flight controls; primarily for orderly shutdown/tests."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            with self._control_threads_lock:
                workers = list(self._control_threads)
            if not workers:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            workers[0].join(timeout=min(remaining, 0.1))

    def _stage_attachments(self,message:NormalizedInboundMessage,*,update_id:int)->NormalizedInboundMessage:
        if not message.attachments: return message
        root=Path(self.config.workspace).resolve(); chat_id=int(message.reply_context.get("chat_id",0) or 0)
        stage=(workspace_telegram_inbox_root(root)/str(chat_id)/str(update_id)).resolve(); stage.relative_to(root); stage.mkdir(parents=True,exist_ok=True)
        max_bytes=max(1,int(os.environ.get("SMARTAGENT_TELEGRAM_MAX_ATTACHMENT_BYTES",str(50*1024*1024))))
        out=[]; seen_unique=set(); seen_hash=set()
        try:
            for i,item in enumerate(message.attachments,1):
                self._log("CONNECT",stage="ATTACHMENT_RECEIVED",update_id=update_id,kind=item.kind,size=item.size,file_unique_id=item.file_unique_id)
                if item.size and int(item.size)>max_bytes: raise RuntimeError(f"telegram_attachment_too_large:{item.size}>{max_bytes}")
                unique=str(item.file_unique_id or "")
                if unique and unique in seen_unique: continue
                if unique: seen_unique.add(unique)
                fallback=f"photo_{i}.jpg" if item.kind=="photo" else f"document_{i}.bin"
                name=Path(str(item.file_name or fallback)).name; name=re.sub(r"[^0-9A-Za-z._() -]+","_",name).strip(" .") or fallback
                target=(stage/f"{i:02d}_{name}").resolve(); target.relative_to(stage); self.client.download_file(item.transport_id,target)
                if not target.is_file(): raise RuntimeError("telegram_attachment_download_missing")
                actual=int(target.stat().st_size)
                if actual<=0: raise RuntimeError("telegram_attachment_empty")
                if actual>max_bytes: raise RuntimeError(f"telegram_attachment_too_large:{actual}>{max_bytes}")
                digest=hashlib.sha256(target.read_bytes()).hexdigest()
                if digest in seen_hash: target.unlink(missing_ok=True); continue
                seen_hash.add(digest)
                out.append(InboundAttachment(kind=item.kind,transport_id=item.transport_id,file_name=item.file_name or name,mime_type=item.mime_type,size=actual,local_path=str(target),file_unique_id=unique,sha256=digest))
                self._log("CONNECT",stage="ATTACHMENT_STAGED",update_id=update_id,path=str(target),size=actual,sha256=digest)
            return replace(message,attachments=tuple(out))
        except Exception as exc:
            self._log("ERROR",stage="ATTACHMENT_STAGE_FAILED",update_id=update_id,error=f"{type(exc).__name__}: {exc}")
            shutil.rmtree(stage,ignore_errors=True)
            raise

    def allowed_bindings(self) -> list[tuple[str, str]]:
        workspace = str(Path(self.config.workspace).resolve())
        chat_ids = set(self.config.allowed_chat_ids)
        if self.pairing_store is not None:
            chat_ids.update(self.pairing_store.paired_chat_ids())
        return [(workspace, f"telegram://chat/{chat_id}") for chat_id in sorted(chat_ids)]

    def _pairing_start(self, update: dict[str, Any]) -> tuple[bool, str]:
        """Consume `/start pair_<secret>` before normal task/session ingress."""
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        identity = TelegramUpdateAdapter._private_non_bot(message)
        text = str(message.get("text") or "").strip()
        if text != "/start" and not text.startswith("/start "):
            return False, ""
        # `/start` is a transport control event in every configuration.  It
        # must never create a task/session, including static-allowlist mode.
        if identity is None:
            return True, ""
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) == 2 else ""
        if self.pairing_store is None:
            return True, "已連線。請直接傳送自然語言工作指令。" if self.adapter.is_authorized(user_id=identity[0], chat_id=identity[1]) else "此 Bot 尚未授權此對話。"
        if not payload.startswith("pair_"):
            return True, "已連線。請直接傳送自然語言工作指令。" if self.adapter.is_authorized(user_id=identity[0], chat_id=identity[1]) else "此 Bot 需要使用桌面端顯示的 QR Code 配對。"
        secret = payload[5:]
        paired = self.pairing_store.consume(secret, user_id=identity[0], chat_id=identity[1])
        return True, "配對完成。現在可直接傳送自然語言工作指令。" if paired else "配對連結無效或已過期；請在電腦端重新產生 QR Code。"

    def poll_once(self) -> list[Any]:
        updates = self.client.get_updates(
            offset=self.offset_store.next_offset,
            timeout=self.config.poll_timeout_sec,
        )
        self._log("POLL", offset=self.offset_store.next_offset, update_count=len(updates))
        with self.binding_lock:
            return self._process_updates(updates)

    def _process_updates(self, updates: list[dict[str, Any]]) -> list[Any]:
        accepted = []
        for update in sorted(updates, key=lambda row: int(row.get("update_id", -1))):
            update_id = int(update.get("update_id", -1))
            callback_message, callback_id, callback_reason = self._callback_control(update)
            if callback_id:
                self.offset_store.commit(update_id)
                if callback_message is None:
                    self._log(
                        "ERROR", stage="CONTROL_CALLBACK_REJECTED",
                        update_id=update_id, error=callback_reason,
                    )
                    continue
                try:
                    self.client.answer_callback_query(callback_id, text="已收到")
                except Exception as exc:
                    self._log(
                        "ERROR", stage="CONTROL_CALLBACK_ACK",
                        update_id=update_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if self._process_pre_resume_choice(callback_message, update_id=update_id):
                    continue
                control_key = canonical_remote_control(callback_message.text)
                control_stages = {
                    MANAGER_COMMAND.casefold(): "CONTROL_BINDING_MANAGER",
                    LIST_SKILLS_COMMAND.casefold(): "CONTROL_LIST_SKILLS",
                    "snapshot webgpt": "CONTROL_SNAPSHOT_WEBGPT",
                    "refresh": "CONTROL_REFRESH_WEBGPT",
                    "重新整理": "CONTROL_REFRESH_WEBGPT",
                    STATUS_COMMAND.casefold(): "CONTROL_STATUS_WEBGPT",
                    INTERRUPT_COMMAND.casefold(): "CONTROL_INTERRUPT_TASK",
                    RECONNECT_COMMAND.casefold(): "CONTROL_RESTART_REMOTE",
                    CMD_CONTROL_LABEL.casefold(): "CONTROL_DIRECT_CMD_TEMPLATE",
                }
                security_decision = (
                    control_key.startswith("確認刪除 ")
                    or control_key.startswith("拒絕刪除 ")
                    or control_key.startswith("security_approve_once ")
                    or control_key.startswith("security_reject_once ")
                    or control_key.startswith("security_workspace_delete ")
                )
                progress_query = control_key.startswith("查看任務進度 ")
                if (control_key in control_stages or security_decision or progress_query) and self.control_handler is not None:
                    self._dispatch_control_async(
                        callback_message,
                        control_key=control_key,
                        control_stage=(
                            "CONTROL_TASK_PROGRESS"
                            if progress_query
                            else control_stages.get(control_key, "CONTROL_SECURITY_APPROVAL")
                        ),
                        update_id=update_id,
                    )
                continue
            handled_start, start_response = self._pairing_start(update)
            if handled_start:
                message = update.get("message") if isinstance(update.get("message"), dict) else {}
                chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                message_id = int(message.get("message_id", 0) or 0)
                chat_id = int(chat.get("id", 0) or 0)
                self.offset_store.commit(update_id)
                if chat_id and start_response:
                    self._send_message(
                        chat_id,
                        start_response,
                        reply_to_message_id=message_id or None,
                        reply_markup=remote_feature_reply_keyboard(),
                    )
                self._log("CONNECT", stage="PAIRING" if "配對完成" in start_response else "START_IGNORED", update_id=update_id)
                continue
            message, reason = self.adapter.normalize(update)
            if message is None:
                if reason not in {"ignored_update", "empty_message"}:
                    self._log("ERROR", stage="AUTHORIZATION", update_id=update_id, error=reason)
                self.offset_store.commit(update_id)
                continue
            control_key=canonical_remote_control(message.text)
            if self._process_pre_resume_choice(message, update_id=update_id):
                self.offset_store.commit(update_id)
                continue
            if control_key == FEATURE_QUERY_COMMAND.casefold():
                self.offset_store.commit(update_id)
                self._dispatch_local_response_async(
                    message,
                    text=render_remote_feature_list(),
                    stage="CONTROL_REMOTE_FEATURE_QUERY",
                    update_id=update_id,
                    reply_markup=remote_feature_inline_keyboard(),
                )
                continue
            control_stages={MANAGER_COMMAND.casefold():"CONTROL_BINDING_MANAGER",LIST_SKILLS_COMMAND.casefold():"CONTROL_LIST_SKILLS","關閉任務":"CONTROL_CLOSE_TASK","snapshot webgpt":"CONTROL_SNAPSHOT_WEBGPT","refresh":"CONTROL_REFRESH_WEBGPT","重新整理":"CONTROL_REFRESH_WEBGPT",STATUS_COMMAND.casefold():"CONTROL_STATUS_WEBGPT",INTERRUPT_COMMAND.casefold():"CONTROL_INTERRUPT_TASK",RECONNECT_COMMAND.casefold():"CONTROL_RESTART_REMOTE",CMD_CONTROL_LABEL.casefold():"CONTROL_DIRECT_CMD_TEMPLATE"}
            binding_update = RemoteBindingManager.is_update(message.text)
            security_decision = (
                control_key.startswith("確認刪除 ")
                or control_key.startswith("拒絕刪除 ")
                or control_key.startswith("security_approve_once ")
                or control_key.startswith("security_reject_once ")
                or control_key.startswith("security_workspace_delete ")
            )
            if (control_key in control_stages or binding_update or security_decision) and self.control_handler is not None:
                self.offset_store.commit(update_id)
                if control_key == "關閉任務":
                    response = self.control_handler(message)
                    self._send_control_response(
                        response,
                        chat_id=int(message.reply_context["chat_id"]),
                        reply_id=int(message.reply_context["message_id"]),
                        update_id=update_id,
                    )
                    self._log("CONNECT",stage=control_stages.get(control_key,"CONTROL_SECURITY_APPROVAL"),update_id=update_id)
                else:
                    self._dispatch_control_async(
                        message,
                        control_key=control_key,
                        control_stage=(
                            "CONTROL_BINDING_UPDATE"
                            if binding_update else control_stages.get(control_key, "CONTROL_SECURITY_APPROVAL")
                        ),
                        update_id=update_id,
                    )
                continue
            # Explicit Turbo configuration is a software-owned human control.
            # Process in the authenticated Telegram ingress before any Planner,
            # WebGPT DOM submission or Agent task creation.
            from agent_core.turbo_settings_command import (
                parse_turbo_control, apply_turbo_control, turbo_control_reply,
            )
            if parse_turbo_control(message.text) is not None:
                self.offset_store.commit(update_id)
                try:
                    if message.attachments:
                        raise ValueError("turbo_control_attachments_not_allowed")
                    outcome = apply_turbo_control(
                        message.text, workspace=self.config.workspace,
                    )
                    reply = turbo_control_reply(outcome)
                    self._log("CONNECT", stage="CONTROL_TURBO_MODE",
                              update_id=update_id,
                              revision=outcome["revision"])
                except (ValueError, OSError, RuntimeError) as exc:
                    reply = "🔴 Turbo Mode 設定拒絕：" + str(exc)[:400]
                    self._log("ERROR", stage="CONTROL_TURBO_MODE_REJECTED",
                              update_id=update_id, error=str(exc)[:300])
                self._send_message(
                    int(message.reply_context["chat_id"]), reply,
                    reply_to_message_id=int(message.reply_context["message_id"]),
                )
                continue
            if is_direct_cmd_request(message.text):
                # Authenticated Telegram software control, same as other
                # buttons and direct controls. The general Agent-task preflight
                # is not a prerequisite. This authenticated human-control
                # channel launches a host CMD under the Telegram Listener's
                # Windows token regardless of ACL/Turbo settings. It can
                # modify outside Turbo Root: never expose it to Planner tools.
                # Never enqueue an Agent task or send content to WebGPT.
                self.offset_store.commit(update_id)
                if self.control_handler is None or message.attachments:
                    self._send_message(
                        int(message.reply_context["chat_id"]),
                        "🔴 執行cmd不可用或不能附帶檔案。",
                        reply_to_message_id=int(message.reply_context["message_id"]),
                    )
                else:
                    self._dispatch_control_async(
                        message, control_key="direct_cmd",
                        control_stage="CONTROL_DIRECT_CMD_EXECUTE",
                        update_id=update_id,
                    )
                continue
            if self.task_admission_guard is not None:
                try:
                    admission = self.task_admission_guard(message)
                    allowed = bool(admission[0]) if isinstance(admission, tuple) else bool(admission)
                    reason = str(admission[1] or "") if isinstance(admission, tuple) and len(admission) > 1 else ""
                except Exception as exc:
                    allowed = False
                    reason = f"security_admission_error:{type(exc).__name__}: {exc}"
                if not allowed:
                    self.offset_store.commit(update_id)
                    self._send_message(
                        int(message.reply_context["chat_id"]),
                        "🔴 安全預檢未通過，任務未排入執行佇列。\n"
                        + (reason[:1000] or "restricted executor unavailable"),
                        reply_to_message_id=int(message.reply_context["message_id"]),
                    )
                    self._log(
                        "ERROR", stage="SECURITY_TASK_ADMISSION_REJECTED",
                        update_id=update_id, error=reason,
                    )
                    continue
            # Pre-resume confirmation is software-owned and takes precedence
            # over Agent ingestion. Neither transport nor model may enqueue
            # the resumed task before the human decision is recorded.
            if self._offer_pre_resume_decision(message, update_id=update_id):
                self.offset_store.commit(update_id)
                continue
            if RemoteSkillManager.looks_like_invocation(message.text):
                try:
                    from agent_core.remote_binding import active, load
                    binding = active() or load()
                    skill_manager = RemoteSkillManager(binding["skill_path"])
                    selected_skill = skill_manager.requested_skill(message.text)
                    message = replace(
                        message,
                        metadata={
                            **dict(message.metadata or {}),
                            "selected_skill": selected_skill,
                        },
                    )
                    self._log(
                        "CONNECT", stage="SKILL_SELECTED", update_id=update_id,
                        skill=selected_skill,
                    )
                except (RemoteSkillError, ValueError, OSError) as exc:
                    self.offset_store.commit(update_id)
                    self.client.send_message(
                        int(message.reply_context["chat_id"]),
                        "🔴 找不到或無法載入指定 skill。\n"
                        f"{str(exc)[:500]}\n請先使用「列出skill」確認名稱。",
                        reply_to_message_id=int(message.reply_context["message_id"]),
                    )
                    self._log(
                        "ERROR", stage="SKILL_SELECTION_REJECTED",
                        update_id=update_id, error=str(exc),
                    )
                    continue
            if message.attachments:
                try: message=self._stage_attachments(message,update_id=update_id)
                except Exception as exc:
                    error=f"{type(exc).__name__}: {exc}"
                    self._log("ERROR",stage="ATTACHMENT_DOWNLOAD",update_id=update_id,error=error)
                    self.offset_store.commit(update_id)
                    self.client.send_message(
                        int(message.reply_context["chat_id"]),
                        f"🔴 執行失敗\n附件下載失敗，請重新傳送。\n{error[:500]}",
                        reply_to_message_id=int(message.reply_context["message_id"]),
                    )
                    continue
            self._log(
                "REQUEST_DETECTED",
                update_id=update_id,
                conversation_key=message.conversation_key,
                source_message_id=message.source_message_id,
            )
            try:
                result = self.ingress.accept(message, workspace=self.config.workspace)
            except ValueError as exc:
                # Permanent request-shape/workspace rejection is NOT a failed
                # Telegram POLL. Record a durable denial before advancing the
                # offset so one poison message cannot starve every later chat.
                # Storage/queue/OS errors are never swallowed and remain retryable.
                reason = str(exc)
                permanent = reason.startswith((
                    "request_explicit_workspace_",
                    "request_path_outside_authorized_roots:",
                    "request_working_directory_unavailable:",
                    "workspace_writable_roots_not_configured",
                    "continuation_workspace_reference_not_found_or_ambiguous",
                    "continuation_workspace_no_longer_writable",
                    "resolved_workspace_no_longer_writable",
                    "empty_transport_message",
                ))
                if not permanent:
                    raise
                self._log(
                    "ERROR", stage="REQUEST_ADMISSION_REJECTED",
                    update_id=update_id,
                    rejection_reason=reason[:240],
                    request_id=self.ingress._request_id(message)
                    if hasattr(self.ingress, "_request_id") else "",
                    permanent=True, executed=False,
                )
                # Commit is durable and may raise: NEVER drop the message when
                # the offset store itself is temporarily unavailable.
                self.offset_store.commit(update_id)
                try:
                    self._send_message(
                        int(message.reply_context["chat_id"]),
                        "🔴 請求未建立：工作目錄選擇無效或不在 Edit Workspace 授權範圍。"
                        "\n請確認明確的 Workspace 設定；文字提及路徑本身不會被擋。",
                        reply_to_message_id=int(message.reply_context["message_id"]),
                    )
                except Exception as send_exc:
                    self._log(
                        "ERROR", stage="REQUEST_REJECTION_REPLY_FAILED",
                        update_id=update_id,
                        error=f"{type(send_exc).__name__}: {send_exc}"[:240],
                    )
                continue
            self.offset_store.commit(update_id)
            if result.response:
                self.client.send_message(
                    int(message.reply_context["chat_id"]),
                    result.response,
                    reply_to_message_id=int(message.reply_context["message_id"]),
                )
            if result.task is not None:
                accepted.append(result.task)
                if bool(getattr(result, "created", True)) and self.accepted_handler is not None:
                    try:
                        self.accepted_handler(result.task)
                    except Exception as exc:
                        self._log(
                            "ERROR",
                            stage="ACCEPTED_DELIVERY",
                            update_id=update_id,
                            task_id=str(getattr(result.task, "task_id", "") or ""),
                            request_id=str(getattr(result.task, "request_id", "") or ""),
                            error=f"{type(exc).__name__}: {exc}",
                        )
        return accepted

    def run(self) -> None:
        self._log("CONNECT", stage="STARTED", allowed_chat_count=len(self.config.allowed_chat_ids))
        while not self._stop.is_set():
            try:
                self.last_poll_started_at = time.time()
                self.poll_once()
                self.last_poll_success_at = time.time()
                self.last_poll_error = ""
            except Exception as exc:
                self.last_poll_error_at = time.time()
                self.last_poll_error = f"{type(exc).__name__}: {exc}"
                self._log("ERROR", stage="POLL", error=f"{type(exc).__name__}: {exc}")
                self._log("RECONNECT", stage="BACKOFF", delay_sec=self.config.retry_delay_sec)
                self._stop.wait(self.config.retry_delay_sec)
        self._log("CONNECT", stage="STOPPED")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="telegram-agent0-receiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.wait_for_controls(timeout_sec=1.0)
