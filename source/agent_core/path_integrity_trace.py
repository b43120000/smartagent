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
        seed = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]
        trace_id = settings[1] + "-" + seed
        previous = _scope.get() or {}
        if previous.get("trace_id") != trace_id:
            previous = {}
        _scope.set({"trace_id": trace_id, "request_id": str(request_id or previous.get("request_id", "")),
                    "task_id": str(task_id or previous.get("task_id", "")),
                    "transport": str(transport or previous.get("transport", ""))})
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
        _scope.set({key: str(value.get(key, "")) for key in ("trace_id", "request_id", "task_id", "transport")})
    except Exception:
        _scope.set(None)



def is_it10_request(request):
    """The marker is a diagnostic selector, never a scope grant."""
    text = str(request or "")
    return bool(re.search(r"(?m)^NLIT_CASE_ID=IT10[ \\t\\r]*$", text)
                and re.search(r"(?m)^NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E[ \\t\\r]*$", text))


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
    digest = hashlib.sha256(request.strip().encode("utf-8")).hexdigest()[:24]
    if handoff.get("trace_id") != run_id + "-" + digest:
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
            row["extra"] = {k: extra[k] for k in ("update_id", "message_id", "entities", "caption_entities", "action_id", "round", "chunk_start", "chunk_end", "field") if k in extra}
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


def record_user_dom(element, semantic_text):
    """Observe only this request's marked user turn; never historical traffic."""
    active = context()
    if (not active or not active.get("request_id")
            or active["request_id"] not in semantic_text
            or "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E" not in semantic_text):
        return
    try:
        pair = element.evaluate("el => ({innerText:el.innerText||'',textContent:el.textContent||''})")
        record("10_submitted_innerText", pair["innerText"], evidence_status="RENDERED_DOM")
        record("10_submitted_textContent", pair["textContent"], evidence_status="RENDERED_DOM")
    except Exception:
        return
