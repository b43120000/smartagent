"""Offline integrity/security regressions; never dispatches a real request."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core import path_integrity_trace as trace
from agent_core.agent_gateway import AgentIngressGateway
from agent_core.request_context import RequestContextStore
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.evidence_foundation import evaluate_predicate
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result
from agent_core.web_ui.providers.chatgpt.composer import canonical_composer_text, normalize_composer_text
from RemoteAgent.telegram_transport import TelegramUpdateAdapter, TelegramReceiverConfig
from WebAgent.protocol_loop import extract_authorized_paths

EXPECTED = r"E:\card\__runtime_evidence_nlit_fixture__\NLIT-IT6-FIXED-002\IT10\__unknown_parent_not_created__\probe.txt"
BAD = r"E:\card_runtime_evidence_nlit_fixture_\NLIT-IT6-FIXED-002\IT10_unknown_parent_not_created_\probe.txt"
MARKDOWN = r"E:\card\_*runtime_evidence_nlit_fixture*\_\NLIT-IT6-FIXED-002\IT10\_*unknown_parent_not_created*\_\probe.txt"
MARKERS = "NLIT_CASE_ID=IT10\nNLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / "private"
        self.output.mkdir()
        self.env = patch.dict(os.environ, {
            "SMARTAGENT_IT10_PATH_TRACE": "1",
            "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "OFFLINE-REGRESSION",
            "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(self.output),
            "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
        })
        self.env.start()
        trace.begin("")

    def tearDown(self):
        trace.begin("")
        self.env.stop()
        self.tmp.cleanup()

    def ingress(self, text):
        adapter = TelegramUpdateAdapter(TelegramReceiverConfig(
            enabled=True, bot_token="OFFLINE-NOT-USED", allowed_user_ids=(1,),
            allowed_chat_ids=(1,), workspace=str(self.root)))
        update = {"update_id": 7, "message": {"message_id": 8, "date": 1,
                  "from": {"id": 1, "is_bot": False}, "chat": {"id": 1, "type": "private"},
                  "text": text, "entities": [{"type": "italic", "offset": 1, "length": 2}]}}
        message, reason = adapter.normalize(update)
        self.assertFalse(reason)
        queue = RemoteTaskQueue(TaskStateStore(self.root / "tasks.json"))
        gateway = AgentIngressGateway(task_queue=queue,
                    session_router=TransportSessionRouter(self.root / "sessions.json"))
        task = gateway.accept(message, workspace=str(self.root)).task
        reloaded = TaskStateStore(self.root / "tasks.json").get(task.task_id)
        self.assertEqual(message.text, text.strip())
        self.assertEqual(reloaded.request, text.strip())
        self.assertEqual(reloaded.metadata["source_text"], text.strip())
        self.assertEqual(reloaded.metadata["source_user_command"], text.strip())
        return reloaded

    def test_PATH_01_receiver_gateway_store(self):
        task = self.ingress(MARKERS + EXPECTED)
        self.assertEqual(extract_authorized_paths(task.request), [EXPECTED])
        store = RequestContextStore(self.root / "contexts", task.request_id, task.task_epoch, task.request)
        ref = store.remember("path", EXPECTED, "USER_REQUEST")
        action = store.expand({"tool": "observe_fact", "action_id": "UNIT-PATH-01", "field_refs": {"path": ref}})
        self.assertEqual(action["path"], EXPECTED)

    def test_PATH_02_markdown_is_raw_not_rendered(self):
        task = self.ingress(MARKERS + MARKDOWN)
        self.assertIn(MARKDOWN, task.request)
        self.assertNotEqual(MARKDOWN, BAD)
        self.assertEqual(extract_authorized_paths(MARKDOWN), [MARKDOWN])
        # Real offline renderer/DOM comparison runs separately; this test must
        # not pretend a hardcoded rendered value is a browser observation.

    def test_PATH_03_json_codepoints(self):
        for value in (EXPECTED, BAD, MARKDOWN, "中文 空白\u00a0\u200b\r\n" + EXPECTED):
            with self.subTest(value=value):
                self.assertEqual([ord(c) for c in json.loads(json.dumps(value))], [ord(c) for c in value])

    def test_PATH_04_raw_vs_comparison(self):
        text = MARKERS + "E:\\中文 空間\\__下\u200b層__\\檔\u00a0案.txt\r\n" + EXPECTED
        task = self.ingress(text)
        self.assertEqual(task.request, text)
        self.assertNotEqual(canonical_composer_text(text), text)
        self.assertIn("\u200b", canonical_composer_text(text))
        self.assertNotEqual(normalize_composer_text(text), text)
        self.assertEqual(canonical_composer_text(EXPECTED), EXPECTED)
        self.assertEqual(normalize_composer_text(EXPECTED), EXPECTED)

    def test_PATH_05_scope_rejection_no_probe_or_repair(self):
        agent = SimpleNamespace(workspace_root=Path("E:/card"), current_request_id="UNIT",
                                current_task_id="UNIT-TASK", current_task_epoch="UNIT-EPOCH",
                                _authorized_local_paths=(), _runtime_completion_binding={
                                    "request_id": "UNIT", "task_epoch": "UNIT-EPOCH", "success_conditions": ("COND",)})
        action = {"tool": "observe_fact", "action_id": "UNIT-SCOPE", "path": BAD,
                  "condition_id": "COND", "predicate": {"field": "file.exists", "operator": "equals", "expected": False}}
        trace.begin(MARKERS + BAD, request_id="UNIT", task_id="UNIT-TASK")
        import os as filesystem
        real_stat = filesystem.stat
        unauthorized_stats = []
        def spy(path, *args, **kwargs):
            if str(path).casefold() == BAD.casefold():
                unauthorized_stats.append(str(path))
            return real_stat(path, *args, **kwargs)
        with patch("agent_core.evidence_foundation.probe_file_exists") as probe, patch("os.stat", side_effect=spy):
            result = decode_result(execute_tool(action, agent=agent))
        self.assertEqual(result["reason"], "EVIDENCE_SCOPE_REJECTED")
        self.assertFalse(result["executed"])
        self.assertIn(BAD, result["detail"])
        self.assertEqual(action["path"], BAD)
        self.assertFalse(probe.called)
        self.assertFalse(unauthorized_stats)

    def test_PATH_06_field_ref_identity(self):
        store = RequestContextStore(self.root / "contexts", "UNIT", "EPOCH")
        for value in (EXPECTED, BAD, MARKDOWN, "E:\\中文 空白\\__層__\\檔.txt"):
            ref = store.remember("path", value, "USER_REQUEST")
            action = store.expand({"tool": "observe_fact", "action_id": "UNIT", "field_refs": {"path": ref}})
            self.assertEqual(action["path"].encode("utf-8"), value.encode("utf-8"))
        with self.assertRaises(ValueError):
            RequestContextStore(self.root / "contexts", "UNIT", "FOREIGN").expand(
                {"tool": "observe_fact", "field_refs": {"path": ref}})

    def test_PATH_07_unknown_is_not_false(self):
        predicate = {"field": "file.exists", "operator": "equals", "expected": False}
        for status in ("UNKNOWN", "REJECTED", "EVIDENCE_SCOPE_REJECTED", "UNVERIFIED"):
            fact = {"field": "file.exists", "observation_status": status, "freshness": "CURRENT", "executed": False}
            self.assertEqual(evaluate_predicate(predicate, fact)["verification_status"], "UNKNOWN")
            self.assertNotIn("value", fact)

    def test_PATH_08_privacy_gates_and_hash_contract(self):
        for flag, text in (("0", MARKERS + EXPECTED), ("1", "unmarked private conversation")):
            with patch.dict(os.environ, {"SMARTAGENT_IT10_PATH_TRACE": flag}):
                trace.begin(text)
                trace.record("01_sender_input", text)
        self.assertFalse(list(self.output.iterdir()))
        value = MARKERS + EXPECTED
        trace.begin(value, request_id="UNIT", task_id="UNIT-TASK", transport="LOCAL_TEST")
        trace.record("01_sender_input", value, extra={"update_id": 7, "bot_token": "MUST_NOT_RECORD",
                     "entities": [{"type": "text_link", "offset": 1, "length": 2,
                                   "url": "MUST_NOT_RECORD", "user": {"name": "MUST_NOT_RECORD"}}]})
        ctx = trace.context()
        trace.restore(ctx)
        trace.record("helper", value)
        rows = [json.loads(line) for p in self.output.glob("*.jsonl") for line in p.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["raw_utf8_sha256"], hashlib.sha256(value.encode()).hexdigest())
        self.assertEqual(rows[0]["path_value_sha256"], hashlib.sha256(EXPECTED.encode()).hexdigest())
        self.assertEqual(rows[0]["trace_id"], rows[1]["trace_id"])
        self.assertNotIn("MUST_NOT_RECORD", json.dumps(rows))
        trace.record("16_local_outbox_delivery", {"event": {"request_id": "FOREIGN", "task_id": "FOREIGN"}})
        self.assertEqual(sum(len(p.read_text().splitlines()) for p in self.output.glob("*.jsonl")), 2)
        trace.begin("ordinary new request")
        trace.record("new", "unrelated")
        self.assertFalse(trace.context())


if __name__ == "__main__":
    unittest.main(verbosity=2)
