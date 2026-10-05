#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline validation for bounded character-level composer mismatch logs."""
from __future__ import annotations

import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.web_runtime import WebLLMScraper


def run() -> None:
    scraper = WebLLMScraper.__new__(WebLLMScraper)
    records: list[tuple[str, str]] = []
    scraper._log_stage = lambda stage, detail="": records.append((stage, detail))
    scraper._read_composer_text_candidates = lambda: ["a\u200bbc!", "ac"]

    reports = scraper._log_composer_mismatch_diagnostics("abc", stage="test")
    assert len(reports) == 2
    assert len(records) == 2
    assert all(stage == "composer_mismatch_diagnostic" for stage, _detail in records)

    primary = reports[0]
    primary_kinds = [change["kind"] for change in primary["changes"]]
    assert primary_kinds == ["extra_in_composer", "extra_in_composer"]
    assert primary["expected_normalized_len"] == 3
    assert primary["composer_normalized_len"] == 5
    assert primary["changes"][0]["composer"]["escaped"] == "\\u200b"
    assert primary["changes"][0]["composer"]["codepoints"] == [
        {"value": "U+200B ZERO WIDTH SPACE", "count": 1}
    ]

    secondary = reports[1]
    assert [change["kind"] for change in secondary["changes"]] == [
        "missing_from_composer"
    ]
    assert secondary["changes"][0]["expected"]["escaped"] == "b"

    decoded = json.loads(records[0][1])
    assert decoded == primary
    assert "abc" not in records[0][1]

    print("COMPOSER_MISMATCH_DIAGNOSTICS_OK")


if __name__ == "__main__":
    run()
