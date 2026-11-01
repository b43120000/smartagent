"""PR-0 bounded Task Requirement + full V9 no-tool admission regression.

Offline, scripted Planner, temporary workspace; never actual WebGPT/Telegram.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.task_requirement import (
    SCHEMA, assess_task_requirement, enabled, render_task_requirement,
)
from agent_core.tri_one_answer_only import classify_original_request
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_tri_one_5a_protocol_it import answer_only_response


class PR0ContractTests(unittest.TestCase):
    def assess(self, request, **flags):
        row = assess_task_requirement(request, **flags)
        self.assertEqual(row["schema"], SCHEMA)
        self.assertEqual(row["decision_owner"], "RUNTIME_POLICY")
        self.assertFalse(row["grants_authorization"])
        self.assertFalse(row["verification_complete"])
        self.assertEqual(len(row["intent_sha256"]), 64)
        return row

    def test_text_only_and_inline(self):
        for request in (
            "Explain RAII in C++.",
            "請幫我解釋 RAII 原理",
            "請解釋 GitHub branch 與 commit 的差異",
            "請解釋如何編譯 C++ 程式",
            "列出 std::unique_ptr 優點",
            "請寫一個 C++ std::unique_ptr 範例",
            "Review 以下程式碼：std::unique_ptr<int> p;",
            "翻譯以下文字：Please send email now",
            "請解釋 C++ RAII（Resource Acquisition Is Initialization）的設計原理，"
            "並提供一個使用 std::unique_ptr 的簡短範例。\n"
            "只需要文字解釋與程式碼範例，不需要執行程式、讀取檔案或修改任何資料。\n"
            "請完成後直接回報結果。",
        ):
            with self.subTest(request=request):
                row = self.assess(request)
                self.assertEqual(row["route"], "ANSWER_ONLY", row)
                self.assertFalse(row["required_operations"])
                self.assertTrue(classify_original_request(request)[0])

    def test_resource_sources_and_actions(self):
        samples = (
            ("Review GitHub PR #42", ["GITHUB"], ["READ"]),
            ("請修改本地 sensor.cpp 檔案", ["LOCAL"], ["WRITE"]),
            ("請修改 GitHub repo，並在本地執行測試",
             ["GITHUB", "LOCAL"], ["EXECUTE", "READ", "WRITE"]),
            ("Read this workspace file then summarize.", ["LOCAL"], ["READ"]),
            ("請解釋 RAII，然後把結果貼到 Slack",
             ["EXTERNAL"], ["DELIVER"]),
            ("請解釋 RAII，並且幫我 push 到 GitHub repo",
             ["GITHUB"], ["READ", "WRITE"]),
            ("What is the exchange rate today?", ["EXTERNAL"], ["READ"]),
            ("Explain the latest GitHub status of my repository.",
             ["EXTERNAL"], ["READ"]),
        )
        for request, sources, operations in samples:
            with self.subTest(request=request):
                row = self.assess(request)
                self.assertEqual(row["route"], "RESOURCE_REQUIRED", row)
                self.assertEqual(row["resource_sources"], sources, row)
                self.assertEqual(row["required_operations"], operations, row)
                self.assertFalse(classify_original_request(request)[0])

    def test_live_b_github_main_read_only_explicit_no_write(self):
        # Original Telegram Live B task: a metadata noun and prohibitions
        # must not become WRITE prerequisites.
        request = (
            "請查詢 GitHub 上 b43120000/smartagent Repository 的 main branch 最新 Commit，"
            "僅做唯讀查詢，不要修改或推送任何資料。"
        )
        row = self.assess(request)
        self.assertEqual(row["route"], "RESOURCE_REQUIRED")
        self.assertEqual(row["resource_sources"], ["GITHUB"])
        self.assertEqual(row["required_operations"], ["READ"])
        self.assertEqual(row["required_capabilities"], ["GITHUB_REPOSITORY_ACCESS"])
        self.assertFalse(row["side_effect_required"])
        self.assertFalse(row["grants_authorization"])
        self.assertIn("EXPLICIT_NO_OPERATION_CONSTRAINTS", row["restrictions"])
        self.assertFalse(classify_original_request(request)[0])

    def test_prohibited_operations_do_not_erase_positive_instructions(self):
        cases = (
            ("請查詢 GitHub main 最新 Commit，不要修改任何資料。",
             ["GITHUB"], ["READ"]),
            ("請查詢 GitHub main 最新 commit，請勿 push。",
             ["GITHUB"], ["READ"]),
            ("請查詢 GitHub main 最新 commit，不得 git commit 或 push。",
             ["GITHUB"], ["READ"]),
            ("請查詢 GitHub main 最新 Commit，並且修改本地檔案。",
             ["GITHUB", "LOCAL"], ["READ", "WRITE"]),
            ("請查詢 GitHub main 最新 Commit，不要修改資料，但要推送變更。",
             ["GITHUB"], ["READ", "WRITE"]),
            ("請修改本地檔案，不要推送資料。",
             ["LOCAL"], ["WRITE"]),
            ("請解釋 GitHub branch 和 commit 的差異。",
             ["NONE"], []),
            ("請查詢 GitHub main HEAD，然後 git commit changes。",
             ["GITHUB"], ["READ", "WRITE"]),
        )
        for request, sources, operations in cases:
            with self.subTest(request=request):
                row = self.assess(request)
                self.assertEqual(row["resource_sources"], sources, row)
                self.assertEqual(row["required_operations"], operations, row)
                self.assertEqual(row["route"],
                                 "ANSWER_ONLY" if not operations else "RESOURCE_REQUIRED")

    def test_independent_followup_action_never_becomes_answer_only(self):
        cases = (
            ("請解釋 RAII。請修改本地檔案", "WRITE"),
            ("請解釋 RAII。\n請把答案寄給 Slack", "DELIVER"),
            ("請解釋 RAII，接著請幫我讀取檔案", "READ"),
            ("請解釋 RAII，不需要執行程式，仍要修改資料", "WRITE"),
            ("請解釋 GitHub branch。\n請幫我 push repo", "WRITE"),
        )
        for request, operation in cases:
            with self.subTest(request=request):
                row = self.assess(request)
                self.assertEqual(row["route"], "RESOURCE_REQUIRED", row)
                self.assertIn(operation, row["required_operations"])
                self.assertFalse(classify_original_request(request)[0])

    def test_uncertain_and_trusted_scope_fail_closed(self):
        for request in (
            "Hello", "幫我處理一下剛才的問題",
            "Explain the attached document.",
            "What is the status of my project?",
        ):
            with self.subTest(request=request):
                self.assertEqual(self.assess(request)["route"], "AMBIGUOUS")
        for options in (
            {"attachments": True}, {"authorized_paths": True},
            {"image_or_artifact_request": True}, {"continuation": True},
        ):
            self.assertNotEqual(
                self.assess("Explain RAII in C++.", **options)["route"], "ANSWER_ONLY")
        self.assertNotEqual(self.assess(
            "Review GitHub https://github.com/example/project")["route"], "ANSWER_ONLY")
        self.assertNotEqual(self.assess(
            "請解釋 RAII，不需要執行程式，但請讀取檔案。")["route"], "ANSWER_ONLY")

    def test_feature_and_render(self):
        self.assertTrue(enabled({"SMARTAGENT_PR0_V1": "1"}))
        self.assertFalse(enabled({"SMARTAGENT_PR0_V1": "0"}))
        row = self.assess("請解釋 RAII")
        text = render_task_requirement(row)
        self.assertIn("[SMARTAGENT_TASK_REQUIREMENT_V1]", text)
        self.assertIn('"grants_authorization":false', text)
        self.assertNotIn("intent_sha256", text)

    def test_shared_v9_5a_execution_and_denial(self):
        cases = (
            ("請幫我解釋 RAII，並提供 C++ std::unique_ptr 範例。", True),
            ("請解釋 RAII，並且幫我 push 到 GitHub repo", False),
            ("What is the status of my project?", False),
        )
        for request, accepted in cases:
            with self.subTest(request=request), tempfile.TemporaryDirectory() as directory:
                prompts, events = [], []
                def planner(prompt, _expected, _attachments):
                    prompts.append(prompt)
                    return answer_only_response()
                root = Path(directory)
                loop = WebAgentProtocolLoop(
                    root, planner, progress_root=root, max_turns=2,
                    event_sink=lambda name, **kw: events.append((name, kw)),
                )
                with patch.dict(os.environ, {
                    "SMARTAGENT_PR0_V1": "1",
                    "TRI_ONE_TASK_RESULT_V1": "1",
                    "TRI_ONE_TEXT_ONLY_V1": "1",
                }), patch.object(loop.tools, "execute",
                                 side_effect=AssertionError("no tools allowed")):
                    if accepted:
                        self.assertEqual(loop.run(
                            request, source_tag="REMOTEAGENT_TELEGRAM"),
                            "RAII releases owned resources.")
                        self.assertEqual(len(prompts), 1)
                        self.assertEqual(
                            loop.terminal_acceptance["completion_kind"], "ANSWER_ONLY")
                    else:
                        with self.assertRaises(Exception):
                            loop.run(request, source_tag="REMOTEAGENT_TELEGRAM")
                        self.assertFalse(loop.terminal_acceptance)
                found = [row for name, row in events
                         if name == "task_requirement_assessed"]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["route"],
                                 self.assess(request)["route"])
                self.assertIn("[SMARTAGENT_TASK_REQUIREMENT_V1]", prompts[0])
                self.assertFalse(found[0]["grants_authorization"])
                self.assertEqual(loop.action_result_ledger, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
