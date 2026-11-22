"""Git67 IT10 Request-scoped identity and strict collector isolation tests.

Synthetic Telegram input only. No network, browser, or Runtime Tool actions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core import path_integrity_trace as PIT
from agent_core.agent_gateway import AgentIngressGateway
from agent_core.it10_raw_collector import collect
from RemoteAgent.telegram_transport import TelegramReceiverConfig, TelegramUpdateAdapter


REQUEST = (
    "NLIT_CASE_ID=IT10\n"
    "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
    "IT10_SUBCASE_ID=GIT67-IDENTITY-DUPLICATE-001\n"
    "請檢查 E:\\AIspace\\.case_d_backups 這個資料夾。\n"
)
RUN_ID = "GIT67-TRACE-IDENTITY-UT"


class TraceIdentityIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name) / "private"
        self.private.mkdir()
        self.env = patch.dict(os.environ, {
            "SMARTAGENT_IT10_PATH_TRACE": "1",
            "SMARTAGENT_IT10_PATH_TRACE_RAW": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": RUN_ID,
            "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(self.private),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        PIT.begin("")
        self.addCleanup(lambda: PIT.begin(""))

    @staticmethod
    def _update(update_id):
        return {"update_id": update_id, "message": {
            "message_id": update_id + 1, "date": 12345678,
            "from": {"id": 1, "is_bot": False},
            "chat": {"id": 1, "type": "private"}, "text": REQUEST,
        }}

    def _stage(self, name):
        stage = Path(self.tmp.name) / name
        stage.mkdir()
        return stage

    def test_same_text_distinct_telegram_updates_are_isolated_end_to_end(self):
        adapter = TelegramUpdateAdapter(TelegramReceiverConfig(
            bot_token="LOCAL-TEST-NO-NETWORK",
            allowed_user_ids=(1,), allowed_chat_ids=(1,),
        ))
        collected = []
        for update_id, task_id in ((701, "TASK-FIRST"), (702, "TASK-SECOND")):
            update = self._update(update_id)
            raw_http = json.dumps({"ok": True, "result": [update]}, ensure_ascii=False).encode()
            PIT.note_telegram_http_body(raw_http)
            message, reason = adapter.normalize(update)
            self.assertFalse(reason)
            self.assertEqual(message.text, REQUEST.strip())
            request_id = AgentIngressGateway._request_id(message)
            receiver = PIT.context()
            self.assertEqual(receiver["request_id"], request_id)
            self.assertTrue(receiver["trace_id"].startswith(RUN_ID + "-"))
            PIT.begin(REQUEST, request_id=request_id, transport="TELEGRAM")
            self.assertEqual(PIT.context()["trace_id"], receiver["trace_id"])
            PIT.record("04_gateway_input", message.text)
            PIT.begin(message.text, task_id=task_id, request_id=request_id, transport="TELEGRAM")
            PIT.record("05_task_store", message.text)
            PIT.begin(message.text, task_id=task_id, request_id=request_id, transport="TELEGRAM")
            PIT.record("06_worker_input", message.text)
            collected.append((receiver["trace_id"], task_id, request_id, raw_http))

        self.assertNotEqual(collected[0][0], collected[1][0])
        for i, (trace_id, task_id, request_id, raw_http) in enumerate(collected):
            stage = self._stage("stage_" + str(i))
            result = collect(task_id, request_id, self.private, stage)
            self.assertEqual(result["selected_trace_ids"], [trace_id])
            self.assertEqual(result["missing_refs"], [])
            self.assertEqual(result["foreign_trace_files_excluded"], 0)
            self.assertEqual(result["foreign_trace_rows_excluded"], 0)
            self.assertGreaterEqual(result["raw_blob_count"], 5)
            observed = {row["stage"] for row in result["boundary_records"]}
            self.assertTrue({"01b_telegram_http_body", "02_telegram_message_text",
                             "03_receiver_normalized", "04_gateway_input",
                             "05_task_store", "06_worker_input"}.issubset(observed))
            self.assertTrue(all(row["trace_id"] == trace_id for row in result["boundary_records"]))
            self.assertFalse(any(
                other_id in json.dumps(result["boundary_records"])
                for _, _, other_id, _ in collected if other_id != request_id
            ))
            raw_row = next(row for row in result["boundary_records"]
                           if row["stage"] == "01b_telegram_http_body")
            body = (stage / "it10" / "raw" / raw_row["raw_blob_ref"]).read_bytes()
            self.assertEqual(body, raw_http)
            self.assertEqual(hashlib.sha256(body).hexdigest(), raw_row["raw_blob_sha256"])

    def test_legacy_text_only_collision_does_not_contaminate_collection(self):
        legacy_id = RUN_ID + "-" + hashlib.sha256(REQUEST.strip().encode()).hexdigest()[:24]
        for pid, task_id, request_id in (
            (11111, "TASK-FIRST", "RR-FIRST"), (22222, "TASK-SECOND", "RR-SECOND"),
        ):
            PIT.restore({
                "trace_id": legacy_id, "request_id": request_id, "task_id": task_id,
                "transport": "TELEGRAM", "subcase_id": "GIT67-IDENTITY-DUPLICATE-001",
            })
            with patch("agent_core.path_integrity_trace.os.getpid", return_value=pid):
                PIT.record("06_worker_input", REQUEST)
        result = collect("TASK-FIRST", "RR-FIRST", self.private, self._stage("foreign"))
        self.assertEqual(result["selected_trace_ids"], [legacy_id])
        self.assertEqual(result["missing_refs"], [])
        self.assertEqual(result["foreign_trace_files_excluded"], 1)
        self.assertEqual(result["foreign_trace_rows_excluded"], 1)
        self.assertEqual(result["raw_blob_count"], 1)
        self.assertEqual(result["trace_file_count"], 1)
        self.assertEqual(len(result["boundary_records"]), 1)
        self.assertNotIn("RR-SECOND", json.dumps(result["boundary_records"]))

    def test_mixed_identity_in_one_jsonl_still_fails_closed(self):
        legacy_id = RUN_ID + "-" + hashlib.sha256(REQUEST.strip().encode()).hexdigest()[:24]
        with patch("agent_core.path_integrity_trace.os.getpid", return_value=33333):
            for task_id, request_id in (
                ("TASK-FIRST", "RR-FIRST"), ("TASK-SECOND", "RR-SECOND"),
            ):
                PIT.restore({
                    "trace_id": legacy_id, "request_id": request_id, "task_id": task_id,
                    "transport": "TELEGRAM", "subcase_id": "GIT67-IDENTITY-DUPLICATE-001",
                })
                PIT.record("06_worker_input", REQUEST)
        result = collect("TASK-FIRST", "RR-FIRST", self.private, self._stage("mixed"))
        self.assertTrue(any(ref.startswith("TRACE_IDENTITY_MISMATCH:")
                            for ref in result["missing_refs"]))
        self.assertEqual(result["trace_file_count"], 0)
        self.assertEqual(result["raw_blob_count"], 0)
        self.assertEqual(result["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
