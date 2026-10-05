#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression coverage for the explicit unsafe composer verification switch."""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import agent_core.web_runtime as runtime


def run() -> None:
    scraper = runtime.WebLLMScraper.__new__(runtime.WebLLMScraper)
    events: list[tuple[str, str]] = []
    scraper._log_stage = lambda stage, detail="": events.append((stage, detail))
    scraper._composer_matches_prompt = lambda _prompt: False

    original_path = runtime.DEBUG_CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="composer-debug-switch-") as temp:
            config_path = Path(temp) / "debug_config.json"
            runtime.DEBUG_CONFIG_PATH = config_path

            config_path.write_text(
                json.dumps({"composer_verification": {"unsafe_force_pass": False}}),
                encoding="utf-8",
            )
            assert scraper._composer_verification_passes("expected") is False
            assert events == []

            config_path.write_text(
                json.dumps({"composer_verification": {"unsafe_force_pass": True}}),
                encoding="utf-8",
            )
            assert scraper._composer_verification_passes("expected") is True
            assert events == [
                ("UNSAFE_composer_verification_forced_pass", "prompt_len=8")
            ]

            events.clear()
            config_path.write_text("not-json", encoding="utf-8")
            assert scraper._composer_verification_passes("expected") is False
            assert events == []

            config_path.unlink()
            assert scraper._composer_verification_passes("expected") is False
            assert events == []
    finally:
        runtime.DEBUG_CONFIG_PATH = original_path

    write_source = inspect.getsource(runtime.WebLLMScraper._write_prompt_to_composer)
    assert "if self._composer_matches_prompt(prompt):" in write_source
    assert write_source.count("self._composer_verification_passes(prompt)") == 2

    ready_source = inspect.getsource(runtime.WebLLMScraper._wait_for_send_ready)
    assert "not self._composer_verification_passes(prompt)" in ready_source

    print("UNSAFE_COMPOSER_VERIFICATION_BYPASS_OK")


if __name__ == "__main__":
    run()
