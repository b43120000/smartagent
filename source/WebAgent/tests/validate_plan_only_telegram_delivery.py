#!/usr/bin/env python3
"""Regression checks for PLAN_ONLY completion and plan_id delivery."""
from __future__ import annotations

import tempfile
from pathlib import Path

from RemoteAgent.telegram_artifacts import prepare_telegram_completion
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter


PLAN_REQUEST = """規劃用 Python 生成灰階 PNG，最後上傳到 Telegram。
路徑：E:\\card
只規劃不實做"""
EXECUTE_REQUEST = """用 Python 生成灰階 PNG，最後上傳到 Telegram。
路徑：E:\\workspace"""


class FakeTelegramClient:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(str(text))
        return {"message_id": len(self.messages)}

    def send_photo(self, chat_id, path, **kwargs):
        return {"message_id": 100}

    def send_document(self, chat_id, path, **kwargs):
        return {"message_id": 101}


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="smartagent-plan-only-telegram-") as value:
        workspace = Path(value).resolve()
        summary = "規劃完成，本輪沒有實作。"

        plan_only = prepare_telegram_completion(
            request=PLAN_REQUEST,
            summary=summary,
            workspace=workspace,
            artifacts=[],
        )
        assert plan_only["summary"] == summary, plan_only

        execution = prepare_telegram_completion(
            request=EXECUTE_REQUEST,
            summary="執行完成。",
            workspace=workspace,
            artifacts=[],
        )
        assert "未產生 Telegram outbound artifact" in execution["summary"], execution

        published_plan = prepare_telegram_completion(
            request=EXECUTE_REQUEST,
            summary=summary,
            workspace=workspace,
            artifacts=[],
            expect_artifact=False,
        )
        assert published_plan["summary"] == summary, published_plan

        image = workspace / "gray.png"
        image.write_bytes(b"not-a-real-png-but-a-real-artifact")
        plan_with_artifact = prepare_telegram_completion(
            request=PLAN_REQUEST,
            summary=summary,
            workspace=workspace,
            artifacts=[{"path": str(image), "kind": "photo"}],
            expect_artifact=False,
        )
        assert len(plan_with_artifact["artifacts"]) == 1, plan_with_artifact

        client = FakeTelegramClient()
        adapter = TelegramDeliveryAdapter(client)
        event = {
            "event_id": "EVT-PLAN-ONLY",
            "event_type": "TASK_COMPLETED",
            "request_id": "REQ-PLAN-ONLY",
            "task_id": "TASK-PLAN-ONLY",
            "status": "COMPLETED",
            "payload": {
                "summary": summary,
                "workspace": str(workspace),
                "artifacts": [],
                "artifact_rejections": [],
                "plan_id": "PLAN-1234567890",
            },
        }
        delivered = adapter.deliver_event(
            {"transport": "TELEGRAM", "chat_id": 1, "message_id": 2},
            event,
        )
        assert delivered["delivered"] is True, delivered
        assert any(summary in message for message in client.messages), client.messages
        assert any("PLAN-1234567890" in message for message in client.messages), client.messages

    result = {
        "plan_only_preserves_summary": True,
        "execute_missing_artifact_still_reported": True,
        "published_plan_suppresses_missing_artifact": True,
        "real_plan_artifact_still_delivered": True,
        "plan_id_survives_delivery_revalidation": True,
    }
    print("PLAN_ONLY_TELEGRAM_DELIVERY_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
