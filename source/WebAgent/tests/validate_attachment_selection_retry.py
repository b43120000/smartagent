#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline validation for evidence-gated attachment selection retry."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.web_runtime import WebLLMScraper


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="smartagent-attachment-retry-") as temp:
        root = Path(temp)
        first = root / "first.txt"
        second = root / "second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")

        scraper = WebLLMScraper.__new__(WebLLMScraper)
        selections: list[str] = []
        stages: list[tuple[str, str]] = []
        waits = 0
        scraper._emit_status = lambda *_args, **_kwargs: None
        scraper._log_stage = lambda stage, detail="": stages.append((stage, detail))
        scraper._upload_one_attachment = lambda path: selections.append(Path(path).name)

        def wait(paths, **_kwargs):
            nonlocal waits
            waits += 1
            names = [Path(path).name for path in paths]
            if waits == 2:
                scraper._last_attachment_wait_reason = "attachment_unconfirmed_timeout"
                scraper._last_attachment_evidence = {
                    "confirmed_names": [first.name],
                    "unconfirmed_names": [second.name],
                    "attachment_states": {
                        first.name: {"seen": True},
                        second.name: {
                            "seen": False,
                            "processing": False,
                            "error": False,
                            "explicit_complete": False,
                            "progress": [],
                        },
                    },
                    "state": "PROCESSING",
                    "send_ready": True,
                    "upload_ring_count": 0,
                    "unexpected_attachment_chips": [],
                }
                return False
            return True

        scraper._wait_for_attachment_ui = wait
        scraper._composer_attachment_count = lambda: 1
        scraper._upload_attachments([str(first), str(second)])

        assert selections == [first.name, second.name, second.name]
        assert sum(stage == "attachment_selection_retry_started" for stage, _ in stages) == 1

        # An active upload ring means the file may already be in-flight; retry
        # must stay disabled to avoid duplicate selection.
        scraper._last_attachment_evidence["upload_ring_count"] = 1
        assert not scraper._can_retry_missing_attachment_selection(
            [str(first), str(second)], str(second)
        )

    print("ATTACHMENT_SELECTION_RETRY_OK")


if __name__ == "__main__":
    run()
