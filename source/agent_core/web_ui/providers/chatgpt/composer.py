"""Composer DOM inversion and full-content validation.

The browser renderer is allowed to change DOM shape, but it is not allowed to
change the software-owned prompt.  This module is the single boundary that
turns the volatile DOM back into prompt text before the send state machine
decides whether submission is safe.
"""
from __future__ import annotations

import hashlib
import re

from ...contracts import ComposerSnapshot, ComposerValidation


def canonical_composer_text(value: object) -> str:
    """Preserve content while canonicalizing browser-only line conventions."""
    return str(value or "").replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")


def normalize_composer_text(value: object) -> str:
    """Legacy visible-text comparison used as the first validation layer."""
    return re.sub(r"\s+", " ", canonical_composer_text(value)).strip()


def normalize_renderer_boundaries(value: object) -> str:
    """Normalize only the proven visual boundary inside ``blob:`` URLs."""
    return re.sub(
        r"(?<=blob:)\s+(?=https?://)",
        "",
        normalize_composer_text(value),
    )


def matches_auto_url_boundary_spacing(expected: object, observed: object) -> bool:
    """Accept only renderer-inserted ASCII space immediately before a URL.

    ChatGPT's ProseMirror may turn ``例如https://...`` into
    ``例如 https://...`` without exposing an ``a[href]`` node.  Walk both
    normalized strings in lockstep and permit that one proven insertion only;
    every URL character and all surrounding content must otherwise be exact.
    """
    target = normalize_composer_text(expected)
    actual = normalize_composer_text(observed)
    if not target:
        return False

    expected_index = 0
    observed_index = 0
    while expected_index < len(target) and observed_index < len(actual):
        if target[expected_index] == actual[observed_index]:
            expected_index += 1
            observed_index += 1
            continue

        scheme = next(
            (
                candidate
                for candidate in ("https://", "http://")
                if target.startswith(candidate, expected_index)
            ),
            "",
        )
        if (
            scheme
            and expected_index > 0
            and not target[expected_index - 1].isspace()
            and actual[observed_index] == " "
            and actual.startswith(scheme, observed_index + 1)
        ):
            observed_index += 1
            continue
        return False

    return expected_index == len(target) and observed_index == len(actual)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def read_composer_snapshot(locator) -> ComposerSnapshot:
    """Read the composer and invert its editable DOM into semantic text.

    ProseMirror represents pasted newlines as top-level paragraph blocks.  Its
    ``innerText`` may additionally expose layout whitespace around auto-links.
    Joining the semantic text of those top-level blocks with the original
    newline separator is the inverse of that deterministic rendering step.
    """
    if locator is None:
        return ComposerSnapshot(primary_text="", semantic_text="")
    try:
        value = locator.evaluate(
            r"""el => {
                if (typeof el.value === 'string') {
                    return {primary: el.value, semantic: el.value};
                }

                const primary = el.innerText || el.textContent || '';
                const ignored = node => {
                    if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
                    return node.matches(
                        '[aria-hidden="true"], [data-inline-url-icon]'
                    );
                };
                const semanticText = node => {
                    if (!node || ignored(node)) return '';
                    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
                    if (node.nodeType !== Node.ELEMENT_NODE) return '';
                    if (node.tagName === 'BR') return '\n';
                    return [...node.childNodes].map(semanticText).join('');
                };

                const blockTags = new Set([
                    'P', 'DIV', 'LI', 'PRE', 'BLOCKQUOTE',
                    'H1', 'H2', 'H3', 'H4', 'H5', 'H6'
                ]);
                const top = [...el.childNodes].filter(node => !ignored(node));
                const elementTop = top.filter(node => node.nodeType === Node.ELEMENT_NODE);
                const isBlockEditor = elementTop.length > 0 &&
                    elementTop.every(node => blockTags.has(node.tagName));

                let semantic;
                if (isBlockEditor) {
                    semantic = top.map(node => {
                        let text = semanticText(node);
                        // An empty ProseMirror paragraph is commonly <p><br></p>.
                        // The paragraph separator already represents its newline.
                        if (node.nodeType === Node.ELEMENT_NODE &&
                            blockTags.has(node.tagName) && /^\n*$/.test(text)) {
                            return '';
                        }
                        return text;
                    }).join('\n');
                } else {
                    semantic = top.map(semanticText).join('');
                }
                return {primary, semantic};
            }"""
        ) or {}
    except Exception:
        return ComposerSnapshot(primary_text="", semantic_text="")
    if not isinstance(value, dict):
        text = str(value or "")
        return ComposerSnapshot(primary_text=text, semantic_text=text)
    return ComposerSnapshot(
        primary_text=str(value.get("primary") or ""),
        semantic_text=str(value.get("semantic") or ""),
    )


def validate_composer_prompt(prompt: str, snapshot: ComposerSnapshot) -> ComposerValidation:
    """Require complete prompt equality using primary then inverse DOM views."""
    expected = canonical_composer_text(prompt)
    primary = canonical_composer_text(snapshot.primary_text)
    semantic = canonical_composer_text(snapshot.semantic_text)
    matched = False
    matched_view = ""
    observed = semantic or primary

    if expected and normalize_composer_text(primary) == normalize_composer_text(expected):
        matched = True
        matched_view = "primary"
        observed = primary
    elif expected and matches_auto_url_boundary_spacing(expected, primary):
        matched = True
        matched_view = "primary_auto_url_boundary"
        observed = primary
    elif expected and semantic == expected:
        matched = True
        matched_view = "semantic_inverse"
        observed = semantic
    elif expected and matches_auto_url_boundary_spacing(expected, semantic):
        matched = True
        matched_view = "semantic_auto_url_boundary"
        observed = semantic
    elif expected and normalize_renderer_boundaries(semantic) == normalize_renderer_boundaries(expected):
        matched = True
        matched_view = "semantic_renderer_boundary"
        observed = semantic

    return ComposerValidation(
        matched=matched,
        matched_view=matched_view,
        expected_length=len(expected),
        observed_length=len(observed),
        expected_sha256=_sha256(expected),
        observed_sha256=_sha256(observed),
    )
