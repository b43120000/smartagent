"""Continuous Composer writer regression: complete-before-send, no partial replay.

All tests are offline. No real browser, CDP connection, or Telegram request.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_core import composer_operation as co


class FakeComposer:
    def __init__(self, *, truncate=False, change_page_after_first=False):
        self.text = ""
        self.truncate = truncate
        self.change_page_after_first = change_page_after_first
        self.key_events = []
        self.reads = []
        self.logs = []
        self._last_composer_validation = None
        self._page = SimpleNamespace(url="https://chatgpt.com/c/isolated")
        self._page.keyboard = SimpleNamespace(
            insert_text=self.insert_text,
            press=self.press,
        )

    def insert_text(self, value):
        self.key_events.append(("write", value))
        if self.truncate:
            value = value[:max(0, len(value) - 1)]
        self.text += value
        if self.change_page_after_first:
            self._page.url = "https://chatgpt.com/c/another"

    def press(self, key):
        self.key_events.append(("key", key))
        if key == "Shift+Enter":
            self.text += "\n"
        else:
            assert key == "Control+End"

    def _composer_matches_prompt(self, expected):
        self.reads.append(expected)
        return self.text == expected

    def _read_composer_text_candidates(self):
        return [self.text]

    def _normalize_composer_text(self, value):
        return value.replace("\r\n", "\n").replace("\r", "\n")

    def _focus_composer_dom(self):
        return SimpleNamespace(evaluate=lambda _: None)

    def _check_cancel_requested(self, stage):
        assert stage == "composer_chunk"

    def _log_stage(self, stage, detail):
        self.logs.append((stage, detail))


def _run(fake, prompt, journal):
    with patch.object(co.PIT, "record", return_value=None):
        co._write_continuous_prompt(
            fake, prompt, {"url": "https://chatgpt.com/c/isolated"}, journal
        )


def test_long_multiline_writer_validates_once_at_end(tmp_path):
    prompt = (
        "[WEBUI_SUBMIT_ID]\nWUS-" + "A" * 32
        + "\n[/WEBUI_SUBMIT_ID]\n"
        + ("相機 HDR 校正; file=goal.exe; check=CXX20_OK:42\n\n" * 400)
    )
    fake = FakeComposer()
    journal = tmp_path / "writer.json"
    with patch.object(co.PIT, "record", return_value=None):
        record = {"url": "https://chatgpt.com/c/isolated"}
        co._write_continuous_prompt(fake, prompt, record, journal)
    assert fake.text == prompt
    assert record["confirmed_prefix"]["chars"] == len(prompt)
    assert record["confirmed_prefix"]["sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert record["write_progress_chars"] == len(prompt)
    assert len(fake.reads) <= 5, "no full-history DOM readback on each batch"
    assert len([x for x in fake.key_events if x == ("key", "Control+End")]) == 1
    assert not any(x == ("key", "Enter") for x in fake.key_events)
    assert any(x == ("key", "Shift+Enter") for x in fake.key_events)
    assert len([x for x in fake.logs if x[0] == "composer_final_readback_verified"]) == 1
    assert not any(x[0] == "composer_chunk_verified" for x in fake.logs)
    saved = journal.read_text(encoding="utf-8")
    assert "相機 HDR 校正" not in saved and "WUS-" not in saved


def test_partial_final_readback_refuses_and_never_replays(tmp_path):
    fake = FakeComposer(truncate=True)
    prompt = "HEADER-" + "A" * 4500
    record = {"url": "https://chatgpt.com/c/isolated"}
    with patch.object(co.PIT, "record", return_value=None):
        with pytest.raises(co.ComposerOperationError, match="composer_chunk_readback_failed"):
            co._write_continuous_prompt(fake, prompt, record, tmp_path / "writer.json")
    assert len([x for x in fake.key_events if x[0] == "write"]) == 5
    assert len(fake.reads) <= 5
    assert record["write_progress_chars"] == len(prompt)
    assert "confirmed_prefix" not in record
    assert record["readback_failure"]["stage"] == "final_full_content"
    assert record["readback_failure"]["observed_length"] < len(prompt)
    assert "composer_final_readback_failed" in [x[0] for x in fake.logs]


def test_page_identity_change_stops_before_another_batch(tmp_path):
    fake = FakeComposer(change_page_after_first=True)
    record = {"url": "https://chatgpt.com/c/isolated"}
    with patch.object(co.PIT, "record", return_value=None):
        with pytest.raises(co.ComposerOperationError, match="composer_operation_identity_changed"):
            co._write_continuous_prompt(fake, "Q" * 2400, record, tmp_path / "writer.json")
    assert len([x for x in fake.key_events if x[0] == "write"]) == 1
    assert "confirmed_prefix" not in record


def test_full_content_must_have_two_stable_readbacks(tmp_path):
    fake = FakeComposer()
    record = {"url": "https://chatgpt.com/c/isolated"}
    calls = []
    original = fake._composer_matches_prompt
    def flapping(expected):
        calls.append(1)
        return original(expected) and len(calls) >= 5
    fake._composer_matches_prompt = flapping
    with patch.object(co.PIT, "record", return_value=None):
        co._write_continuous_prompt(fake, "XYZ", record, tmp_path / "writer.json")
    assert len(calls) >= 6
    assert record["confirmed_prefix"]["chars"] == 3
