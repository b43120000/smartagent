#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared ChatGPT conversation watcher primitives (Stage 5).

This module is deliberately protocol-agnostic.  It only answers one question:
"which *human* conversation turns are fresh since the last accepted turn?"

It does **not** parse ``remoteagent_control`` and it does **not** execute
``smartagent_tool``.  RemoteAgent Supervisor (Stage 6+) may consume fresh turns
from here and pass their text to ``RemoteAgent.remote_protocol``.

Design goals:
- Existing conversation history is baselined on first attach; old turns never
  become new tasks merely because a watcher started.
- Refresh / React DOM replacement is stable.  Fingerprints do not depend on
  volatile DOM node ids.
- Repeated identical human messages remain distinguishable by occurrence index.
- While WebGPT generation is active, polling returns WAIT and does not advance
  ``last_seen_turn`` or expose partial content.
- Registry persistence is optional and uses ConversationRegistry's existing
  ``last_seen_turn`` field when supplied.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional

WATCH_IDLE = "IDLE"
WATCH_WAIT = "WAIT"
WATCH_BASELINED = "BASELINED"
WATCH_FRESH = "FRESH"
WATCH_DRIFT = "DRIFT"

_WS_RE = re.compile(r"\s+")


def normalize_turn_text(value: object) -> str:
    """Normalize presentation-only whitespace without changing semantics."""
    text = str(value or "").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _WS_RE.sub(" ", text).strip()


def _safe_attr(element: Any, name: str) -> str:
    try:
        return str(element.get_attribute(name) or "")
    except Exception:
        return ""


def _safe_raw_text(element: Any) -> str:
    """Return rendered turn text with line boundaries preserved for protocol parsing."""
    for getter_name in ("inner_text", "text_content"):
        try:
            getter = getattr(element, getter_name)
            value = getter()
            if value is not None:
                return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
        except Exception:
            continue
    return ""


def _safe_text(element: Any) -> str:
    return normalize_turn_text(_safe_raw_text(element))


def _turn_attachment_signature(element: Any) -> list[str]:
    """Best-effort stable attachment/media descriptors inside a human turn.

    The watcher must not depend only on text: a user can send the same caption
    with two different attachments.  At the same time, volatile blob/src URLs
    are intentionally ignored so page refresh does not create a false new turn.
    """
    values: list[str] = []
    try:
        nodes = list(element.query_selector_all("[data-testid*='file'], [data-testid*='attachment'], img, video, a"))
    except Exception:
        nodes = []
    for node in nodes:
        parts = []
        for attr in ("download", "aria-label", "title", "alt", "data-testid"):
            value = normalize_turn_text(_safe_attr(node, attr))
            if value:
                parts.append(f"{attr}={value}")
        # Link text / visible filename is usually stable; href/src often is not.
        text = _safe_text(node)
        if text:
            parts.append(f"text={text}")
        if parts:
            values.append("|".join(parts))
    return sorted(set(values))


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str
    fingerprint: str
    ordinal: int
    same_content_occurrence: int
    attachments: tuple[str, ...] = ()
    raw_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attachments"] = list(self.attachments)
        return data


@dataclass(frozen=True)
class WatchPollResult:
    status: str
    fresh_turns: tuple[ConversationTurn, ...] = ()
    latest_fingerprint: str = ""
    detail: str = ""

    @property
    def has_fresh_turns(self) -> bool:
        return bool(self.fresh_turns)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fresh_turns": [turn.as_dict() for turn in self.fresh_turns],
            "latest_fingerprint": self.latest_fingerprint,
            "detail": self.detail,
        }


def fingerprint_turn(
    *,
    role: str,
    text: str,
    same_content_occurrence: int = 1,
    attachments: Iterable[str] = (),
) -> str:
    """Create a refresh-stable fingerprint for one semantic conversation turn.

    ``same_content_occurrence`` distinguishes two genuinely separate, identical
    messages while remaining stable if the DOM is recreated during refresh.
    """
    payload = {
        "role": str(role or "").strip().lower(),
        "text": normalize_turn_text(text),
        "same_content_occurrence": int(same_content_occurrence),
        "attachments": sorted(str(x) for x in attachments if str(x)),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def _runtime_turn_elements(runtime: Any, role: str) -> list[Any]:
    """Read turns from WebLLMScraper or a compatible test/runtime adapter."""
    if hasattr(runtime, "_turn_elements"):
        try:
            return list(runtime._turn_elements(role))
        except Exception:
            return []
    if hasattr(runtime, "turn_elements"):
        try:
            return list(runtime.turn_elements(role))
        except Exception:
            return []
    return []


def _runtime_generation_active(runtime: Any) -> bool:
    if hasattr(runtime, "_dismiss_known_blocking_dialogs"):
        try:
            runtime._dismiss_known_blocking_dialogs()
        except Exception:
            pass
    for name in ("_is_generation_active", "is_generation_active"):
        if hasattr(runtime, name):
            try:
                return bool(getattr(runtime, name)())
            except Exception:
                return False
    return False


def collect_turns(runtime: Any, role: str = "user") -> list[ConversationTurn]:
    """Collect semantic turns in DOM order using refresh-stable fingerprints."""
    role = str(role or "user").lower()
    elements = _runtime_turn_elements(runtime, role)
    counts: dict[str, int] = {}
    result: list[ConversationTurn] = []
    for ordinal, element in enumerate(elements, 1):
        raw_text = _safe_raw_text(element)
        text = normalize_turn_text(raw_text)
        attachments = tuple(_turn_attachment_signature(element))
        content_key = json.dumps(
            {"role": role, "text": text, "attachments": list(attachments)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        occurrence = counts.get(content_key, 0) + 1
        counts[content_key] = occurrence
        fp = fingerprint_turn(
            role=role,
            text=text,
            same_content_occurrence=occurrence,
            attachments=attachments,
        )
        result.append(ConversationTurn(
            role=role,
            text=text,
            fingerprint=fp,
            ordinal=ordinal,
            same_content_occurrence=occurrence,
            attachments=attachments,
            raw_text=raw_text,
        ))
    return result


def get_latest_turn(runtime: Any, role: str = "user") -> ConversationTurn | None:
    turns = collect_turns(runtime, role=role)
    return turns[-1] if turns else None


def has_changed(last_seen_fingerprint: str, latest_turn: ConversationTurn | None) -> bool:
    if latest_turn is None:
        return False
    return str(last_seen_fingerprint or "") != latest_turn.fingerprint


def get_new_turns(turns: Iterable[ConversationTurn], last_seen_fingerprint: str) -> list[ConversationTurn]:
    """Return turns strictly after ``last_seen_fingerprint``.

    If the fingerprint is no longer present, return an empty list rather than
    replaying the whole conversation.  The caller receives DRIFT from poll() and
    can safely re-baseline or re-open the known conversation.
    """
    turns = list(turns)
    last_seen = str(last_seen_fingerprint or "")
    if not last_seen:
        return []
    for idx, turn in enumerate(turns):
        if turn.fingerprint == last_seen:
            return turns[idx + 1:]
    return []


def wait_until_idle(runtime: Any, *, timeout_sec: float = 30.0, poll_interval: float = 0.25) -> bool:
    """Wait until generation is inactive; return False on timeout."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        if not _runtime_generation_active(runtime):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, float(poll_interval)))


class ConversationWatcher:
    """Stateful fresh-human-turn watcher shared by Local/Remote runtimes."""

    def __init__(
        self,
        runtime: Any,
        *,
        registry: Any = None,
        workspace: str = "",
        conversation_url: str = "",
        role: str = "user",
        last_seen_fingerprint: str = "",
    ):
        self.runtime = runtime
        self.registry = registry
        self.workspace = str(workspace or "")
        self.conversation_url = str(conversation_url or "")
        self.role = str(role or "user").lower()
        self.last_seen_fingerprint = str(last_seen_fingerprint or "")
        if not self.last_seen_fingerprint:
            self.last_seen_fingerprint = self._load_registry_last_seen()

    def _load_registry_last_seen(self) -> str:
        if not self.registry or not self.workspace or not self.conversation_url:
            return ""
        try:
            record = self.registry.find(self.workspace, self.conversation_url)
        except Exception:
            return ""
        if not isinstance(record, dict):
            return ""
        return str(record.get("last_seen_turn", "") or "")

    def _persist_last_seen(self, fingerprint: str) -> None:
        self.last_seen_fingerprint = str(fingerprint or "")
        if not self.registry or not self.workspace or not self.conversation_url:
            return
        try:
            self.registry.set_last_seen_turn(
                self.workspace,
                self.conversation_url,
                self.last_seen_fingerprint,
            )
        except Exception:
            # Watching must stay readable even if persistence is temporarily
            # unavailable. Stage 6 Supervisor may surface persistence health.
            pass

    def baseline(self) -> WatchPollResult:
        """Mark current latest human turn seen without emitting it as fresh."""
        if _runtime_generation_active(self.runtime):
            return WatchPollResult(WATCH_WAIT, detail="generation_active")
        latest = get_latest_turn(self.runtime, self.role)
        if latest is None:
            self._persist_last_seen("")
            return WatchPollResult(WATCH_BASELINED, latest_fingerprint="", detail="empty_conversation")
        self._persist_last_seen(latest.fingerprint)
        return WatchPollResult(WATCH_BASELINED, latest_fingerprint=latest.fingerprint)

    def poll(self, *, acknowledge: bool = True) -> WatchPollResult:
        """Poll once for fresh human turns.

        ``acknowledge=True`` persists the newest fresh fingerprint immediately.
        Stage 6 Supervisor can set it False if it wants task creation to commit
        the cursor only after durable enqueue.
        """
        if _runtime_generation_active(self.runtime):
            return WatchPollResult(
                WATCH_WAIT,
                latest_fingerprint=self.last_seen_fingerprint,
                detail="generation_active",
            )

        turns = collect_turns(self.runtime, self.role)
        latest = turns[-1] if turns else None
        if latest is None:
            return WatchPollResult(WATCH_IDLE, latest_fingerprint=self.last_seen_fingerprint)

        # First attach to a pre-existing conversation is baseline-only.
        if not self.last_seen_fingerprint:
            self._persist_last_seen(latest.fingerprint)
            return WatchPollResult(
                WATCH_BASELINED,
                latest_fingerprint=latest.fingerprint,
                detail="initial_attach_no_replay",
            )

        if latest.fingerprint == self.last_seen_fingerprint:
            return WatchPollResult(WATCH_IDLE, latest_fingerprint=latest.fingerprint)

        fresh = get_new_turns(turns, self.last_seen_fingerprint)
        if not fresh:
            # Cursor vanished due to conversation replacement/truncation. Never
            # replay all visible history; re-baseline latest and surface DRIFT.
            if acknowledge:
                self._persist_last_seen(latest.fingerprint)
            return WatchPollResult(
                WATCH_DRIFT,
                latest_fingerprint=latest.fingerprint,
                detail="last_seen_not_found_no_replay",
            )

        if acknowledge:
            self._persist_last_seen(fresh[-1].fingerprint)
        return WatchPollResult(
            WATCH_FRESH,
            fresh_turns=tuple(fresh),
            latest_fingerprint=fresh[-1].fingerprint,
        )

    def acknowledge(self, fingerprint: str) -> None:
        self._persist_last_seen(fingerprint)


# ---------------------------------------------------------------------------
# Deterministic Stage 5 regression tests (no Playwright/network required)
# ---------------------------------------------------------------------------

class _FakeElement:
    def __init__(self, text: str, attrs: Optional[dict[str, str]] = None, children: Optional[list[Any]] = None):
        self._text = text
        self._attrs = dict(attrs or {})
        self._children = list(children or [])

    def inner_text(self):
        return self._text

    def text_content(self):
        return self._text

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def query_selector_all(self, _selector: str):
        return list(self._children)


class _FakeRuntime:
    def __init__(self, user_texts: Optional[list[str]] = None):
        self.user_texts = list(user_texts or [])
        self.generation_active = False
        self.repaint_generation = 0

    def _turn_elements(self, role: str):
        if role != "user":
            return []
        # New objects and volatile ids on every call simulate React repaint.
        return [
            _FakeElement(text, attrs={"data-message-id": f"volatile-{self.repaint_generation}-{idx}"})
            for idx, text in enumerate(self.user_texts)
        ]

    def _is_generation_active(self):
        return self.generation_active

    def repaint(self):
        self.repaint_generation += 1


class _FakeRegistry:
    def __init__(self):
        self.record = {"last_seen_turn": ""}

    def find(self, _workspace: str, _url: str):
        return dict(self.record)

    def set_last_seen_turn(self, _workspace: str, _url: str, fp: str):
        self.record["last_seen_turn"] = fp


def run_conversation_watcher_self_tests() -> dict[str, Any]:
    results: dict[str, bool] = {}

    runtime = _FakeRuntime(["old message"])
    registry = _FakeRegistry()
    watcher = ConversationWatcher(runtime, registry=registry, workspace="C:/w", conversation_url="https://chatgpt.com/c/test")
    first = watcher.poll()
    results["initial_attach_baselines_old_history"] = first.status == WATCH_BASELINED and not first.fresh_turns

    idle_results = [watcher.poll() for _ in range(20)]
    results["twenty_polls_no_new_turn"] = all(x.status == WATCH_IDLE and not x.fresh_turns for x in idle_results)

    runtime.user_texts.append("new human request")
    fresh = watcher.poll()
    results["one_new_human_turn_exactly_once"] = (
        fresh.status == WATCH_FRESH
        and len(fresh.fresh_turns) == 1
        and fresh.fresh_turns[0].text == "new human request"
        and watcher.poll().status == WATCH_IDLE
    )

    before = get_latest_turn(runtime)
    runtime.repaint()
    after = get_latest_turn(runtime)
    results["dom_repaint_fingerprint_stable"] = bool(before and after and before.fingerprint == after.fingerprint)

    runtime.generation_active = True
    runtime.user_texts.append("must wait")
    cursor_before = watcher.last_seen_fingerprint
    waiting = watcher.poll()
    results["generation_active_returns_wait"] = (
        waiting.status == WATCH_WAIT
        and not waiting.fresh_turns
        and watcher.last_seen_fingerprint == cursor_before
    )
    runtime.generation_active = False
    after_wait = watcher.poll()
    results["turn_becomes_fresh_after_idle"] = (
        after_wait.status == WATCH_FRESH
        and len(after_wait.fresh_turns) == 1
        and after_wait.fresh_turns[0].text == "must wait"
    )

    # Identical text twice must remain two distinct semantic turns.
    runtime2 = _FakeRuntime(["same", "same"])
    turns2 = collect_turns(runtime2)
    results["identical_messages_have_distinct_fingerprints"] = (
        len(turns2) == 2 and turns2[0].fingerprint != turns2[1].fingerprint
    )
    runtime2.repaint()
    turns2b = collect_turns(runtime2)
    results["identical_message_fingerprints_survive_repaint"] = (
        [x.fingerprint for x in turns2] == [x.fingerprint for x in turns2b]
    )

    # Cursor drift must never replay the complete visible conversation.
    drift_runtime = _FakeRuntime(["a", "b"])
    drift_watcher = ConversationWatcher(drift_runtime, last_seen_fingerprint="not-present")
    drift = drift_watcher.poll()
    results["missing_cursor_does_not_replay_history"] = drift.status == WATCH_DRIFT and not drift.fresh_turns

    results["all_passed"] = all(results.values())
    return results


__all__ = [
    "WATCH_IDLE", "WATCH_WAIT", "WATCH_BASELINED", "WATCH_FRESH", "WATCH_DRIFT",
    "ConversationTurn", "WatchPollResult", "ConversationWatcher",
    "normalize_turn_text", "fingerprint_turn", "collect_turns", "get_latest_turn",
    "has_changed", "get_new_turns", "wait_until_idle", "run_conversation_watcher_self_tests",
]
