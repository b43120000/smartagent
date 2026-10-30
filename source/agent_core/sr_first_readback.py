#!/usr/bin/env python3
"""First-readback-only diagnostic gate for shared WebGPT Runtime.

Never grants execution authority, repairs a protocol, or sends a second prompt.
Enabled only by SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=1.
Only the request-owned assistant turn is captured, never a whole browser page,
browser profile, cookie, authentication state, or environment variable dump.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .protocol_v8 import parse_v8_tool_transport_detailed
from .protocol_v9 import parse_v9_tool_transport_detailed

FLAG = "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1"
EVIDENCE_ENV = "SMARTAGENT_SR_EVIDENCE_DIR"
MAX_TEXT_BYTES = 2 * 1024 * 1024


class SRFirstReadbackCaptured(RuntimeError):
    """Expected controlled stop. NEVER trigger a response or repair."""

    def __init__(self, capture_dir: Path, verdict: str):
        self.capture_dir = Path(capture_dir)
        self.verdict = str(verdict)
        super().__init__(
            "SR_FIRST_READBACK_CAPTURED "
            f"verdict={self.verdict} action_executed=false "
            "repair_prompts_sent=0"
        )


def enabled() -> bool:
    return os.environ.get(FLAG, "").strip() == "1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _clamp_text(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text, False
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), True


def _safe_name(raw: object) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or ""))[:80] or "unknown"


def _selected_dom_turn_text(assistant_turn) -> tuple[str | None, str]:
    """Independent DOM innerText view, not a second assistant selection."""
    node = getattr(assistant_turn, "element", assistant_turn)
    inner_text = getattr(node, "inner_text", None)
    if not callable(inner_text):
        return None, "NO_TURN_INNER_TEXT"
    try:
        raw = inner_text()
        return str(raw or ""), "DOM_TURN_INNER_TEXT"
    except Exception as exc:
        return None, "DOM_TURN_READ_ERROR_" + type(exc).__name__


def _selected_dom_structure(assistant_turn) -> dict:
    """Bounded, text-only structure of the selected assistant subtree.

    This captures pre/code/table independently from the text extractor,
    without dumping outerHTML, attributes, scripts or whole-page DOM.
    """
    node = getattr(assistant_turn, "element", assistant_turn)
    query = getattr(node, "query_selector_all", None)
    result: dict = {}
    if not callable(query):
        return {"status": "SELECTOR_API_UNAVAILABLE"}
    for selector, label in (("pre", "pre"), ("code", "code"), ("table", "table")):
        try:
            elements = list(query(selector))
            snippets = []
            for element in elements[:32]:
                getter = getattr(element, "inner_text", None)
                if callable(getter):
                    value, truncated = _clamp_text(str(getter() or ""))
                    snippets.append({"text": value, "truncated": truncated})
            result[label] = {
                "count": len(elements),
                "captured": len(snippets),
                "first_32_texts": snippets,
            }
        except Exception as exc:
            result[label] = {"read_error": type(exc).__name__}
    result["status"] = "SELECTED_ASSISTANT_SUBTREE_ONLY"
    return result


def classify(raw_text: str, *, action_seed: str = "") -> dict:
    """Classify original transport and all normalization without admitting it."""
    source = str(raw_text or "")
    legacy = parse_v8_tool_transport_detailed(source)
    parsed = parse_v9_tool_transport_detailed(source, action_id_seed=action_seed)
    valid = bool(parsed.calls and not parsed.diagnostics)
    calls = list(parsed.calls) if valid else []
    has_commit = bool(calls and calls[-1].get("tool") == "turn_commit")
    fenced = source.strip()
    single = bool(
        fenced.lower().startswith(chr(96) * 3 + "smartagent_tool\n")
        and fenced.endswith("\n" + chr(96) * 3)
        and fenced.count(chr(96) * 3) == 2
    )
    if not source.strip():
        verdict = "EMPTY_READBACK"
    elif not valid:
        verdict = "PARSE_FAILED"
    elif not has_commit:
        verdict = "COMMIT_MISSING"
    elif single and not parsed.normalizations and parsed.transport_kind == "fenced":
        verdict = "STRICT_SINGLE_FENCE"
    else:
        verdict = "NORMALIZED_ONLY"
    return {
        "verdict": verdict,
        "syntax_valid": valid,
        "single_fence_original": single,
        "has_turn_commit": has_commit,
        "parsed_tool_names": [str(item.get("tool", "")) for item in calls],
        "parsed_action_count": len(calls) - int(has_commit),
        "v9_transport_kind": str(parsed.transport_kind),
        "v9_normalizations": list(parsed.normalizations),
        "v9_diagnostics": list(parsed.diagnostics),
        "v8_transport_kind": str(legacy.transport_kind),
        "v8_normalizations": list(legacy.normalizations),
        "v8_diagnostics": list(legacy.diagnostics),
        "semantic_ack_admission": "NOT_RUN",
        "action_admission": "NOT_RUN",
        "actions_executed": 0,
        "repair_prompts_sent": 0,
    }


def capture_first_readback(
    *,
    text: str,
    assistant_turn,
    response_source: dict,
    expected: dict,
    ui_idle_seconds: float,
    stable_seconds: float,
) -> SRFirstReadbackCaptured:
    """Persist evidence BEFORE any normal parser recovery or tool admission."""
    root_value = os.environ.get(EVIDENCE_ENV, "").strip()
    if not root_value:
        raise RuntimeError("SR_FIRST_READBACK_EVIDENCE_DIR_UNSET")
    root = Path(root_value).expanduser().resolve()
    request_id = str((expected or {}).get("run_id", "") or "")
    round_id = str((expected or {}).get("turn_id", "") or "")
    if not request_id or not round_id:
        raise RuntimeError("SR_FIRST_READBACK_MISSING_IDENTITY")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / "captures" / (
        f"{_safe_name(request_id)}_r{_safe_name(round_id)}_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    target.mkdir(parents=True, exist_ok=False)

    body = str(text or "")
    dom, dom_state = _selected_dom_turn_text(assistant_turn)
    body_saved, body_truncated = _clamp_text(body)
    if dom is not None:
        dom_saved, dom_truncated = _clamp_text(dom)
    else:
        dom_saved, dom_truncated = None, False

    action_seed = request_id + ":" + round_id
    verdict = classify(body, action_seed=action_seed)
    dom_digest = _digest(dom) if dom is not None else ""
    scoped_id = str((response_source or {}).get("scope_id", "") or "")
    metadata = {
        "schema": "SMARTAGENT_SR_FIRST_READBACK_V1",
        "request_id": request_id,
        "round": round_id,
        "utc": stamp,
        "interface": "SHARED_WEB_RUNTIME",
        "capture_stage": "FIRST_UI_CONFIRMED_IDLE_AND_STABLE",
        "ui_idle_seconds": round(float(ui_idle_seconds), 2),
        "text_stable_seconds": round(float(stable_seconds), 2),
        "response_sha256": _digest(body),
        "response_utf8_bytes": len(body.encode("utf-8", errors="replace")),
        "response_capture_truncated": body_truncated,
        "dom_turn_source": dom_state,
        "dom_turn_sha256": dom_digest,
        "dom_turn_utf8_bytes": len(dom.encode("utf-8", errors="replace")) if dom is not None else None,
        "dom_capture_truncated": dom_truncated,
        "dom_and_extractor_equal": (dom == body) if dom is not None else None,
        "dom_comparison_note": (
            "DOM node innerText can include controls/chrome and is not expected "
            "to match adapter-extracted text byte-for-byte."
        ),
        "assistant_id_sha256": str((response_source or {}).get("assistant_id_sha256", "") or ""),
        "assistant_fingerprint": str((response_source or {}).get("assistant_fingerprint", "") or ""),
        "scope_id_sha256": _digest(scoped_id) if scoped_id else "",
        "diagnostic": verdict,
        "live_terminal_success": False,
        "expected_stop": True,
    }
    (target / "extracted_response.txt").write_text(body_saved, encoding="utf-8")
    if dom_saved is not None:
        (target / "dom_turn_inner_text.txt").write_text(dom_saved, encoding="utf-8")
    (target / "capture.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (target / "dom_selected_structure.json").write_text(
        json.dumps(_selected_dom_structure(assistant_turn),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return SRFirstReadbackCaptured(target, verdict["verdict"])
