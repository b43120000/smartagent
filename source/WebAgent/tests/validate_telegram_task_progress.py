#!/usr/bin/env python3
"""Offline validation for Telegram task-scoped Progress updates."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from RemoteAgent.remote_feature_catalog import (
    command_for_remote_callback,
    task_progress_inline_keyboard,
)
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from RemoteAgent.telegram_transport import (
    TelegramOffsetStore,
    TelegramReceiver,
    TelegramReceiverConfig,
)
from RemoteAgent.telegram_webagent_worker import _event_sink
from agent_core.remote_control_dispatcher import RemoteControlDispatcher
from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.paths import remote_tasks_path
from agent_core.task_progress import initialize_progress, record_model_progress
from agent_core.task_state import RemoteTaskQueue, TaskStateStore


class FakeLog:
    def __init__(self) -> None:
        self.rows = []

    def write(self, event: str, **fields) -> None:
        self.rows.append((event, fields))


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"message_id": len(self.messages)}

    def answer_callback_query(self, callback_query_id, *, text=""):
        self.callback_answer = (callback_query_id, text)
        return True


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="telegram-progress-") as temp:
        root = Path(temp)
        queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
        task, created = queue.enqueue_remote_request(
            {
                "request_id": "RR-TELEGRAM-PROGRESS",
                "request": "修改 source code",
                "workspace": str(root),
                "conversation_url": "telegram://chat/7",
                "transport": "TELEGRAM",
            },
            origin_turn_fingerprint="telegram-progress-turn",
            metadata={
                "transport": "TELEGRAM",
                "reply_route": {
                    "transport": "TELEGRAM",
                    "chat_id": 7,
                    "message_id": 11,
                },
            },
        )
        assert created
        initialize_progress(
            task.task_id,
            request_id=task.request_id,
            goal=task.request,
            root=root,
        )
        record_model_progress(
            task.task_id,
            {
                "base_evaluation": "Base 已載入",
                "total_steps": 3,
                "current_step": 1.5,
                "steps": [
                    {"step": 1, "desc": "盤點", "status": "COMPLETED"},
                    {"step": 2, "desc": "修改", "status": "IN_PROGRESS"},
                    {"step": 3, "desc": "驗證", "status": "PENDING"},
                ],
                "current_focus": "修改 Telegram 進度介面",
                "next_action": "執行回歸測試",
                "completion_contract": {
                    "success": ["修改完成且回歸測試通過"],
                    "failure": ["修改已結束但回歸測試失敗"],
                    "in_progress": ["修改或回歸測試仍未完成"],
                    "interrupted": ["Runtime 無法繼續修改或測試"],
                },
                "decision": "CONTINUE",
                "outcome": "PENDING",
                "matched_condition": "修改或回歸測試仍未完成",
                "evidence_refs": ["RUNTIME_STATUS"],
                "decision_reason": "目前仍在修改階段",
            },
            request_id=task.request_id,
            goal=task.request,
            round_id=1,
            root=root,
        )

        # The first accepted model progress creates exactly one durable update.
        events = RemoteEventStore(root / "localdata" / "runtime" / "events.json")
        log = FakeLog()
        sink = _event_sink(log, task, root=root, events=events)
        sink("task_progress_updated", current_step=1.5, total_steps=3)
        sink("task_progress_updated", current_step=2, total_steps=3)
        events.load()
        progress_events = [
            event for event in events.events.values()
            if event.event_type == "TASK_PROGRESS"
        ]
        assert len(progress_events) == 1
        assert "1.5 / 3" in progress_events[0].payload["summary"]
        broken_events = SimpleNamespace(
            emit=lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("outbox unavailable")
            )
        )
        _event_sink(log, task, root=root, events=broken_events)(
            "task_progress_updated", current_step=2, total_steps=3
        )
        assert any(
            fields.get("stage") == "TASK_PROGRESS_QUEUE_FAILED"
            for _event, fields in log.rows
        )

        # Both the running bubble and first Progress bubble expose a scoped button.
        client = FakeTelegramClient()
        delivery = TelegramDeliveryAdapter(client)
        route = {"transport": "TELEGRAM", "chat_id": 7, "message_id": 11}
        started_result = delivery.deliver_event(route, {
            "event_id": "EVT-TASK_STARTED",
            "event_type": "TASK_STARTED",
            "request_id": task.request_id,
            "task_id": task.task_id,
            "status": "RUNNING",
            "payload": {},
        })
        assert started_result["delivered"] is True
        progress_result = DeliveryManager(
            events, {"TELEGRAM": delivery}
        ).deliver(progress_events[0])
        assert progress_result["delivered"] is True
        expected_markup = task_progress_inline_keyboard(task.task_id)
        assert client.messages[0]["reply_markup"] == expected_markup
        assert client.messages[1]["reply_markup"] == expected_markup
        callback_data = expected_markup["inline_keyboard"][0][0]["callback_data"]
        assert command_for_remote_callback(callback_data) == (
            "查看任務進度 " + task.task_id
        )

        # A button query reads only the task-scoped local ledger.
        dispatcher = RemoteControlDispatcher.__new__(RemoteControlDispatcher)
        dispatcher.root = root
        allowed = dispatcher._run_task_progress_control(
            SimpleNamespace(reply_context={"chat_id": 7}), task.task_id
        )
        assert allowed["status_source"] == "task_progress_ledger"
        assert "1.5 / 3" in allowed["message"]
        denied = dispatcher._run_task_progress_control(
            SimpleNamespace(reply_context={"chat_id": 8}), task.task_id
        )
        assert "找不到" in denied["message"]

        # The real callback receiver routes the scoped button to the software control.
        receiver = TelegramReceiver(
            config=TelegramReceiverConfig(
                enabled=True,
                bot_token="test-token",
                allowed_user_ids=(7,),
                allowed_chat_ids=(7,),
                workspace=str(root),
            ),
            client=client,
            offset_store=TelegramOffsetStore(root / "telegram_offset.json"),
            ingress=SimpleNamespace(),
            control_handler=dispatcher.handle,
        )
        receiver._process_updates([{
            "update_id": 71,
            "callback_query": {
                "id": "CALLBACK-71",
                "data": callback_data,
                "from": {"id": 7, "is_bot": False},
                "message": {
                    "message_id": 99,
                    "chat": {"id": 7, "type": "private"},
                },
            },
        }])
        assert receiver.wait_for_controls(2.0)
        assert client.callback_answer == ("CALLBACK-71", "已收到")
        assert client.messages[-1]["reply_to_message_id"] == 99
        assert "1.5 / 3" in client.messages[-1]["text"]

        return {
            "checkpoint_event_once": True,
            "notification_failure_nonfatal": True,
            "started_button": True,
            "progress_button": True,
            "task_scoped_callback": True,
            "callback_receiver_routed": True,
            "software_only_ledger_query": True,
            "cross_chat_access_rejected": True,
        }


if __name__ == "__main__":
    import json

    print("TELEGRAM_TASK_PROGRESS_OK")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
