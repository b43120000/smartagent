#!/usr/bin/env python3
"""SR-04/05 offline tests: UTF-8 byte fidelity, long reply, repeat identities.

No browser, network, real tool action, or model recovery is exercised.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.sr_first_readback import (
    capture_pre_submit_identity, capture_first_readback, _case_and_markers,
)
from WebAgent.tests.validate_shared_runtime_sr02_sr03 import make_wire
from WebAgent.tests.collect_sr_readback_bundle import (
    bundle, _capture_integrity_report, _sr05_pair_report,
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeNode:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text

    def text_content(self):
        return "prefix\n" + self.text + "\r\nsuffix"

    def query_selector_all(self, selector):
        return [self] if selector == "code" else []


class FakeTurn:
    def __init__(self, text, ident, ordinal):
        self.element = FakeNode(text)
        self.text = text
        self.structural_id = sha(ident)
        self.content_digest = sha(text)
        self.ordinal = ordinal


class FakeAdapter:
    def __init__(self, turns):
        self._turns = turns

    def turns(self, role):
        return self._turns if role == "assistant" else ()

    def durable_turn_id(self, turn):
        return turn.structural_id

    def extract_final_text(self, turn):
        return turn.text


def sr04():
    return "SR04_BEGIN\n" + "".join(
        f"SR04_SECTION_{i:02d}\r\n第{i:02d}節，影像演算法與驗證條件。\n" +
        ("資料必須完整，包含 Unicode、C++、JSON 與其原始換行。\n" * 12)
        for i in range(1, 21)
    ) + "SR04_END"


def snapshot(adapter, before):
    return capture_pre_submit_identity(
        adapter, {"assistant_count": len(before), "user_count": 2},
    )


def save(root, *, number, previous, current, content, request=None):
    request = request or f"WA-SR05-{number}"
    raw = make_wire(content)
    adapter = FakeAdapter(previous)
    pre = snapshot(adapter, previous)
    with patch.dict(os.environ, {
        "SMARTAGENT_SR_EVIDENCE_DIR": str(
            root / "localdata/logs/sr-first-readback"
        ),
    }):
        return capture_first_readback(
            text=raw, assistant_turn=current,
            response_source={
                "scope_id": f"scope-{request}",
                "assistant_id_sha256": sha(current.structural_id),
                "assistant_fingerprint": current.structural_id,
            },
            expected={"run_id": request, "turn_id": 1},
            ui_idle_seconds=5.0, stable_seconds=2.5,
            pre_submit_identity=pre,
            snapshot_summary={
                "assistant_count_before": len(previous),
                "user_count_before": 2,
                "assistant_count_after": len(previous) + 1,
                "user_count_after": 3,
                "request_user_ordinal": 3,
                "request_scope_binding": "BOUND",
            },
            adapter=adapter,
        )


class SR04SR05IT(unittest.TestCase):
    def test_sr04_all_markers_and_order(self):
        result = _case_and_markers(sr04())
        self.assertEqual(result["test_case"], "SR-04")
        self.assertEqual(result["status"], "MARKERS_PRESENT")
        self.assertEqual(result["missing_markers"], [])
        self.assertEqual(result["expected_marker_count"], 22)
        self.assertTrue(result["markers_in_order"])

    def test_case_hint_preserves_missing_marker_diagnostic(self):
        long = _case_and_markers("模型漏掉了全部標記", expected_case="SR-04")
        self.assertEqual(long["test_case"], "SR-04")
        self.assertEqual(len(long["missing_markers"]), 22)
        short = _case_and_markers("相同內容但缺結尾", expected_case="SR-05")
        self.assertEqual(short["test_case"], "SR-05")
        self.assertEqual(short["status"], "MARKER_MISSING")

    def test_sr04_missing_middle_is_not_a_pass(self):
        broken = sr04().replace("SR04_SECTION_11", "SECTION_LOST")
        result = _case_and_markers(broken)
        self.assertIn("SR04_SECTION_11", result["missing_markers"])
        self.assertFalse(result["markers_in_order"])

    def test_exact_lf_crlf_utf8_and_text_content(self):
        with tempfile.TemporaryDirectory(prefix="sr04-fidelity-") as tmp:
            root = Path(tmp)
            original = make_wire(sr04())
            ref = FakeTurn(original, "current-1", 2)
            result = save(
                root, number=1, previous=[], current=ref,
                content=sr04(), request="WA-SR04-1",
            )
            capture = json.loads(
                (result.capture_dir / "capture.json").read_text(encoding="utf-8")
            )
            self.assertEqual(capture["test_case"]["status"], "MARKERS_PRESENT")
            for key, filename in [
                ("extracted_response", "extracted_response.txt"),
                ("dom_turn_inner_text", "dom_turn_inner_text.txt"),
                ("dom_turn_text_content", "dom_turn_text_content.txt"),
            ]:
                evidence = (result.capture_dir / filename).read_bytes()
                f = capture["byte_fidelity"][key]
                self.assertEqual(len(evidence), f["original_bytes"])
                self.assertEqual(hashlib.sha256(evidence).hexdigest(),
                                 f["original_sha256"])
                self.assertEqual(f["stored_sha256"], f["original_sha256"])
                self.assertTrue(f["matches_original"])
                self.assertEqual(f["newline_policy"], "UTF8_BYTES_NO_TRANSLATION")
            # JSON transport escapes inner CRLF into literal backslashes;
            # the independent DOM textContent fixture deliberately contains
            # BOTH LF and CRLF and must survive byte-for-byte on Windows.
            extracted = (result.capture_dir / "extracted_response.txt").read_bytes()
            text_content = (result.capture_dir / "dom_turn_text_content.txt").read_bytes()
            self.assertIn(b"\\r\\n", extracted)
            self.assertIn(b"\n", extracted)
            self.assertIn(b"\r\n", text_content)
            self.assertIn(b"prefix\n", text_content)
            structure = json.loads((
                result.capture_dir / "dom_selected_structure.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(structure["code"]["count"], 1)

    def test_two_same_contents_have_different_identity_and_scope(self):
        with tempfile.TemporaryDirectory(prefix="sr05-pair-") as tmp:
            root = Path(tmp)
            content = "相同回覆。請勿添加其他內容。\nSR05_END"
            raw = make_wire(content)
            previous = FakeTurn(raw, "previous", 1)
            first = FakeTurn(raw, "first-assistant", 2)
            second = FakeTurn(raw, "second-assistant", 3)
            capture_a = save(root, number=1, previous=[previous],
                             current=first, content=content)
            capture_b = save(root, number=2, previous=[previous, first],
                             current=second, content=content)
            a = json.loads((capture_a.capture_dir / "capture.json").read_text())
            b = json.loads((capture_b.capture_dir / "capture.json").read_text())
            self.assertEqual(a["final_content_sha256"], b["final_content_sha256"])
            self.assertNotEqual(
                a["turn_identity"]["current_assistant"]["durable_id_sha256"],
                b["turn_identity"]["current_assistant"]["durable_id_sha256"],
            )
            self.assertEqual(
                b["turn_identity"]["current_assistant"]["previous_comparison"]["assessment"],
                "NEW_IDENTITY_CONTENT_REPEAT",
            )
            report = _sr05_pair_report(
                root / "localdata/logs/sr-first-readback/captures"
            )
            self.assertEqual(report["status"], "CONSISTENT_WITH_DISTINCT_TURNS")
            self.assertEqual(report["decoded_final_content_sha_equal"], True)
            self.assertEqual(report["assistant_id_sha_equal"], False)
            self.assertEqual(report["request_scope_sha_equal"], False)
            self.assertEqual(report["second_previous_assistant_matches_first"], True)
            self.assertTrue(report["distinct_request_ids"])
            zip_path = bundle(root)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertTrue(any(s.endswith("turn_identity.json") for s in names))
                self.assertTrue(any(s.endswith("dom_turn_text_content.txt") for s in names))
                pair = json.loads(archive.read("sr05_pair_comparison.json"))
                self.assertEqual(pair["status"], "CONSISTENT_WITH_DISTINCT_TURNS")

    def test_same_assistant_identity_is_not_distinct(self):
        with tempfile.TemporaryDirectory(prefix="sr05-stale-") as tmp:
            root = Path(tmp)
            content = "不變的文字\nSR05_END"
            raw = make_wire(content)
            turn = FakeTurn(raw, "same-assistant", 2)
            save(root, number=1, previous=[], current=turn, content=content)
            save(root, number=2, previous=[turn], current=turn, content=content)
            report = _sr05_pair_report(
                root / "localdata/logs/sr-first-readback/captures"
            )
            self.assertEqual(report["status"], "POSSIBLE_REUSED_TURN_REVIEW_REQUIRED")

    def test_bundle_rejects_tampered_captured_bytes(self):
        with tempfile.TemporaryDirectory(prefix="sr-tamper-") as tmp:
            root = Path(tmp)
            text = "繁體中文\nSR05_END"
            cap = save(root, number=1, previous=[],
                       current=FakeTurn(make_wire(text), "x", 1), content=text)
            (cap.capture_dir / "extracted_response.txt").write_bytes(b"tampered")
            integrity = _capture_integrity_report(
                root / "localdata/logs/sr-first-readback/captures"
            )
            self.assertEqual(integrity[0]["status"], "FAIL")
            with self.assertRaisesRegex(RuntimeError, "SR_BUNDLE_FIDELITY_MISMATCH"):
                bundle(root)

    def test_legacy_capture_is_not_silently_declared_byte_verified(self):
        with tempfile.TemporaryDirectory(prefix="sr-legacy-") as tmp:
            captures = Path(tmp)
            old = captures / "old"
            old.mkdir()
            (old / "capture.json").write_text('{"schema":"LEGACY"}', encoding="utf-8")
            self.assertEqual(
                _capture_integrity_report(captures)[0]["status"],
                "LEGACY_UNVERIFIED",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
