#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression coverage for rich-link-safe composer integrity matching.

This validator is intentionally browser-free so it can run in restricted release
validation environments.  It tests the matching contract directly and also checks
that the production extractor remains scoped to HTTP/HTTPS anchor boundaries.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.web_ui.contracts import ComposerSnapshot
import agent_core.web_ui.providers.chatgpt.composer as composer_module
from agent_core.web_ui.providers.chatgpt.composer import (
    read_composer_snapshot,
    validate_composer_prompt,
)


def validate(prompt: str, candidates: tuple[str, ...]):
    primary = candidates[0] if candidates else ""
    semantic = candidates[1] if len(candidates) > 1 else primary
    return validate_composer_prompt(prompt, ComposerSnapshot(primary, semantic))


def assert_match(prompt: str, *candidates: str) -> None:
    assert validate(prompt, candidates).matched is True


def assert_mismatch(prompt: str, *candidates: str) -> None:
    assert validate(prompt, candidates).matched is False


def run() -> dict:
    # Ordinary exact content remains accepted.
    assert_match("ordinary prompt", "ordinary prompt")

    # Existing normalization behavior remains intact for ordinary whitespace.
    assert_match("ordinary prompt", " ordinary\u00a0   prompt ")

    # Ordinary substantive mutations remain rejected.
    assert_mismatch("ordinary prompt", "ordinary prompt changed")
    assert_mismatch("ordinary prompt", "ordinary")

    # Rich-link boundary whitespace: the raw DOM candidate may fail, while the
    # rich-link-safe candidate preserves the complete text and succeeds.
    assert_match(
        "中文https://example.com後續",
        "中文 https://example.com 後續",
        "中文https://example.com後續",
    )

    # Current ProseMirror can insert only the leading URL boundary space while
    # exposing no a[href] node.  The primary candidate must still round-trip.
    assert_match(
        "中文https://example.com後續",
        "中文 https://example.com後續",
    )

    # A prompt that intentionally contains spaces still matches through its
    # normal DOM candidate; the additional candidate does not replace it.
    assert_match(
        "中文 https://example.com 後續",
        "中文 https://example.com 後續",
        "中文https://example.com後續",
    )

    # URL mutations remain integrity failures even when a rich-link-safe view exists.
    assert_mismatch(
        "中文https://example.com後續",
        "中文 https://example.com/x 後續",
        "中文https://example.com/x後續",
    )

    # Surrounding text mutations remain integrity failures.
    assert_mismatch(
        "中文https://example.com後續",
        "中文改 https://example.com 後續",
        "中文改https://example.com後續",
    )

    # Reordered or truncated substantive content must not be accepted.
    assert_mismatch(
        "前段https://example.com後段",
        "後段 https://example.com 前段",
        "後段https://example.com前段",
    )
    assert_mismatch(
        "前段https://example.com後段",
        "前段 https://example.com",
        "前段https://example.com",
    )

    # Multiple links may each contribute boundary whitespace, but equality is
    # still required for the complete final candidate.
    assert_match(
        "Ahttps://a.exampleBhttp://b.exampleC",
        "A https://a.example B http://b.example C",
        "Ahttps://a.exampleBhttp://b.exampleC",
    )
    assert_match(
        "Ahttps://a.exampleBhttp://b.exampleC",
        "A https://a.exampleB http://b.exampleC",
    )
    assert_mismatch(
        "Ahttps://a.exampleBhttp://b.exampleC",
        "A https://a.example B http://b.example/changed C",
        "Ahttps://a.exampleBhttp://b.example/changedC",
    )

    # Punctuation is substantive content and must remain exact.
    assert_match(
        "(https://example.com)",
        "( https://example.com )",
        "(https://example.com)",
    )
    assert_mismatch(
        "[https://example.com]",
        "( https://example.com )",
        "(https://example.com)",
    )

    # The exception is not a general whitespace or fuzzy comparison rule.
    assert_mismatch("普通文字沒有空白", "普通文字 沒有空白")
    assert_mismatch(
        "中文https://example.com後續",
        "中文 https://example.com/changed後續",
    )
    assert_mismatch(
        "中文https://example.com後續",
        "中文 https://example.com後續缺字",
    )

    # Empty targets continue to be rejected.
    assert_mismatch("", "")

    # Production extraction stays provider-owned and deterministically inverts
    # top-level editor blocks without accepting fuzzy/substantive mutations.
    extractor_source = inspect.getsource(composer_module)
    assert "semanticText" in extractor_source
    assert "childNodes" in extractor_source
    assert "blockTags" in extractor_source
    assert "join('\\n')" in extractor_source
    assert "TEXT_NODE" in extractor_source
    # The normal composer representation must remain the original
    # innerText-with-textContent-fallback view. Raw textContent must not be added
    # as an independent ordinary candidate because that could collapse block
    # boundaries unrelated to rich-links.
    assert "const primary" in extractor_source
    assert "el.innerText" in extractor_source
    assert "el.textContent" in extractor_source
    assert "return {primary, semantic}" in extractor_source

    matcher_source = inspect.getsource(validate_composer_prompt)
    assert "== normalize_composer_text(expected)" in matcher_source
    assert "matches_auto_url_boundary_spacing" in matcher_source

    result = {
        "ordinary_exact_match": True,
        "ordinary_content_mutation_rejected": True,
        "rich_link_boundary_whitespace_accepted": True,
        "intentional_spaces_preserved_via_normal_candidate": True,
        "rich_link_url_mutation_rejected": True,
        "rich_link_surrounding_text_mutation_rejected": True,
        "reordered_content_rejected": True,
        "truncated_content_rejected": True,
        "multiple_rich_links_supported": True,
        "punctuation_integrity_preserved": True,
        "empty_prompt_rejected": True,
        "extractor_scope_contract_verified": True,
        "full_equality_contract_verified": True,
    }
    print("COMPOSER_RICH_LINK_MATCHING_OK")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
