"""Git67 byte-exact IT10 capture & collector regressions; no network / no Runtime actions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core import path_integrity_trace as PIT
from agent_core.it10_raw_collector import collect
from agent_core.request_context import RequestContextStore
from agent_core.web_ui.providers.chatgpt.composer import read_composer_snapshot
from RemoteAgent.telegram_transport import TelegramBotClient, TelegramReceiverConfig, TelegramUpdateAdapter


REQUEST = ("NLIT_CASE_ID=IT10\nNLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
           "NLIT_MANUAL_RUN_ID=GIT67-RAW-TEST\n"
           "E:\\AIspace\\.case_d_backups\\中文\\probe.txt\r\n")
TASK = "TASK-GIT67-RAW"
RID = "RR-GIT67-RAW"


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.raw


class RawCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.private = self.root / "private"
        self.stage = self.root / "stage"
        self.private.mkdir()
        self.stage.mkdir()
        self.env = patch.dict(os.environ, {
            "SMARTAGENT_IT10_PATH_TRACE": "1",
            "SMARTAGENT_IT10_PATH_TRACE_RAW": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "GIT67-RAW-OFFLINE",
            "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(self.private),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        PIT.begin(REQUEST, task_id=TASK, request_id=RID)

    def _rows(self):
        return [json.loads(line) for p in self.private.glob("it10-path-*.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines()]

    def _update(self):
        return {"update_id": 71, "message": {"message_id": 72, "date": 12345678,
                 "from": {"id": 1, "is_bot": False},
                 "chat": {"id": 1, "type": "private"},
                 "text": REQUEST}}

    def test_raw_blob_exact_utf8_bytes_and_pointer_closure(self):
        PIT.record("06_worker_input", REQUEST)
        row = self._rows()[0]
        original = REQUEST.encode("utf-8")
        self.assertEqual(row["raw_blob_status"], "PERSISTED")
        self.assertEqual(row["raw_origin"], "UTF8_FROM_PYTHON_STR")
        self.assertEqual((self.private / row["raw_blob_ref"]).read_bytes(), original)
        self.assertEqual(row["raw_blob_sha256"], hashlib.sha256(original).hexdigest())
        result = collect(TASK, RID, self.private, self.stage)
        self.assertEqual(result["status"], "PARTIAL")  # Other live boundaries absent.
        self.assertEqual(result["raw_blob_count"], 1)
        self.assertTrue((self.stage / "it10/raw" / row["raw_blob_ref"]).is_file())
        self.assertEqual((self.stage / "it10/raw" / row["raw_blob_ref"]).read_bytes(), original)
        self.assertEqual(len(result["missing_refs"]), 0)

    def test_request_context_pointer_resolves_original_file_bytes(self):
        store = RequestContextStore(self.root / "contexts", RID, "epoch", REQUEST)
        path = str(self.root / "probe.txt")
        reference = store.remember("path", path, "USER_REQUEST")
        original = store.path.read_bytes()
        self.assertEqual(store.resolve("path", reference), path)
        rows = [row for row in self._rows() if row["stage"] == "12a_request_context_file_bytes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_origin"], "REQUEST_CONTEXT_FILE_BYTES")
        self.assertEqual((self.private / rows[0]["raw_blob_ref"]).read_bytes(), original)
        result = collect(TASK, RID, self.private, self.stage)
        self.assertEqual(result["raw_blob_count"], 1)
        self.assertEqual((self.stage / "it10/raw" / rows[0]["raw_blob_ref"]).read_bytes(), original)

    def test_composer_raw_dom_and_semantic_views_remain_separate(self):
        inner_html = "<p>E:\\AIspace\\.case_d_backups</p><p>下一行</p>"
        text_content = "E:\\AIspace\\.case_d_backups下一行"
        class Locator:
            def evaluate(self, script):
                if "innerHTML:" in script:
                    return {"innerHTML": inner_html, "textContent": text_content}
                return {"primary": "E:\\AIspace\\.case_d_backups\n下一行",
                        "semantic": "E:\\AIspace\\.case_d_backups\n下一行"}
        snapshot = read_composer_snapshot(Locator())
        self.assertIn(".case_d_backups", snapshot.primary_text)
        observed = {row["stage"]: row for row in self._rows()}
        for stage, exact in (("09_composer_dom_innerHTML", inner_html),
                             ("09_composer_dom_textContent", text_content)):
            self.assertEqual(observed[stage]["evidence_status"], "DOM_SERIALIZATION")
            blob = self.private / observed[stage]["raw_blob_ref"]
            self.assertEqual(blob.read_bytes(), exact.encode("utf-8"))

    def test_unarmed_raw_is_not_persisted(self):
        with patch.dict(os.environ, {"SMARTAGENT_IT10_PATH_TRACE_RAW": "NO"}):
            PIT.record("06_worker_input", REQUEST)
            self.assertFalse(list(self.private.glob("*.bin")))
            self.assertEqual(collect(TASK, RID, self.private, self.stage)["status"], "NOT_ARMED")

    def test_missing_raw_pointer_is_explicit(self):
        PIT.record("06_worker_input", REQUEST)
        reference = self._rows()[0]["raw_blob_ref"]
        (self.private / reference).unlink()
        result = collect(TASK, RID, self.private, self.stage)
        self.assertEqual(result["raw_blob_count"], 0)
        self.assertTrue(any(x.startswith("RAW_MISSING_OR_OVERSIZED") for x in result["missing_refs"]))

    def test_http_body_exact_before_decode(self):
        raw = json.dumps({"ok": True, "result": [self._update()]},
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        config = TelegramReceiverConfig(bot_token="LOCAL-TEST-NO-NETWORK",
                    allowed_user_ids=(1,), allowed_chat_ids=(1,))
        client = TelegramBotClient(config, opener=lambda *a, **k: FakeResponse(raw))
        update = client.get_updates(offset=0, timeout=1)[0]
        message, reason = TelegramUpdateAdapter(config).normalize(update)
        self.assertEqual(reason, "")
        self.assertEqual(message.text, REQUEST.strip())
        rows = [row for row in self._rows() if row["stage"] == "01b_telegram_http_body"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_origin"], "TELEGRAM_GETUPDATES_HTTP_RESPONSE_BODY")
        self.assertEqual((self.private / rows[0]["raw_blob_ref"]).read_bytes(), raw)
        self.assertEqual(rows[0]["raw_blob_sha256"], hashlib.sha256(raw).hexdigest())

    def test_mixed_http_update_batch_is_not_leaked(self):
        item = self._update()
        other = {"update_id": 72, "message": {
            "message_id": 73, "date": 12345679, "from": {"id": 2, "is_bot": False},
            "chat": {"id": 2, "type": "private"}, "text": "another private conversation"}}
        raw = json.dumps({"ok": True, "result": [item, other]}, ensure_ascii=False).encode("utf-8")
        config = TelegramReceiverConfig(bot_token="LOCAL-TEST-NO-NETWORK",
                    allowed_user_ids=(1,), allowed_chat_ids=(1,))
        update = TelegramBotClient(config, opener=lambda *a, **k: FakeResponse(raw)).get_updates(offset=0, timeout=1)[0]
        TelegramUpdateAdapter(config).normalize(update)
        self.assertFalse(any(row["stage"] == "01b_telegram_http_body" for row in self._rows()))

    def test_wrong_task_or_request_cannot_collect_neighbor_trace(self):
        PIT.record("06_worker_input", REQUEST)
        self.assertEqual(collect("TASK-FOREIGN", RID, self.private, self.stage)["status"],
                         "NO_TASK_REQUEST_BOUND_TRACE")
        self.assertEqual(collect(TASK, "RR-FOREIGN", self.private, self.stage)["status"],
                         "NO_TASK_REQUEST_BOUND_TRACE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
