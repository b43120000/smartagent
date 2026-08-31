#!/usr/bin/env python3
from __future__ import annotations

"""Telegram Bot API transport. Disabled until explicit local configuration."""

import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from agent_core.transport_message import InboundAttachment, NormalizedInboundMessage
from RemoteAgent.telegram_pairing import TelegramPairingStore


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
        with self._opener(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError(f"telegram_api_error:{str(body.get('description', 'unknown'))[:300] if isinstance(body, dict) else 'invalid_response'}")
        return body.get("result")

    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {"offset": int(offset), "timeout": int(timeout), "allowed_updates": ["message"]},
            timeout=float(timeout) + 10.0,
        )
        return list(result or [])

    def get_me(self) -> dict[str, Any]:
        return dict(self._call("getMe", {}, timeout=20.0) or {})

    def send_message(self, chat_id: int, text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
        result = self._call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "reply_parameters": {"message_id": int(reply_to_message_id)} if reply_to_message_id else None,
            },
            timeout=20.0,
        )
        return dict(result or {})

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
        with self._opener(req,timeout=60.0) as response: body=json.loads(response.read().decode())
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
            self.next_offset = max(self.next_offset, int(update_id) + 1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            temp.write_text(
                json.dumps({"version": 1, "next_offset": self.next_offset}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self.path)
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
            ))
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = photos[-1] if isinstance(photos[-1], dict) else {}
            rows.append(InboundAttachment(
                kind="photo",
                transport_id=str(photo.get("file_id", "")),
                size=int(photo.get("file_size", 0) or 0),
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
        text = str(message.get("text") or message.get("caption") or "").strip()
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
    ):
        config.validate()
        self.config = config
        self.client = client
        self.offset_store = offset_store
        self.ingress = ingress
        self.runtime_log = runtime_log
        self.pairing_store = pairing_store
        self.control_handler = control_handler
        self.adapter = TelegramUpdateAdapter(config, pairing_store=pairing_store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _log(self, event: str, **detail) -> None:
        if self.runtime_log is not None:
            self.runtime_log.write(event, component="telegram_receiver", **detail)

    def _stage_attachments(self,message:NormalizedInboundMessage,*,update_id:int)->NormalizedInboundMessage:
        if not message.attachments: return message
        root=Path(self.config.workspace).resolve(); chat_id=int(message.reply_context.get("chat_id",0) or 0); stage=(root/".agents"/"telegram_inbox"/str(chat_id)/str(update_id)).resolve(); stage.relative_to(root); stage.mkdir(parents=True,exist_ok=True); out=[]
        for i,item in enumerate(message.attachments,1):
            fallback=f"photo_{i}.jpg" if item.kind=="photo" else f"document_{i}.bin"; name=Path(str(item.file_name or fallback)).name; name=re.sub(r"[^0-9A-Za-z._() -]+","_",name).strip(" .") or fallback; target=(stage/f"{i:02d}_{name}").resolve(); target.relative_to(stage); self.client.download_file(item.transport_id,target)
            if not target.is_file(): raise RuntimeError("telegram_attachment_download_missing")
            out.append(InboundAttachment(item.kind,item.transport_id,item.file_name or name,item.mime_type,item.size,str(target)))
        return replace(message,attachments=tuple(out))

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
        accepted = []
        for update in sorted(updates, key=lambda row: int(row.get("update_id", -1))):
            update_id = int(update.get("update_id", -1))
            handled_start, start_response = self._pairing_start(update)
            if handled_start:
                message = update.get("message") if isinstance(update.get("message"), dict) else {}
                chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                message_id = int(message.get("message_id", 0) or 0)
                chat_id = int(chat.get("id", 0) or 0)
                self.offset_store.commit(update_id)
                if chat_id and start_response:
                    self.client.send_message(chat_id, start_response, reply_to_message_id=message_id or None)
                self._log("CONNECT", stage="PAIRING" if "配對完成" in start_response else "START_IGNORED", update_id=update_id)
                continue
            message, reason = self.adapter.normalize(update)
            if message is None:
                if reason not in {"ignored_update", "empty_message"}:
                    self._log("ERROR", stage="AUTHORIZATION", update_id=update_id, error=reason)
                self.offset_store.commit(update_id)
                continue
            if message.text == "關閉任務" and self.control_handler is not None:
                response=str(self.control_handler(message) or "")
                self.offset_store.commit(update_id)
                if response:
                    self.client.send_message(int(message.reply_context["chat_id"]),response,reply_to_message_id=int(message.reply_context["message_id"]))
                self._log("CONNECT",stage="CONTROL_CLOSE_TASK",update_id=update_id)
                continue
            if message.attachments:
                try: message=self._stage_attachments(message,update_id=update_id)
                except Exception as exc:
                    self._log("ERROR",stage="ATTACHMENT_DOWNLOAD",update_id=update_id,error=f"{type(exc).__name__}: {exc}"); self.offset_store.commit(update_id); continue
            self._log(
                "REQUEST_DETECTED",
                update_id=update_id,
                conversation_key=message.conversation_key,
                source_message_id=message.source_message_id,
            )
            result = self.ingress.accept(message, workspace=self.config.workspace)
            self.offset_store.commit(update_id)
            if result.response:
                self.client.send_message(
                    int(message.reply_context["chat_id"]),
                    result.response,
                    reply_to_message_id=int(message.reply_context["message_id"]),
                )
            if result.task is not None:
                accepted.append(result.task)
        return accepted

    def run(self) -> None:
        self._log("CONNECT", stage="STARTED", allowed_chat_count=len(self.config.allowed_chat_ids))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
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
