#!/usr/bin/env python3
"""WebGPT1/shared runtime SR-02 + SR-03 deterministic format tests.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_shared_runtime_sr02_sr03.py
No browser, files outside temporary fixtures, task dispatch or network.
"""
from __future__ import annotations

import json
import unittest

from agent_core.narrative_bridge import _encode_canonical_tool_json
from agent_core.protocol_v8 import parse_v8_tool_transport
from agent_core.protocol_v9 import parse_v9_tool_transport

FENCE = chr(96) * 3


def cpp_content() -> str:
    return (
        "RAII example:\n"
        + FENCE + "cpp\n"
        + '#include <fstream>\nint main() { std::ofstream out("trace.txt"); }\n'
        + FENCE + "\n"
        + "Memory example:\n"
        + FENCE + "cpp\n"
        + "#include <memory>\nstd::unique_ptr<int> p = std::make_unique<int>(42);\n"
        + FENCE + "\n"
        + "Mutex example:\n"
        + FENCE + "cpp\n"
        + "#include <mutex>\nstd::lock_guard<std::mutex> guard(mu);\n"
        + FENCE + "\nSR02_END"
    )


def json_content() -> str:
    obj = {
        "scenario": "SR-03",
        "user": "新竹相機",
        "config": {
            "paths": [r"C:\camera\tests\ae.json", r"E:\logs\iq"],
            "quotes": 'A "quoted" value',
            "lines": "line1\nline2\tTabbed",
            "unicode": ["繁體中文", "AE/AWB", "°C"],
            "nested": [{"name": "alpha", "enabled": True}, {"name": "beta", "enabled": False}],
            "embedded_markdown": FENCE + "cpp\nint main() { return 0; }\n" + FENCE,
        },
    }
    return "JSON sample:\n" + FENCE + "json\n" + json.dumps(
        obj, ensure_ascii=False, indent=2,
    ) + "\n" + FENCE + "\nSR03_END"


def make_wire(content: str, *, safe: bool = True) -> str:
    blocks = [
        {"tool": "report_progress", "action_id": "P-SR-01",
         "total_steps": 1, "current_step": 1,
         "current_focus": "complete format test"},
        {"tool": "final_response", "action_id": "A-SR-01", "content": content},
        {"tool": "turn_commit", "action_count": 2},
    ]
    serializer = (
        _encode_canonical_tool_json if safe
        else lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    )
    return FENCE + "smartagent_tool\n" + "\n".join(
        serializer(block) for block in blocks
    ) + "\n" + FENCE


class SR02SR03FormatTest(unittest.TestCase):
    def test_sr02_cpp_markdown_roundtrip_v9(self):
        original = cpp_content()
        self.assertEqual(original.count(FENCE + "cpp"), 3)
        wire = make_wire(original)
        self.assertNotIn(FENCE + "cpp", wire)
        calls, errors = parse_v9_tool_transport(wire)
        self.assertEqual(errors, [])
        self.assertEqual(calls[1]["content"], original)
        self.assertEqual(calls[-1], {"tool": "turn_commit", "action_count": 2})
        self.assertEqual(calls[1]["content"].count(FENCE + "cpp"), 3)

    def test_sr03_nested_json_unicode_and_escapes_roundtrip_v9(self):
        original = json_content()
        self.assertIn("SR03_END", original)
        self.assertIn("繁體中文", original)
        self.assertIn(r"C:\camera\tests\ae.json", original)
        calls, errors = parse_v9_tool_transport(make_wire(original))
        self.assertEqual(errors, [])
        self.assertEqual(calls[1]["content"], original)
        embedded = calls[1]["content"]
        json_block = embedded.split(FENCE + "json\n", 1)[1].split("\n" + FENCE, 1)[0]
        restored = json.loads(json_block)
        self.assertEqual(restored["config"]["quotes"], 'A "quoted" value')
        self.assertEqual(restored["config"]["lines"], "line1\nline2\tTabbed")
        self.assertEqual(restored["config"]["embedded_markdown"].count(FENCE), 2)

    def test_both_cases_v8_and_v9_share_transport(self):
        for content in (cpp_content(), json_content()):
            with self.subTest(marker=content[-8:]):
                wire = make_wire(content)
                v8_calls, v8_errors = parse_v8_tool_transport(wire)
                v9_calls, v9_errors = parse_v9_tool_transport(wire)
                self.assertEqual(v8_errors, [])
                self.assertEqual(v9_errors, [])
                self.assertEqual(v8_calls[1]["content"], v9_calls[1]["content"])
                self.assertEqual(v8_calls[1]["content"], content)

    def test_unescaped_nested_fence_remains_fail_closed(self):
        wire = make_wire(cpp_content(), safe=False)
        calls, errors = parse_v9_tool_transport(wire)
        self.assertEqual(calls, [])
        self.assertTrue(errors, "Raw in-band markdown fence must not gain authority")

    def test_prose_outside_tool_transport_remains_rejected(self):
        wire = make_wire(cpp_content())
        calls, errors = parse_v9_tool_transport("Unauthorized prose\n" + wire)
        self.assertEqual(calls, [])
        self.assertTrue(errors)

    def test_missing_turn_commit_remains_rejected(self):
        wire = (
            FENCE + "smartagent_tool\n"
            + _encode_canonical_tool_json({
                "tool": "final_response", "action_id": "A-SR-01",
                "content": json_content(),
            })
            + "\n" + FENCE
        )
        calls, errors = parse_v9_tool_transport(wire)
        self.assertEqual(calls, [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
