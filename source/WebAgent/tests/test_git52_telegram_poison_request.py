"""Git52 Telegram: one permanent request rejection never starves next update."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from RemoteAgent.telegram_transport import (
    TelegramOffsetStore, TelegramReceiver, TelegramReceiverConfig,
)


class Client:
    def __init__(self):
        self.messages = []
    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"ok": True}


class Ingress:
    def __init__(self, error):
        self.error = error
        self.calls = 0
    def accept(self, message, *, workspace):
        self.calls += 1
        if self.calls == 1 and self.error:
            raise self.error
        return SimpleNamespace(
            task=SimpleNamespace(task_id="TASK-SECOND"),
            created=True,
            response="",
        )


def _update(update_id, text):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1750000000,
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 100, "is_bot": False},
            "text": text,
        },
    }


def _receiver(tmp_path, error):
    config = TelegramReceiverConfig(
        enabled=False, allowed_user_ids=(100,), allowed_chat_ids=(200,),
        workspace=str(tmp_path),
    )
    client = Client()
    store = TelegramOffsetStore(tmp_path / "offset.json")
    log = []
    receiver = TelegramReceiver(
        config=config, client=client, offset_store=store,
        ingress=Ingress(error), install_root=tmp_path,
    )
    receiver._log = lambda event, **detail: log.append((event, detail))
    receiver._callback_control = lambda update: (None, "", "")
    receiver._process_pre_resume_choice = lambda *args, **kw: False
    receiver._offer_pre_resume_decision = lambda *args, **kw: False
    return receiver, client, store, log


def test_invalid_workspace_is_acked_and_following_hello_processed(tmp_path):
    receiver, client, offsets, logs = _receiver(
        tmp_path, ValueError("request_explicit_workspace_not_writable"),
    )
    results = receiver._process_updates([
        _update(858983588, "Workspace: C:\\outside"),
        _update(858983589, "你好"),
    ])
    assert len(results) == 1
    assert results[0].task_id == "TASK-SECOND"
    assert offsets.next_offset == 858983590
    assert TelegramOffsetStore(offsets.path).next_offset == 858983590
    assert client.messages and "請求未建立" in client.messages[0][1]
    assert any(item.get("stage") == "REQUEST_ADMISSION_REJECTED"
               for _, item in logs)


def test_transient_ingress_error_keeps_offset_and_retries(tmp_path):
    receiver, _, offsets, _ = _receiver(
        tmp_path, ValueError("unexpected_queue_write_failure"),
    )
    with pytest.raises(ValueError, match="unexpected_queue_write_failure"):
        receiver._process_updates([_update(1, "你好")])
    assert offsets.next_offset == 0
    assert not offsets.path.exists()


def test_offset_persistence_failure_does_not_advance_in_memory_cursor(
    tmp_path, monkeypatch,
):
    store = TelegramOffsetStore(tmp_path / "offset.json")
    def fail_replace(self, destination):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.commit(10)
    assert store.next_offset == 0
    assert not store.path.exists()
