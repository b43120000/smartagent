"""Integration: Telegram CMD shares the existing private-chat pairing and reply route."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from RemoteAgent.telegram_pairing import TelegramPairingStore
from RemoteAgent.telegram_transport import (
    TelegramReceiver, TelegramReceiverConfig, TelegramOffsetStore,
)
from agent_core.paths import telegram_pairing_path
from agent_core.remote_control_dispatcher import RemoteControlDispatcher


class Log:
    def __init__(self):
        self.rows = []

    def write(self, *args, **kwargs):
        self.rows.append((args, kwargs))


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, str(text), kwargs))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_id, *, text=""):
        return True


class NoTaskIngress:
    def accept(self, *_args, **_kwargs):
        raise AssertionError("CMD must never enter Agent Task Queue")


def packet(user_id=11, chat_id=22, update_id=101):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "date": 1000,
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": chat_id, "type": "private"},
            "text": "run cmd by TG\ncmd path: \"C:\\smartagent\"\ncommand:\n\"echo hello\"",
        },
    }


class TelegramCmdSharedAuthorizationIT(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="telegram-cmd-shared-auth-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.client = FakeClient()
        self.log = Log()
        self.dispatcher = RemoteControlDispatcher(
            root=self.root, python="python", runtime_log=self.log,
            control_plane=SimpleNamespace(), session_state=lambda: {},
            request_restart=lambda _: {}, request_close=lambda _: {},
        )

    def _receiver(self, *, pairing_enabled=True, users=(), chats=()):
        config = TelegramReceiverConfig(
            enabled=True, bot_token="TEST_BOT_TOKEN",
            pairing_enabled=pairing_enabled,
            allowed_user_ids=users,
            allowed_chat_ids=chats,
            workspace=str(self.root),
        )
        pairing = (
            TelegramPairingStore(telegram_pairing_path(self.root))
            if pairing_enabled else None
        )
        receiver = TelegramReceiver(
            config=config,
            client=self.client,
            pairing_store=pairing,
            offset_store=TelegramOffsetStore(self.root / "offset.json"),
            ingress=NoTaskIngress(),
            control_handler=self.dispatcher.handle,
            # Agent-task failure must not block independently authorized SW control.
            task_admission_guard=lambda _: (False, "AGENT_TASK_BLOCKED"),
        )
        return config, pairing, receiver

    def test_already_paired_private_chat_works_without_any_cmd_settings(self):
        config, pairing, receiver = self._receiver()
        secret = pairing.issue()
        self.assertTrue(pairing.consume(secret, user_id=11, chat_id=22))

        # Neither legacy command settings nor environment variables are needed.
        self.assertFalse((self.root / "localdata" / "secure" / "telegram_cmd_settings.json").exists())
        output = {"message": "CMD 執行結果\nExit Code: 0\nhello\n狀態：SUCCESS",
                  "control_id": "TG-CMD-TEST", "status": "SUCCESS",
                  "results": [{"exit_code": 0}]}
        with patch(
            "RemoteAgent.telegram_transport.TelegramReceiverConfig.from_env",
            return_value=config,
        ), patch(
            "RemoteAgent.telegram_direct_cmd.execute_cmd_request",
            return_value=output,
        ) as run:
            receiver._process_updates([packet()])
            self.assertTrue(receiver.wait_for_controls(5))
            self.assertEqual(run.call_count, 1)
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["user_id"], 11)
            self.assertEqual(kwargs["chat_id"], 22)
            kwargs["policy"].assert_authorized(11, 22)
            self.assertEqual(kwargs["trusted_install_root"], self.root)
            self.assertEqual(self.client.sent[-1][0], 22)
            self.assertEqual(self.client.sent[-1][2]["reply_to_message_id"], 111)
            self.assertIn("狀態：SUCCESS", self.client.sent[-1][1])

    def test_unpaired_private_sender_is_rejected_before_execution(self):
        config, pairing, receiver = self._receiver()
        with patch(
            "RemoteAgent.telegram_transport.TelegramReceiverConfig.from_env",
            return_value=config,
        ), patch(
            "RemoteAgent.telegram_direct_cmd.execute_cmd_request",
        ) as run:
            receiver._process_updates([packet(update_id=102)])
            self.assertTrue(receiver.wait_for_controls(5))
            self.assertEqual(run.call_count, 0)
            self.assertEqual(self.client.sent, [])

    def test_static_telegram_allowlist_is_reused_without_pairing(self):
        config, _, receiver = self._receiver(
            pairing_enabled=False, users=(11,), chats=(22,)
        )
        output = {"message": "DIRECT_CMD_STATIC_ALLOWLIST_OK",
                  "control_id": "TG-CMD-STATIC", "status": "SUCCESS",
                  "results": [{"exit_code": 0}]}
        with patch(
            "RemoteAgent.telegram_transport.TelegramReceiverConfig.from_env",
            return_value=config,
        ), patch(
            "RemoteAgent.telegram_direct_cmd.execute_cmd_request",
            return_value=output,
        ) as run:
            receiver._process_updates([packet(update_id=103)])
            self.assertTrue(receiver.wait_for_controls(5))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.client.sent[-1][0], 22)

    def test_dispatcher_rejects_forged_non_telegram_caller(self):
        for transport in ("WEBGPT", "LOCAL_AGENT", None):
            message = SimpleNamespace(
                transport=transport, endpoint="api.telegram.org",
                conversation_key="telegram://chat/22", sender_id="11",
                reply_context={"chat_id": 22, "message_id": 111},
                text="run cmd by TG\ncmd path: \"C:\\smartagent\"\ncommand:\n\"echo hello\"",
            )
            with self.subTest(transport=transport), patch(
                "RemoteAgent.telegram_direct_cmd.execute_cmd_request"
            ) as run:
                result = self.dispatcher.handle(message)
                self.assertIn("拒絕", result["message"])
                run.assert_not_called()

    def test_dispatcher_rechecks_existing_pairing_current_state(self):
        config, pairing, _ = self._receiver()
        message = SimpleNamespace(
            transport="TELEGRAM", endpoint="api.telegram.org",
            conversation_key="telegram://chat/22", sender_id="11",
            reply_context={"chat_id": 22, "message_id": 111},
            text="run cmd by TG\ncmd path: \"C:\\smartagent\"\ncommand:\n\"echo hello\"",
        )
        with patch(
            "RemoteAgent.telegram_transport.TelegramReceiverConfig.from_env",
            return_value=config,
        ), patch(
            "RemoteAgent.telegram_direct_cmd.execute_cmd_request",
        ) as run:
            result = self.dispatcher.handle(message)
            self.assertIn("未授權", result["message"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
