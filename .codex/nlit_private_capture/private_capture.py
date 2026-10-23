"""Opt-in, private NL-IT trace observer. NOT a Planner, Runtime or mock.

Loaded via sitecustomize only in an authorized dedicated worker. Captures exact
UTF-8 bytes at the final composer boundary, the existing Web UI submission
stages, and the returned assistant response. Does not alter tool decisions,
submit behavior, retries or browser state. Trace failure never means delivery.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import os
from pathlib import Path
import time

_REQUEST = contextvars.ContextVar("nlit_trace_request", default=None)
_TURN = contextvars.ContextVar("nlit_trace_turn", default=None)
_PATCHED = set()
_TOKEN_PREFIX = "NLIT_TRACE_TOKEN="


def _hash_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _safe_root() -> Path:
    raw = os.environ.get("SMARTAGENT_NLIT_CAPTURE_ROOT", "").strip()
    if not raw or not Path(raw).is_absolute():
        raise ValueError("NLIT_CAPTURE_ROOT_MUST_BE_ABSOLUTE")
    if os.environ.get("SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED") != "YES":
        raise ValueError("NLIT_PRIVATE_ACL_NOT_CONFIRMED")
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("NLIT_CAPTURE_ROOT_NOT_DIRECTORY")
    # Require a separately ACL-protected directory OUTSIDE the running checkout;
    # this also prevents accidental inclusion in GitHub and update manifests.
    checkout = Path.cwd().resolve()
    try:
        if os.path.commonpath((str(root), str(checkout))) == str(checkout):
            raise ValueError("NLIT_CAPTURE_ROOT_INSIDE_CHECKOUT")
    except ValueError as exc:
        if str(exc) == "NLIT_CAPTURE_ROOT_INSIDE_CHECKOUT":
            raise
        # On Windows, paths on different drives raise ValueError: different
        # drives are isolated for this check.
    return root


def _identity_dir(root: Path, request: dict) -> Path:
    # Directory names are SHA-derived, not supplied user strings or local paths.
    name = _hash_bytes((request["request_id"] + "|" + request["task_id"] +
                        "|" + request["task_epoch"]).encode())[:24]
    folder = root / ("request-" + name)
    folder.mkdir(mode=0o700, parents=False, exist_ok=True)
    return folder


def _event(turn: dict, kind: str, **details) -> None:
    try:
        folder = turn["folder"]
        payload = {
            "schema": "SMARTAGENT_NLIT_PRIVATE_TRACE_EVENT_V1",
            "kind": kind,
            "time_ns": time.time_ns(),
            "request_id": turn["request_id"],
            "task_id": turn["task_id"],
            "task_epoch": turn["task_epoch"],
            "round": turn["round"],
            "result_id": turn["result_id"],
            **details,
        }
        with (folder / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        # Capture is observability only. It may not interfere with the actual
        # Planner/Runtime; collector failures make acceptance NOT_OBSERVED.
        turn["capture_error"] = True


def _bytes(turn: dict, filename: str, text: str) -> None:
    try:
        blob = text.encode("utf-8", errors="strict")
        path = turn["folder"] / filename
        # Never overwrite another round, request or previously captured result.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        _event(turn, filename + "_CAPTURED",
               sha256=_hash_bytes(blob), bytes=len(blob),
               scope="PRIVATE_LOCAL_ONLY")
    except Exception:
        turn["capture_error"] = True
        _event(turn, filename + "_CAPTURE_FAILED", reason="PRIVATE_CAPTURE_IO_ERROR")


def _enabled_request(text: str, kw: dict) -> dict | None:
    token = os.environ.get("SMARTAGENT_NLIT_CAPTURE_NONCE", "").strip()
    if len(token) < 32 or not all(c in "0123456789abcdefABCDEF" for c in token):
        return None
    if not str(text).startswith(_TOKEN_PREFIX + token + "\n"):
        return None
    request_id = str(kw.get("request_id", "") or "")
    task_id = str(kw.get("task_id", "") or "")
    epoch = str(kw.get("task_epoch", "") or "")
    if not request_id or not task_id or not epoch:
        return None
    try:
        root = _safe_root()
    except Exception:
        return None
    return {"root": root, "request_id": request_id,
            "task_id": task_id, "task_epoch": epoch}


def patch_protocol_loop(cls) -> bool:
    key = ("protocol", cls)
    if key in _PATCHED:
        return True
    original = cls._run_impl

    @functools.wraps(original)
    def observed(self, request, *args, **kwargs):
        request_identity = _enabled_request(str(request), kwargs)
        if request_identity is None:
            return original(self, request, *args, **kwargs)
        marker = _REQUEST.set(request_identity)
        try:
            return original(self, request, *args, **kwargs)
        finally:
            _REQUEST.reset(marker)

    cls._run_impl = observed
    _PATCHED.add(key)
    return True


def patch_browser_client(cls) -> bool:
    key = ("browser_client", cls)
    if key in _PATCHED:
        return True
    original = cls.ask

    @functools.wraps(original)
    def observed(self, prompt, *args, **kwargs):
        request = _REQUEST.get()
        expected = kwargs.get("protocol_expected")
        if (request is None or not isinstance(expected, dict)
            or str(expected.get("run_id", "")) != request["request_id"]
            or str(expected.get("task_id", "")) != request["task_id"]
            or str(expected.get("task_epoch", "")) != request["task_epoch"]):
            return original(self, prompt, *args, **kwargs)
        try:
            number = int(expected.get("turn_id"))
            if number < 1:
                return original(self, prompt, *args, **kwargs)
            folder = _identity_dir(request["root"], request) / ("round-%04d" % number)
            folder.mkdir(mode=0o700, exist_ok=True)
        except Exception:
            # Never replace a real Planner action with a trace configuration error.
            return original(self, prompt, *args, **kwargs)
        turn = {**request, "folder": folder, "round": number,
                "result_id": str(expected.get("ack_result_id", "") or ""),
                "capture_error": False, "user_turn_confirmed": False}
        token = _TURN.set(turn)
        _event(turn, "PLANNER_TRANSPORT_ENTERED", stage=str(kwargs.get("stage", "")))
        try:
            response = original(self, prompt, *args, **kwargs)
            _bytes(turn, "assistant_utf8.bin", str(response))
            _event(turn, "MODEL_RESPONSE_RETURNED",
                   user_turn_confirmed=turn["user_turn_confirmed"],
                   capture_complete=not turn["capture_error"])
            return response
        except BaseException as exc:
            _event(turn, "TRANSPORT_FAILED", error_type=type(exc).__name__)
            raise
        finally:
            _TURN.reset(token)

    cls.ask = observed
    _PATCHED.add(key)
    return True


def patch_scraper(cls) -> bool:
    key = ("scraper", cls)
    if key in _PATCHED:
        return True
    original_write = cls._write_prompt_to_composer
    original_stage = cls._log_stage

    @functools.wraps(original_write)
    def observed_write(self, prompt, *args, **kwargs):
        turn = _TURN.get()
        if turn is not None:
            # This is the final, fully transformed transport prompt passed to
            # the Composer, NOT a reconstruction from the tool-result ledger.
            _bytes(turn, "composer_input_utf8.bin", str(prompt))
            _event(turn, "PREPARED_NOT_SUBMITTED")
        result = original_write(self, prompt, *args, **kwargs)
        if turn is not None:
            _event(turn, "COMPOSER_WRITER_VERIFIED")
        return result

    @functools.wraps(original_stage)
    def observed_stage(self, stage, detail="", *args, **kwargs):
        ret = original_stage(self, stage, detail, *args, **kwargs)
        turn = _TURN.get()
        if turn is not None:
            if stage == "submit_returned":
                _event(turn, "SUBMIT_CLICK_RETURNED_NOT_DELIVERY_PROOF")
            elif stage == "user_sent":
                turn["user_turn_confirmed"] = True
                _event(turn, "USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER")
            elif stage == "send_ready":
                _event(turn, "SEND_READY_VERIFIED")
        return ret

    cls._write_prompt_to_composer = observed_write
    cls._log_stage = observed_stage
    _PATCHED.add(key)
    return True


def patch_available_modules() -> bool:
    """Called by the lazy import hook; never imports product modules itself."""
    import sys
    for module_name, class_name, patch in (
        ("WebAgent.protocol_loop", "WebAgentProtocolLoop", patch_protocol_loop),
        ("WebAgent.browser_client", "WebAgentBrowserClient", patch_browser_client),
        ("agent_core.web_runtime", "WebLLMScraper", patch_scraper),
    ):
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        if cls is not None:
            patch(cls)
    return all(
        any(key[0] == family for key in _PATCHED)
        for family in ("protocol", "browser_client", "scraper")
    )
