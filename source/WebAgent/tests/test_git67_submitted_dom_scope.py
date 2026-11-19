"""IT10 post-submit DOM evidence bound to a fresh per-submit user turn.

Only temporary private roots and synthetic DOM handles; no network or Runtime
actions. An offline fixture cannot substitute for live browser evidence.
"""
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
from agent_core.web_runtime import WebLLMScraper


REQUEST = (
    "NLIT_CASE_ID=IT10\n"
    "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
    "IT10_SUBCASE_ID=GIT67-SCOPED-DOM-CAPTURE-001\n"
    "請檢查 E:\\AIspace\\.case_d_backups\n"
)
NONCE = "WUS-" + "A" * 32
OTHER_NONCE = "WUS-" + "B" * 32


class UserContent:
    def __init__(self, text):
        self.text = text

    def evaluate(self, script):
        if "innerText:el.innerText" in script:
            return {"innerText": self.text, "textContent": self.text}
        if "el.innerHTML" in script:
            return "<p>" + self.text.replace("\n", "</p><p>") + "</p>"
        raise AssertionError("unexpected DOM evaluation")


class UserTurn:
    def __init__(self, children):
        self.children = children

    def query_selector_all(self, selector):
        assert selector == ".content"
        return list(self.children)


class SubmittedDomScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "SMARTAGENT_IT10_PATH_TRACE": "1",
            "SMARTAGENT_IT10_PATH_TRACE_RAW": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "GIT67-SCOPED-DOM-TEST",
            "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(self.private),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        PIT.begin(REQUEST, request_id="RR-DOM-1", task_id="TASK-DOM-1")
        self.addCleanup(lambda: PIT.begin(""))

    def _rows(self):
        return [json.loads(line) for p in self.private.glob("it10-path-*.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _scope(content, *, nonce=NONCE, raw=None, bound=True):
        root = UserTurn([content])
        turn = SimpleNamespace(
            raw_text=nonce + " " + REQUEST if raw is None else raw,
            element=root,
        ) if bound else None
        return SimpleNamespace(
            prompt_text="[WEBUI_SUBMIT_ID]\n" + nonce
                        + "\n[/WEBUI_SUBMIT_ID]\n" + REQUEST,
            request_markers=(nonce,),
            user_turn=turn,
        )

    def test_unique_confirmed_nonce_captures_raw_dom_views(self):
        content = UserContent("[WEBUI_SUBMIT_ID]\n" + NONCE
                              + "\n[/WEBUI_SUBMIT_ID]\n" + REQUEST)
        adapter = SimpleNamespace(profile=SimpleNamespace(user_content_selector=".content"))
        self.assertEqual(PIT.record_scoped_submitted_user_dom(self._scope(content), adapter),
                         "CAPTURED")
        rows = {r["stage"]: r for r in self._rows()}
        for stage in ("10_submitted_innerText", "10_submitted_textContent",
                      "10_submitted_innerHTML"):
            self.assertIn(stage, rows)
            row = rows[stage]
            self.assertEqual(row["raw_blob_status"], "PERSISTED")
            blob = (self.private / row["raw_blob_ref"]).read_bytes()
            self.assertEqual(hashlib.sha256(blob).hexdigest(), row["raw_blob_sha256"])
        self.assertEqual((self.private / rows["10_submitted_innerText"]["raw_blob_ref"]).read_bytes(),
                         content.text.encode("utf-8"))
        self.assertIn(r"E:\AIspace\.case_d_backups", content.text)

    def test_unbound_foreign_or_ambiguous_turn_produces_no_raw_evidence(self):
        content = UserContent(NONCE + "\n" + REQUEST)
        adapter = SimpleNamespace(profile=SimpleNamespace(user_content_selector=".content"))
        for scope, result in (
            (self._scope(content, bound=False), "USER_TURN_UNBOUND"),
            (self._scope(content, raw=OTHER_NONCE + REQUEST), "BOUND_NONCE_NOT_VISIBLE"),
            (self._scope(UserContent(OTHER_NONCE + REQUEST)), "CONTENT_NOT_FOUND"),
        ):
            self.assertEqual(PIT.record_scoped_submitted_user_dom(scope, adapter), result)
        scope = self._scope(content)
        scope.user_turn.element = UserTurn([content, UserContent(content.text)])
        self.assertEqual(PIT.record_scoped_submitted_user_dom(scope, adapter), "CONTENT_AMBIGUOUS")
        self.assertEqual(self._rows(), [])
        self.assertFalse(list(self.private.glob("*.bin")))

    def test_runtime_hook_records_once_and_does_not_alter_snapshot_state(self):
        content = UserContent(NONCE + "\n" + REQUEST)
        scope = self._scope(content)
        adapter = SimpleNamespace(profile=SimpleNamespace(user_content_selector=".content"))
        snapshot = {"_web_ui_scope": scope, "user_count": 7}
        logged = []
        fake = SimpleNamespace(_web_ui_adapter=lambda: adapter,
                               _log_stage=lambda *parts: logged.append(parts))
        WebLLMScraper._observe_scoped_it10_user_dom(fake, snapshot)
        self.assertEqual(snapshot["_it10_submitted_dom_status"], "CAPTURED")
        before = len(self._rows())
        WebLLMScraper._observe_scoped_it10_user_dom(fake, snapshot)
        self.assertEqual(len(self._rows()), before)
        self.assertEqual(snapshot["user_count"], 7)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0][0], "it10_submitted_dom_capture")


if __name__ == "__main__":
    unittest.main(verbosity=2)
