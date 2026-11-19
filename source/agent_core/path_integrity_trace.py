"""Opt-in, local-only IT10 diagnostics. Never repairs input or grants scope.

Recording needs an explicit test marker, an existing private output directory,
an ACL acknowledgement and a fresh run ID. Ordinary traffic is never recorded.
Diagnostics are best effort and cannot replace a Runtime result or receipt.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

_scope = contextvars.ContextVar("it10_path_trace", default=None)
_lock = threading.Lock()
_telegram_http_pending = threading.local()
_MAX_RAW_BYTES = 2 * 1024 * 1024
_RAW_STAGES = frozenset({
    "01_sender_input", "02_telegram_message_text", "03_receiver_normalized",
    "06a_worker_protocol_manifest_result", "06b_worker_loaded_source_attestation",
    "06c_worker_writer_attestation_gate",
    "04_gateway_input", "05_task_store", "06_worker_input", "07_loop_input",
    "08_user_request_block", "08_minimal_prompt", "09_composer_input", "09_composer_primary",
    "09_composer_semantic", "09_composer_dom_textContent",
    "09_composer_dom_innerHTML", "10_submitted_innerText", "10_submitted_textContent",
    "10_submitted_innerHTML",
    "10d_provider_response_return",
    "10c_answer_content_innerHTML", "10c_answer_content_textContent",
    "10c_answer_content_innerText", "10c_answer_adapter_value",
    "11_planner_response", "11_parser_input",
    "11_minimal_planner_response", "11_minimal_parser_input",
    "11_minimal_json_candidate_raw", "11_minimal_candidate_before_json_decode",
    "11_minimal_json_decoded", "11_minimal_path_integrity_rejected",
    "12_minimal_decision", "12_minimal_parse_error",
    "13_minimal_admission_input", "13_minimal_admission_output",
    "13_minimal_pre_dispatch_action", "13_minimal_broker_gate",
    "15_minimal_tool_result_raw", "15_minimal_tool_result_decoded",
    "12_request_context",
    "13_field_refs_expanded",
})


def _settings():
    try:
        if (os.environ.get("SMARTAGENT_IT10_PATH_TRACE") != "1"
                or os.environ.get("SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED") != "YES"):
            return None
        run = os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RUN_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", run):
            return None
        root = Path(os.environ.get("SMARTAGENT_IT10_PATH_TRACE_ROOT", ""))
        if not root.is_absolute() or not root.is_dir():
            return None
        root = root.resolve()
        package = Path(__file__).resolve().parents[2]
        if root.is_relative_to(package) and root.relative_to(package).parts[0] != "localdata":
            return None
        return root, run
    except (OSError, ValueError, IndexError, RuntimeError):
        return None

def _trace_id(run_id: str, request: str, *, request_id: str = "") -> str:
    """Separate identical request text by its trusted transport Request ID.

    An unbound local-only diagnostic caller retains the legacy text-only ID.
    Real Telegram binds its deterministic Request ID before recording 01b/02/03.
    The filename contains only a hash prefix, never the source text or ID.
    """
    payload = str(request or "").strip().encode("utf-8")
    identity = str(request_id or "").strip()
    if identity:
        payload = b"IT10_REQUEST_SCOPED_V2\x00" + identity.encode("utf-8") + b"\x00" + payload
    return run_id + "-" + hashlib.sha256(payload).hexdigest()[:24]


def begin(request, *, request_id="", task_id="", transport=""):
    """Activate only an explicitly marked diagnostic request; reset on others."""
    try:
        settings = _settings()
        if not settings:
            _scope.set(None)
            return
        text = str(request or "")
        marked = (re.search(r"(?m)^NLIT_CASE_ID=IT10[ \t\r]*$", text)
                  and re.search(r"(?m)^NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E[ \t\r]*$", text))
        if not settings or not marked:
            _scope.set(None)
            return
        subcase = re.search(r"(?m)^IT10_SUBCASE_ID=([A-Za-z0-9_-]{8,120})[ \t\r]*$", text)
        trace_id = _trace_id(settings[1], text, request_id=request_id)
        previous = _scope.get() or {}
        if previous.get("trace_id") != trace_id:
            previous = {}
        _scope.set({"trace_id": trace_id, "request_id": str(request_id or previous.get("request_id", "")),
                    "task_id": str(task_id or previous.get("task_id", "")),
                    "transport": str(transport or previous.get("transport", "")),
                    "subcase_id": str(subcase.group(1) if subcase else previous.get("subcase_id", ""))})
    except Exception:
        _scope.set(None)


def context():
    return dict(_scope.get() or {}) if _settings() else {}


def restore(value):
    """Carry diagnostic identity into our owned Composer helper, not authority."""
    try:
        settings = _settings()
        if (not settings or not isinstance(value, dict)
                or not re.fullmatch(re.escape(settings[1]) + r"-[a-f0-9]{24}", str(value.get("trace_id", "")))):
            _scope.set(None)
            return
        _scope.set({key: str(value.get(key, "")) for key in ("trace_id", "request_id", "task_id", "transport", "subcase_id")})
    except Exception:
        _scope.set(None)



def is_it10_request(request):
    """The marker is a diagnostic selector, never a scope grant."""
    text = str(request or "")
    return bool(re.search(r"(?m)^NLIT_CASE_ID=IT10[ \t\r]*$", text)
                and re.search(r"(?m)^NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E[ \t\r]*$", text))


def diagnostic_status(request):
    """Reason-only health check. Never logs trace root, credentials or request."""
    if not is_it10_request(request):
        return "NOT_IT10"
    if os.environ.get("SMARTAGENT_IT10_PATH_TRACE") != "1":
        return "FLAG_DISABLED"
    if os.environ.get("SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED") != "YES":
        return "ACL_NOT_ACKNOWLEDGED"
    run = os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RUN_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", run):
        return "INVALID_RUN_ID"
    return "READY" if _settings() else "INVALID_OR_MISSING_PRIVATE_ROOT"


def export_handoff():
    """Carry the trusted LOCAL_TEST sender opt-in through the durable task store.

    This is a diagnostic capability only. It never authorizes a Runtime path.
    No request body, token, or browser identity is included.
    """
    active = context()
    settings = _settings()
    if not active or not settings:
        return {}
    return {
        "schema": "IT10_TRACE_HANDOFF_V1",
        "trace_id": active["trace_id"],
        "run_id": settings[1],
        "private_root": str(settings[0]),
        "issued_at": time.time(),
    }


def worker_env_from_handoff(task):
    """Verify a locally issued diagnostic handoff before enabling a child process."""
    metadata = getattr(task, "metadata", {}) or {}
    if str(metadata.get("transport", "")).upper() != "LOCAL_TEST":
        return {}
    request = str(getattr(task, "request", "") or "")
    if not is_it10_request(request):
        return {}
    handoff = metadata.get("it10_trace_handoff")
    if not isinstance(handoff, dict) or handoff.get("schema") != "IT10_TRACE_HANDOFF_V1":
        return {}
    run_id = str(handoff.get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", run_id):
        return {}
    expected_trace_id = _trace_id(
        run_id, request, request_id=str(getattr(task, "request_id", "") or "")
    )
    if handoff.get("trace_id") != expected_trace_id:
        return {}
    issued_at = handoff.get("issued_at")
    if not isinstance(issued_at, (float, int)) or not 0 <= time.time() - issued_at <= 1800:
        return {}
    try:
        root = Path(str(handoff.get("private_root") or ""))
        if not root.is_absolute() or not root.is_dir():
            return {}
        root = root.resolve()
        package = Path(__file__).resolve().parents[2]
        if root.is_relative_to(package) and root.relative_to(package).parts[0] != "localdata":
            return {}
    except (OSError, ValueError, IndexError, RuntimeError):
        return {}
    return {
        "SMARTAGENT_IT10_PATH_TRACE": "1",
        "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
        "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": run_id,
        "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(root),
    }




def _raw_enabled():
    """Raw blobs are an additional explicit opt-in, never default telemetry."""
    return bool(_settings() and os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RAW") == "YES")


def raw_capture_enabled():
    """A caller may take additional read-only observations only for marked IT10."""
    return bool(context() and _raw_enabled())


def _write_raw_blob(payload: bytes, active: dict) -> dict:
    """Write original bytes verbatim. The pointer is relative to the IT10 root."""
    if not _raw_enabled():
        return {"raw_blob_status": "DISABLED"}
    if len(payload) > _MAX_RAW_BYTES:
        return {"raw_blob_status": "OVERSIZED", "raw_blob_byte_length": len(payload)}
    if not payload:
        return {"raw_blob_status": "EMPTY"}
    try:
        root, _ = _settings()
        name = "it10-raw-" + active["trace_id"] + "-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".bin"
        # Unique names + exclusive create avoid rewriting previous evidence.
        with (root / name).open("xb") as sink:
            sink.write(payload)
        return {
            "raw_blob_status": "PERSISTED",
            "raw_blob_ref": name,
            "raw_blob_sha256": hashlib.sha256(payload).hexdigest(),
            "raw_blob_byte_length": len(payload),
        }
    except (OSError, ValueError, KeyError):
        return {"raw_blob_status": "WRITE_FAILED"}


def record_bytes(stage: str, original: bytes, *, origin: str, extra=None):
    """Retain exact boundary bytes, not a decoded/re-encoded reconstruction."""
    try:
        active, settings = _scope.get(), _settings()
        if not active or not settings or not _raw_enabled() or not isinstance(original, bytes):
            return
        if origin not in {"TELEGRAM_GETUPDATES_HTTP_RESPONSE_BODY",
                          "REQUEST_CONTEXT_FILE_BYTES"}:
            return
        frame = sys._getframe(1)
        row = {
            "schema": "SMARTAGENT_IT10_PATH_TRACE_V1", **active,
            "event_id": uuid.uuid4().hex, "pid": os.getpid(),
            "stage": str(stage), "timestamp": time.time(),
            "evidence_status": "ORIGINAL_BYTES",
            "raw_origin": origin,
            "source_file": frame.f_code.co_filename,
            "source_function": frame.f_code.co_name,
            **_write_raw_blob(original, active),
        }
        if extra:
            row["extra"] = {k: extra[k] for k in ("update_id", "message_id") if k in extra}
        target = settings[0] / f"it10-path-{active['trace_id']}-{os.getpid()}.jsonl"
        with _lock, target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        return


def note_telegram_http_body(raw: bytes):
    """Keep the exact urllib HTTP response body until authorized normalization."""
    _telegram_http_pending.body = None
    if _raw_enabled() and isinstance(raw, bytes) and 0 < len(raw) <= _MAX_RAW_BYTES:
        _telegram_http_pending.body = raw


def capture_telegram_http_body(update_id: int, text: str):
    """Commit only a single authorized, marked update; never a mixed update batch."""
    raw = getattr(_telegram_http_pending, "body", None)
    _telegram_http_pending.body = None
    if not raw or not context() or not is_it10_request(text):
        return
    try:
        document = json.loads(raw.decode("utf-8"))
        updates = document.get("result") if isinstance(document, dict) else None
        if not isinstance(updates, list) or len(updates) != 1:
            return
        item = updates[0]
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict) or int(item.get("update_id", -1)) != int(update_id):
            return
        if (message.get("text") or message.get("caption") or "") != text:
            return
        record_bytes("01b_telegram_http_body", raw,
                     origin="TELEGRAM_GETUPDATES_HTTP_RESPONSE_BODY",
                     extra={"update_id": int(update_id), "message_id": message.get("message_id")})
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return


def _paths(value):
    found = []
    def visit(item):
        if isinstance(item, str):
            candidate = item.removeprefix("[TOOL_RESULT] ").lstrip()
            if candidate.startswith(("{", "[")):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, (dict, list)):
                        visit(parsed)
                except ValueError:
                    pass
            for line in item.splitlines():
                # Preserve the literal line; never Markdown-unescape or infer a root.
                if re.match(r"^[A-Za-z]:[\\/]", line):
                    found.append(line)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in {"path", "paths", "requested_path", "resolved_path", "project_root", "workspace_root"}:
                    for path in child if isinstance(child, list) else [child]:
                        if isinstance(path, str):
                            found.append(path)
                elif key == "value" and item.get("field") in {"path", "paths", "project_root"}:
                    for path in child if isinstance(child, list) else [child]:
                        if isinstance(path, str):
                            found.append(path)
                else:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
    visit(value)
    return list(dict.fromkeys(found))[:32]


def record(stage, value, *, evidence_status="RAW_INPUT", paths=None, extra=None):
    """Hash the exact value; persist only path literals and bounded test input."""
    try:
        settings = _settings()
        active = _scope.get()
        if not settings or not active:
            return
        if isinstance(value, dict):
            event = value.get("event") if isinstance(value.get("event"), dict) else value
            for key in ("request_id", "task_id"):
                if event.get(key) and active.get(key) and str(event[key]) != active[key]:
                    return  # Delivery retries may belong to another request.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        raw = text.encode("utf-8")
        normalized = re.sub(r"\s+", " ", text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")).strip()
        literals = _paths(value) if paths is None else list(dict.fromkeys(paths))[:32]
        frame = sys._getframe(1)
        row = {"schema": "SMARTAGENT_IT10_PATH_TRACE_V1", **active,
               "event_id": uuid.uuid4().hex, "pid": os.getpid(), "stage": stage,
               "timestamp": time.time(), "evidence_status": evidence_status,
               "raw_utf8_sha256": hashlib.sha256(raw).hexdigest(),
               "raw_utf8_byte_length": len(raw), "unicode_codepoint_length": len(text),
               "normalized_comparison_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
               "source_file": frame.f_code.co_filename, "source_function": frame.f_code.co_name,
               "path_values": [{"value": p, "path_value_sha256": hashlib.sha256(p.encode("utf-8")).hexdigest(),
                                "path_codepoints": [f"U+{ord(c):04X}" for c in p]} for p in literals if isinstance(p, str)]}
        if stage in _RAW_STAGES and _raw_enabled():
            row.update(_write_raw_blob(raw, active))
            row["raw_origin"] = ("UTF8_FROM_PYTHON_STR" if isinstance(value, str)
                                 else "JSON_SERIALIZED_VALUE_UTF8")
        row["path_value_sha256"] = row["path_values"][0]["path_value_sha256"] if len(row["path_values"]) == 1 else None
        row["path_codepoints"] = row["path_values"][0]["path_codepoints"] if len(row["path_values"]) == 1 else []
        # Only the explicitly opted-in request fragment, never general chat,
        # environment snapshots, full Planner answers or injected toolkits.
        if stage in {"01_sender_input", "02_telegram_message_text", "03_receiver_normalized", "04_gateway_input",
                     "05_task_store", "06_worker_input", "07_loop_input", "08_user_request_block"}:
            row["test_input_fragment"] = text[:8000]
            row["fragment_truncated"] = len(text) > 8000
        if extra:
            # Callers supply only selected Update identity/entities, never tokens.
            row["extra"] = {k: extra[k] for k in ("update_id", "message_id", "entities", "caption_entities", "action_id", "round", "candidate_index", "chunk_start", "chunk_end", "field") if k in extra}
            for key in ("entities", "caption_entities"):
                if key in row["extra"]:
                    entities = row["extra"][key]
                    row["extra"][key] = [
                        {k: entity[k] for k in ("type", "offset", "length") if k in entity}
                        for entity in entities[:128] if isinstance(entity, dict)
                    ] if isinstance(entities, list) else []
        target = settings[0] / f"it10-path-{active['trace_id']}-{os.getpid()}.jsonl"
        with _lock, target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        # No diagnostic failure may change admission, execution or delivery.
        return

def record_scoped_submitted_user_dom(scope, adapter):
    """Read only the confirmed request-bound submitted USER content.

    The existing RequestScope reconciles the actual fresh User Turn. Require
    its per-submit WUS nonce in both the bound turn and pinned content node;
    never scan or copy unrelated history. Observations are diagnostic only.
    """
    if not raw_capture_enabled():
        return "NOT_ARMED"
    active = context()
    if not active or not active.get("request_id"):
        return "NO_REQUEST_ID"
    try:
        prompt = str(getattr(scope, "prompt_text", "") or "")
        if not ("NLIT_CASE_ID=IT10" in prompt and
                "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E" in prompt):
            return "REQUEST_MARKERS_NOT_IN_PROMPT"
        subcase_id = str(active.get("subcase_id") or "")
        if not subcase_id or ("IT10_SUBCASE_ID=" + subcase_id) not in prompt:
            return "SUBCASE_NOT_IN_PROMPT"
        all_markers = tuple(getattr(scope, "request_markers", ()) or ())
        markers = tuple(s for s in all_markers
                        if isinstance(s, str) and re.fullmatch(r"WUS-[A-F0-9]{32}", s))
        if len(markers) != 1 or len(all_markers) != 1:
            return "SUBMIT_NONCE_NOT_UNIQUE"
        marker = markers[0]
        bound = getattr(scope, "user_turn", None)
        if bound is None:
            return "USER_TURN_UNBOUND"
        if marker not in str(getattr(bound, "raw_text", "") or ""):
            return "BOUND_NONCE_NOT_VISIBLE"
        root = getattr(bound, "element", None)
        if root is None:
            return "BOUND_ELEMENT_MISSING"
        selector = str(getattr(getattr(adapter, "profile", None),
                               "user_content_selector", "") or "")
        if not selector:
            return "PROFILE_CONTENT_SELECTOR_MISSING"
        candidates = [root] if selector == ":scope" else list(root.query_selector_all(selector))
        matched = []
        for element in candidates:
            pair = element.evaluate("el => ({innerText:el.innerText||'',textContent:el.textContent||''})")
            if (not isinstance(pair, dict) or
                    not isinstance(pair.get("innerText"), str) or
                    not isinstance(pair.get("textContent"), str)):
                continue
            if marker in pair["innerText"] or marker in pair["textContent"]:
                matched.append((element, pair))
        if len(matched) != 1:
            return "CONTENT_NOT_FOUND" if not matched else "CONTENT_AMBIGUOUS"
        element, pair = matched[0]
        # Do not canonicalize Windows paths or conflate native DOM properties.
        record("10_submitted_innerText", pair["innerText"], evidence_status="RENDERED_DOM")
        record("10_submitted_textContent", pair["textContent"], evidence_status="RENDERED_DOM")
        html = element.evaluate("el => el.innerHTML || ''")
        if isinstance(html, str):
            record("10_submitted_innerHTML", html, evidence_status="DOM_SERIALIZATION")
        return "CAPTURED"
    except Exception:
        return "DOM_EVALUATION_FAILED"


def record_user_dom(element, semantic_text):
    """Observe only this request's marked user turn; never historical traffic."""
    active = context()
    if not active or "NLIT_CASE_ID=IT10" not in semantic_text or "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E" not in semantic_text:
        return
    # Request IDs are not necessarily present in the submitted ChatGPT turn.
    # Match the unique sender-supplied subcase, not a previous IT10 message.
    subcase = str(active.get("subcase_id") or "")
    if subcase:
        if not re.search(r"(?m)^IT10_SUBCASE_ID=" + re.escape(subcase) + r"[ \t\r]*$", semantic_text):
            return
    elif not (active.get("request_id") and active["request_id"] in semantic_text):
        return
    try:
        pair = element.evaluate("el => ({innerText:el.innerText||'',textContent:el.textContent||''})")
        record("10_submitted_innerText", pair["innerText"], evidence_status="RENDERED_DOM")
        record("10_submitted_textContent", pair["textContent"], evidence_status="RENDERED_DOM")
        if raw_capture_enabled():
            # Read the matched USER turn, not arbitrary conversation DOM.
            html = element.evaluate("el => el.innerHTML || ''")
            record("10_submitted_innerHTML", str(html), evidence_status="DOM_SERIALIZATION")
    except Exception:
        return
