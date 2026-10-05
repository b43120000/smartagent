"""Claude composer serialization and validation."""
from __future__ import annotations

import hashlib

from ...contracts import ComposerSnapshot, ComposerValidation, normalize_ui_text, ui_text_matches


def _read_primary(composer) -> str:
    if composer is None:
        return ""
    try:
        value = composer.evaluate("el => ('value' in el ? el.value : (el.innerText || el.textContent || ''))")
        return str(value or "")
    except Exception:
        return ""


def _read_semantic(composer) -> str:
    if composer is None:
        return ""
    try:
        value = composer.evaluate(r"""el => {
            if ('value' in el) return String(el.value || '');
            const clone = el.cloneNode(true);
            for (const br of clone.querySelectorAll('br')) br.replaceWith('\n');
            const blocks = clone.querySelectorAll('p, div');
            for (const block of blocks) {
                if (block !== clone && block.nextSibling) block.append('\n');
            }
            return clone.textContent || '';
        }""")
        return str(value or "")
    except Exception:
        return _read_primary(composer)


def read_composer_snapshot(composer) -> ComposerSnapshot:
    return ComposerSnapshot(
        primary_text=_read_primary(composer),
        semantic_text=_read_semantic(composer),
    )


def _sha(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def validate_composer_prompt(prompt: str, snapshot: ComposerSnapshot) -> ComposerValidation:
    expected = str(prompt or "")
    candidates = snapshot.candidates
    matched_view = ""
    observed = candidates[0] if candidates else ""
    for name, candidate in (("primary", snapshot.primary_text), ("semantic", snapshot.semantic_text)):
        if ui_text_matches(candidate, expected):
            matched_view = name
            observed = candidate
            break
    expected_normalized = normalize_ui_text(expected)
    observed_normalized = normalize_ui_text(observed)
    return ComposerValidation(
        matched=bool(matched_view),
        matched_view=matched_view,
        expected_length=len(expected_normalized),
        observed_length=len(observed_normalized),
        expected_sha256=_sha(expected_normalized),
        observed_sha256=_sha(observed_normalized),
    )


__all__ = ["read_composer_snapshot", "validate_composer_prompt"]
