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



def _turn_identity(adapter, turn) -> dict:
    """Hashed provider identity and ordinal; neither implies freshness alone."""
    if turn is None:
        return {"present": False, "ordinal": None,
                "durable_id_sha256": "", "structural_id": "",
                "content_digest": "", "durable_id_available": False}
    try:
        durable = str(adapter.durable_turn_id(turn) or "")
    except Exception:
        durable = ""
    structure = str(getattr(turn, "structural_id", "") or "")
    return {
        "present": True,
        "ordinal": getattr(turn, "ordinal", None),
        "durable_id_sha256": _digest(durable) if durable else "",
        "structural_id": structure,
        "content_digest": str(getattr(turn, "content_digest", "") or ""),
        "durable_id_available": bool(durable),
        "identity_note": "May use ordinal-derived fallback; identity alone is not freshness proof.",
    }


def _final_response_content(text: str) -> tuple[str | None, str]:
    """Passive parse of the decoded FINAL_RESPONSE, never ACK admission."""
    try:
        parsed = parse_v9_tool_transport_detailed(str(text or ""))
        if parsed.diagnostics or not parsed.calls:
            return None, "UNPARSEABLE"
        final = [x.get("content") for x in parsed.calls
                 if x.get("tool") == "final_response"]
        if len(final) == 1 and isinstance(final[0], str):
            return final[0], "FOUND"
        return None, "NO_SINGLE_FINAL_RESPONSE"
    except Exception as exc:
        return None, "PARSER_EXCEPTION_" + type(exc).__name__



def _observe_turn_roster(adapter) -> dict:
    """Read-only, bounded DOM evidence; never store URL, prompt, or message text.

    Each role is enumerated independently, so the roster is observational,
    not an atomic browser snapshot. Durable IDs may have ordinal fallbacks.
    """
    result = {
        "schema": "SR_DOM_TURN_ROSTER_V1",
        "utc": datetime.now(timezone.utc).isoformat(),
        "status": "UNAVAILABLE" if adapter is None else "OBSERVED",
        "root": "page",
        "snapshot_atomic": False,
    }
    if adapter is None:
        return result
    page = getattr(adapter, "page", None)
    url = str(getattr(page, "url", "") or "")
    result["page_url_sha256"] = _digest(url) if url else ""
    result["url_available"] = bool(url)
    result["provider"] = str(getattr(adapter, "provider_name", "") or "")[:64]
    profile = getattr(adapter, "profile", None)
    for role in ("user", "assistant"):
        selector = str(getattr(profile, role + "_turn_selector", "") or "")
        observation = {
            "selector": selector,
            "raw_dom_count": None,
            "logical_turn_count": None,
            "truncated": False,
            "turns": [],
        }
        try:
            raw_fn = getattr(adapter, "turn_elements", None)
            if callable(raw_fn):
                observation["raw_dom_count"] = len(raw_fn(role))
            turns = list(adapter.turns(role))
            observation["logical_turn_count"] = len(turns)
            observation["truncated"] = len(turns) > 128
            for turn in turns[:128]:
                try:
                    durable = str(adapter.durable_turn_id(turn) or "")
                except Exception:
                    durable = ""
                observation["turns"].append({
                    "ordinal": getattr(turn, "ordinal", None),
                    "durable_id_sha256": _digest(durable) if durable else "",
                    "structural_id": str(getattr(turn, "structural_id", "") or ""),
                    "content_digest": str(getattr(turn, "content_digest", "") or ""),
                    "stable_attribute_names": sorted({
                        str(name) for name, _ in
                        (getattr(turn, "stable_attributes", ()) or ())
                    }),
                })
        except Exception as exc:
            observation["error_type"] = type(exc).__name__
        result[role] = observation
    return result


def _roster_delta(before: dict, after: dict) -> dict:
    """Summarize hashed IDs only; never infer which provider DOM rule changed."""
    result = {"status": "OBSERVED", "identity_caveat": "ORDINAL_FALLBACK_POSSIBLE"}
    if before.get("status") != "OBSERVED" or after.get("status") != "OBSERVED":
        return {"status": "UNKNOWN", "reason": "ROSTER_UNAVAILABLE"}
    result["same_page_url_hash"] = (
        before["page_url_sha256"] == after["page_url_sha256"]
        if before.get("page_url_sha256") and after.get("page_url_sha256")
        else None
    )
    for role in ("user", "assistant"):
        prev = before.get(role) or {}
        curr = after.get(role) or {}
        old = [x["durable_id_sha256"] for x in prev.get("turns", ())
               if x.get("durable_id_sha256")]
        new = [x["durable_id_sha256"] for x in curr.get("turns", ())
               if x.get("durable_id_sha256")]
        result[role] = {
            "raw_count_before": prev.get("raw_dom_count"),
            "raw_count_after": curr.get("raw_dom_count"),
            "logical_count_before": prev.get("logical_turn_count"),
            "logical_count_after": curr.get("logical_turn_count"),
            "removed_id_hashes": [x for x in old if x not in new],
            "added_id_hashes": [x for x in new if x not in old],
            "selector_same": prev.get("selector") == curr.get("selector"),
            "complete_rosters": not (prev.get("truncated") or curr.get("truncated")),
        }
    return result


def capture_pre_submit_identity(adapter, snapshot: dict) -> dict:
    """Snapshot last assistant BEFORE submit, without copying old conversation."""
    result = {
        "status": "UNAVAILABLE", "assistant_count_before": int(
            snapshot.get("assistant_count", 0) or 0
        ), "user_count_before": int(snapshot.get("user_count", 0) or 0),
        "previous_assistant": {"present": False},
        "previous_response_sha256": "",
        "previous_final_content_sha256": "",
        "previous_final_content_state": "NOT_OBSERVED",
    }
    try:
        turns = list(adapter.turns("assistant"))
        previous = turns[-1] if turns else None
        result["previous_assistant"] = _turn_identity(adapter, previous)
        if previous is not None:
            raw = str(adapter.extract_final_text(previous) or "")
            result["previous_response_sha256"] = _digest(raw)
            content, content_state = _final_response_content(raw)
            result["previous_final_content_state"] = content_state
            if content is not None:
                result["previous_final_content_sha256"] = _digest(content)
        result["status"] = "OBSERVED"
    except Exception as exc:
        result["status"] = "OBSERVATION_ERROR_" + type(exc).__name__
    result["dom_roster_before"] = _observe_turn_roster(adapter)
    return result


def _dom_turn_text_content(assistant_turn) -> tuple[str | None, str]:
    node = getattr(assistant_turn, "element", assistant_turn)
    fn = getattr(node, "text_content", None)
    if not callable(fn):
        return None, "TEXT_CONTENT_UNAVAILABLE"
    try:
        return str(fn() or ""), "TEXT_CONTENT_OBSERVED"
    except Exception as exc:
        return None, "TEXT_CONTENT_ERROR_" + type(exc).__name__


def _write_exact_text(target: Path, text: str) -> dict:
    """No universal-newline rewriting on Windows: byte-stable UTF-8 artifact."""
    original = str(text or "").encode("utf-8", errors="replace")
    limited, truncated = _clamp_text(str(text or ""))
    written = limited.encode("utf-8", errors="replace")
    target.write_bytes(written)
    verify = target.read_bytes()
    result = {
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "stored_sha256": hashlib.sha256(verify).hexdigest(),
        "original_bytes": len(original),
        "stored_bytes": len(verify),
        "truncated": truncated,
        "write_verified": written == verify,
        "matches_original": original == verify,
        "newline_policy": "UTF8_BYTES_NO_TRANSLATION",
    }
    if not result["write_verified"] or (not truncated and not result["matches_original"]):
        raise RuntimeError("SR_BYTE_FIDELITY_WRITE_MISMATCH")
    return result


def _case_and_markers(final_content: str | None, *, expected_case: str = "") -> dict:
    if not isinstance(final_content, str):
        return {"test_case": expected_case if expected_case in {"SR-04", "SR-05"}
                else "UNCLASSIFIED", "status": "NO_FINAL_CONTENT"}
    if expected_case == "SR-04" or "SR04_" in final_content or "SR04_END" in final_content:
        required = ["SR04_BEGIN", *[f"SR04_SECTION_{i:02d}" for i in range(1, 21)],
                    "SR04_END"]
        missing = [marker for marker in required if marker not in final_content]
        positions = [final_content.find(marker) for marker in required]
        return {
            "test_case": "SR-04",
            "status": "MARKERS_PRESENT" if not missing and positions == sorted(positions)
                      else "MARKERS_INCOMPLETE_OR_ORDERED_WRONG",
            "expected_marker_count": len(required),
            "missing_markers": missing,
            "markers_in_order": not missing and positions == sorted(positions),
            "content_characters": len(final_content),
        }
    if expected_case == "SR-05" or "SR05_END" in final_content:
        return {"test_case": "SR-05",
                "status": "MARKER_PRESENT" if "SR05_END" in final_content
                          else "MARKER_MISSING",
                "content_characters": len(final_content)}
    return {"test_case": "OTHER", "status": "NOT_SR04_OR_SR05"}


def _freshness_pair(pre: dict, current: dict, current_content_sha: str) -> dict:
    prev = dict(pre.get("previous_assistant") or {})
    before = str(prev.get("durable_id_sha256", "") or "")
    after = str(current.get("durable_id_sha256", "") or "")
    prev_content_sha = str(pre.get("previous_final_content_sha256", "") or "")
    if before and after:
        identity = "SAME" if before == after else "CHANGED"
    else:
        identity = "UNKNOWN"
    same_content = (prev_content_sha == current_content_sha) if (
        prev_content_sha and current_content_sha) else None
    return {
        "previous_vs_current_assistant_identity": identity,
        "decoded_final_content_sha_equal": same_content,
        "previous_final_content_observed": bool(prev_content_sha),
        "assessment": (
            "SAME_IDENTITY_REQUIRES_REVIEW" if identity == "SAME"
            else "NEW_IDENTITY_CONTENT_REPEAT" if identity == "CHANGED" and same_content
            else "NEW_IDENTITY_CONTENT_DIFFERS" if identity == "CHANGED" and same_content is False
            else "INSUFFICIENT_EVIDENCE"
        ),
        "admission_consequence": "NONE_DIAGNOSTIC_ONLY",
    }

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
    pre_submit_identity: dict | None = None,
    snapshot_summary: dict | None = None,
    adapter=None,
    test_case_hint: str = "",
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
    raw_fidelity = _write_exact_text(target / "extracted_response.txt", body)
    dom_fidelity = (
        _write_exact_text(target / "dom_turn_inner_text.txt", dom)
        if dom is not None else None
    )
    dom_content, dom_content_status = _dom_turn_text_content(assistant_turn)
    content_fidelity = (
        _write_exact_text(target / "dom_turn_text_content.txt", dom_content)
        if dom_content is not None else None
    )
    # Byte-preserved evidence is durable even when parsing fails.
    body_truncated = bool(raw_fidelity["truncated"])
    dom_truncated = bool(dom_fidelity and dom_fidelity["truncated"])
    action_seed = request_id + ":" + round_id
    try:
        verdict = classify(body, action_seed=action_seed)
    except Exception as exc:
        verdict = {
            "verdict": "PARSER_EXCEPTION",
            "error_type": type(exc).__name__,
            "syntax_valid": False,
            "semantic_ack_admission": "NOT_RUN",
            "action_admission": "NOT_RUN",
            "actions_executed": 0,
            "repair_prompts_sent": 0,
        }
    dom_digest = _digest(dom) if dom is not None else ""
    scoped_id = str((response_source or {}).get("scope_id", "") or "")
    final_content, final_content_state = _final_response_content(body)
    final_sha = _digest(final_content) if final_content is not None else ""
    previous = dict(pre_submit_identity or {})
    if adapter is not None:
        current = _turn_identity(adapter, assistant_turn)
    else:
        current = {
            "present": assistant_turn is not None,
            "ordinal": getattr(assistant_turn, "ordinal", None),
            "durable_id_sha256": str(
                (response_source or {}).get("assistant_id_sha256", "") or ""
            ),
            "durable_id_available": bool(
                (response_source or {}).get("assistant_id_sha256", "")
            ),
        }
    current["response_sha256"] = raw_fidelity["original_sha256"]
    current["final_content_sha256"] = final_sha
    current["final_content_state"] = final_content_state
    current["previous_comparison"] = _freshness_pair(previous, current, final_sha)
    snapshot_info = dict(snapshot_summary or {})
    # No raw prompt, page URL, browser profile or request scope identifiers.
    allow_snapshot = (
        "assistant_count_before", "user_count_before",
        "assistant_count_after", "user_count_after",
        "request_user_ordinal", "request_scope_binding",
    )
    safe_snapshot = {k: snapshot_info[k] for k in allow_snapshot
                     if k in snapshot_info}
    roster_before = previous.pop("dom_roster_before", {"status": "NOT_CAPTURED"})
    roster_after = _observe_turn_roster(adapter)
    turn_observation = {
        "schema": "SR05_TURN_OBSERVATION_V1",
        "request_id": request_id,
        "round": round_id,
        "before_submit": roster_before,
        "after_completion": roster_after,
        "delta": _roster_delta(roster_before, roster_after),
        "runtime_count_summary": safe_snapshot,
        "notes": [
            "Non-atomic role enumerations, not a streaming DOM trace.",
            "Page URLs, prompt text and user/assistant message text are NOT stored.",
            "Hashed identities can be ordinal fallbacks; no admission effect.",
        ],
    }
    (target / "turn_observation.json").write_text(
        json.dumps(turn_observation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
        "byte_fidelity": {
            "extracted_response": raw_fidelity,
            "dom_turn_inner_text": dom_fidelity,
            "dom_turn_text_content": content_fidelity,
        },
        "dom_turn_text_content_status": dom_content_status,
        "final_content_sha256": final_sha,
        "final_content_state": final_content_state,
        "test_case": _case_and_markers(final_content, expected_case=test_case_hint),
        "turn_identity": {
            "before_submit": previous,
            "current_assistant": current,
            "counts_and_binding": safe_snapshot,
            "request_scope_id_sha256": _digest(scoped_id) if scoped_id else "",
        },
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
    (target / "capture.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (target / "turn_identity.json").write_text(
        json.dumps(metadata["turn_identity"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target / "dom_selected_structure.json").write_text(
        json.dumps(_selected_dom_structure(assistant_turn),
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return SRFirstReadbackCaptured(target, verdict["verdict"])
