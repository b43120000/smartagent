"""Planner-assisted, locally validated Web UI selector calibration.

The planner only proposes inert CSS selector data.  This controller owns all
browser access, validation, persistence and rollback boundaries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid
from typing import Mapping

from .paths import browser_operator_root, log_root, web_ui_calibration_lock_path
from .process_file_lock import exclusive_process_lock
from .conversation_identity import same_conversation
from .web_ui.factory import (
    create_web_ui_for_page,
    normalize_web_conversation_url,
    provider_from_url,
)
from .web_ui.provider_registry import calibration_providers
from .web_ui.profile_store import (
    ALL_FIELDS,
    ProfileValidationError,
    build_profile_document,
    publish_profile,
    rollback_profile,
    validate_selector_payload,
)


PLAN_SCHEMA = "SMARTAGENT_UI_CALIBRATION_PLAN_V1"
MAX_PLAN_ATTEMPTS = 3
DOM_EVIDENCE_LIMIT = 240
CALIBRATION_LOG_NAME = "web_ui_calibration.jsonl"
_CALIBRATION_LOG_WARNING_EMITTED = False

_PROGRESS_COMPONENTS = (
    "composer_selector",
    "send_selector",
    "user_turn_selector",
    "user_content_selector",
    "assistant_turn_selector",
    "response_complete",
    "final_content_selector",
    "profile_published",
)
_CALIBRATION_PROGRESS_DONE: set[str] = set()


def _progress_percent(completed: int, total: int) -> int:
    if total <= 0:
        return 100
    return max(0, min(100, (max(0, int(completed)) * 100) // int(total)))


def _emit_calibration_progress(*, result: str = "", exit_code: int | None = None) -> None:
    completed = len(_CALIBRATION_PROGRESS_DONE)
    total = len(_PROGRESS_COMPONENTS)
    percent = _progress_percent(completed, total)
    width = 20
    filled = min(width, (completed * width) // total) if total else width
    bar = "#" * filled + "-" * (width - filled)
    suffix = f" | components={completed}/{total}"
    if result:
        suffix += f" | RESULT={result}"
    if exit_code is not None:
        suffix += f" | exit_code={int(exit_code)}"
    print(f"[adapterUI] Progress: [{bar}] {percent}%{suffix}")


def _reset_calibration_progress() -> None:
    _CALIBRATION_PROGRESS_DONE.clear()


def _mark_calibration_progress(component: str, *, result: str = "") -> None:
    if component not in _PROGRESS_COMPONENTS:
        raise ValueError(f"unknown calibration progress component: {component}")
    _CALIBRATION_PROGRESS_DONE.add(component)
    _emit_calibration_progress(result=result)


def _calibration_log(event: str, **fields: object) -> bool:
    global _CALIBRATION_LOG_WARNING_EMITTED
    try:
        path = log_root() / CALIBRATION_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": time.time(), "event": str(event), **fields}
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:
        if not _CALIBRATION_LOG_WARNING_EMITTED:
            _CALIBRATION_LOG_WARNING_EMITTED = True
            print(
                f"[adapterUI] WARN calibration_log_write_failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return False


class CalibrationError(RuntimeError):
    pass


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    decoder = json.JSONDecoder()
    last_error = None
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(value, dict) and value.get("schema") == PLAN_SCHEMA:
            return value
    if last_error is not None:
        raise CalibrationError(
            f"planner_response_invalid_json:{last_error.msg}:line={last_error.lineno}:col={last_error.colno}:pos={last_error.pos}"
        )
    raise CalibrationError("planner_response_missing_calibration_json")


def parse_planner_response(text: str, *, expected_provider: str) -> dict:
    payload = _extract_json_object(text)
    allowed = {"schema", "provider", "selectors", "notes"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CalibrationError("planner_unknown_fields:" + ",".join(unknown))
    if str(payload.get("provider") or "").strip().lower() != expected_provider:
        raise CalibrationError("planner_provider_mismatch")
    selectors = validate_selector_payload(expected_provider, payload.get("selectors", {}))
    return {"selectors": selectors, "notes": str(payload.get("notes") or "")[:2000]}


def capture_structural_evidence(page) -> dict:
    """Capture bounded attributes only; never page text, URLs or user content."""
    rows = page.evaluate(
        """(limit) => {
          const candidates = Array.from(document.querySelectorAll(
            '[contenteditable],textarea,button,[role="textbox"],[role="button"],'
            + '[data-testid],[data-message-author-role],[data-content-search-unit-key],'
            + '[data-is-streaming],[aria-busy],[role="progressbar"]'
          ));
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const clean = value => String(value || '').slice(0, 180);
          return candidates.slice(0, limit).map(el => {
            const parent = el.parentElement;
            return {
              tag: el.tagName.toLowerCase(),
              id: clean(el.id),
              role: clean(el.getAttribute('role')),
              testid: clean(el.getAttribute('data-testid')),
              aria_label: clean(el.getAttribute('aria-label')),
              contenteditable: clean(el.getAttribute('contenteditable')),
              message_role: clean(el.getAttribute('data-message-author-role')),
              search_unit: clean(el.getAttribute('data-content-search-unit-key')),
              streaming: clean(el.getAttribute('data-is-streaming')),
              aria_busy: clean(el.getAttribute('aria-busy')),
              class_name: clean(typeof el.className === 'string' ? el.className : ''),
              parent_tag: parent ? parent.tagName.toLowerCase() : '',
              parent_role: parent ? clean(parent.getAttribute('role')) : '',
              parent_testid: parent ? clean(parent.getAttribute('data-testid')) : '',
              visible: visible(el)
            };
          });
        }""",
        DOM_EVIDENCE_LIMIT,
    )
    return {
        "schema": "SMARTAGENT_UI_STRUCTURAL_EVIDENCE_V1",
        "element_count": len(rows or []),
        "elements": list(rows or []),
    }


def capture_page_evidence_image(page, label: str) -> str:
    """Capture one calibration-state viewport image without mutating the page."""
    try:
        root = browser_operator_root()
        root.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(label or "state"))[:48]
        target = root / f"{int(time.time() * 1000)}_calibration_{safe}.png"
        page.screenshot(path=str(target), full_page=False)
        _calibration_log("state_screenshot_captured", state=str(label), path=str(target))
        return str(target)
    except Exception as exc:
        print(
            f"[adapterUI] WARN calibration_screenshot_failed state={label}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        _calibration_log(
            "state_screenshot_failed", state=str(label),
            error_type=type(exc).__name__, error=str(exc),
        )
        return ""


def capture_calibration_state(page, state_name: str) -> dict:
    evidence = capture_structural_evidence(page)
    image_path = capture_page_evidence_image(page, state_name)
    return {"state": str(state_name), "evidence": evidence, "image_path": image_path}


def _snapshot_image_paths(snapshot: Mapping[str, object]) -> list[str] | None:
    value = str(snapshot.get("image_path") or "").strip()
    return [value] if value else None


def _planner_prompt(provider: str, evidence: Mapping[str, object], feedback: str = "") -> str:
    selector_shape = {
        "composer_selector": "CSS selector",
        "send_selector": "CSS selector",
        "user_turn_selector": "CSS selector",
        "user_content_selector": "CSS selector",
        "assistant_turn_selector": "CSS selector",
        "final_content_selector": "CSS selector",
        "stop_selectors": ["CSS selector"],
        "busy_selectors": ["CSS selector"],
    }
    return (
        "You are the planner for SmartAgent Web UI calibration. Analyze the bounded structural DOM "
        "evidence below together with the attached calibration-state screenshot when one is present. "
        "Treat DOM evidence as authoritative for constructing CSS selectors; use the screenshot only "
        "for visual, state, and role disambiguation. Return exactly one JSON object and no markdown. "
        "Do not return Python, JavaScript, shell commands, URLs, XPath, text selectors, or "
        "instructions. Selectors must be standard CSS usable by document.querySelectorAll. "
        "Use stable semantic attributes and keep provider-specific details inside this profile.\n"
        "OUTPUT CONTRACT: the first non-whitespace character must be { and the last must be }; "
        "emit no prose before or after the JSON. If previous validation feedback is present, correct it.\n"
        "STRICT JSON: the complete response must parse with Python json.loads. Every selector is a JSON string. "
        "Prefer single quotes inside CSS attribute selectors, for example [role='textbox'] and [aria-busy='false'], "
        "so JSON string delimiters are never confused with CSS syntax. If a CSS selector must contain a double quote, "
        "escape that quote correctly for JSON; never emit a raw double quote inside a selector string.\n"
        f"Required schema: {PLAN_SCHEMA}\n"
        f"Provider: {provider}\n"
        f"Exact shape: {json.dumps({'schema': PLAN_SCHEMA, 'provider': provider, 'selectors': selector_shape, 'notes': ''}, ensure_ascii=False)}\n"
        f"Previous local validation feedback: {feedback or 'none'}\n"
        "Structural evidence (contains no page text):\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


def _selector_counts(page, selectors: Mapping[str, object]) -> dict:
    result: dict[str, object] = {}
    for field in ALL_FIELDS:
        values = selectors[field] if isinstance(selectors[field], tuple) else (selectors[field],)
        entries = []
        for selector in values:
            try:
                locator = page.locator(selector)
                count = int(locator.count())
                visible = sum(1 for index in range(count) if locator.nth(index).is_visible())
                entries.append({"selector": selector, "count": count, "visible": visible})
            except Exception as exc:
                raise CalibrationError(f"invalid_css:{field}:{type(exc).__name__}") from exc
        result[field] = entries
    return result


def validate_static_candidate(page, selectors: Mapping[str, object]) -> dict:
    counts = _selector_counts(page, selectors)
    composer = counts["composer_selector"][0]
    if int(composer["count"]) < 1 or int(composer["visible"]) != 1:
        raise CalibrationError(
            f"composer_must_have_one_visible:count={composer['count']}:visible={composer['visible']}"
        )
    # Send can be hidden until the composer contains text.  Turn selectors may
    # legitimately be empty in a new conversation and are proven by live probe.
    return counts


def _visible_last(page, selector: str):
    locator = page.locator(selector)
    for index in range(int(locator.count()) - 1, -1, -1):
        item = locator.nth(index)
        if item.is_visible():
            return item
    return None


def _write_composer(page, selector: str, prompt: str) -> None:
    composer = _visible_last(page, selector)
    if composer is None:
        raise CalibrationError("live_composer_missing")
    composer.click()
    try:
        composer.fill(prompt)
    except Exception:
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(prompt)
    observed = ""
    for reader in (composer.inner_text, composer.input_value, composer.text_content):
        try:
            value = reader()
        except Exception:
            continue
        if value is not None:
            observed = str(value).strip()
            if observed:
                break
    if observed != prompt:
        raise CalibrationError("live_composer_roundtrip_mismatch")


def _send_probe(page, selector: str) -> None:
    control = _visible_last(page, selector)
    if control is None:
        raise CalibrationError("live_send_control_missing")
    control.click()


def _clear_composer(page, selector: str) -> None:
    try:
        composer = _visible_last(page, selector)
        if composer is None:
            return
        composer.click()
        try:
            composer.fill("")
        except Exception:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
    except Exception:
        pass


def _texts(page, selector: str) -> list[str]:
    out: list[str] = []
    for element in page.query_selector_all(selector):
        try:
            out.append(str(element.inner_text() or element.text_content() or ""))
        except Exception:
            out.append("")
    return out


def begin_live_probe(page, selectors: Mapping[str, object]) -> dict:
    token = "SMARTAGENT_CALIBRATION_OK_" + uuid.uuid4().hex[:12].upper()
    prompt = f"[SMARTAGENT_UI_CALIBRATION] Reply with exactly: {token}"
    _write_composer(page, str(selectors["composer_selector"]), prompt)
    try:
        _send_probe(page, str(selectors["send_selector"]))
    except Exception:
        _clear_composer(page, str(selectors["composer_selector"]))
        raise
    # Allow the target UI to materialize the newly sent user turn before
    # post-send structural evidence is captured.
    page.wait_for_timeout(1000)
    return {"token": token, "prompt": prompt}


def _matching_assistant(page, selector: str, token: str):
    assistants = page.query_selector_all(selector)
    for assistant in reversed(assistants):
        try:
            if token in str(assistant.inner_text() or assistant.text_content() or ""):
                return assistant
        except Exception:
            pass
    return None


def wait_for_probe_turns(
    page,
    selectors: Mapping[str, object],
    *,
    token: str,
    prompt: str,
    timeout_sec: float = 180.0,
) -> dict:
    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    user_confirmed = False
    assistant_confirmed = False
    busy_observed = False
    while time.monotonic() < deadline:
        users = _texts(page, str(selectors["user_turn_selector"]))
        assistants = _texts(page, str(selectors["assistant_turn_selector"]))
        user_confirmed = any(prompt in value for value in users)
        assistant_confirmed = any(token in value for value in assistants)
        for field in ("stop_selectors", "busy_selectors"):
            for candidate in selectors[field]:
                try:
                    if _visible_last(page, candidate) is not None:
                        busy_observed = True
                except Exception:
                    pass
        if user_confirmed and assistant_confirmed:
            return {
                "user_turn_confirmed": True,
                "assistant_turn_confirmed": True,
                "activity_control_observed": busy_observed,
            }
        page.wait_for_timeout(250)
    if not user_confirmed:
        raise CalibrationError("live_user_turn_not_confirmed")
    raise CalibrationError("live_assistant_turn_not_confirmed")


def wait_for_response_complete(
    page,
    selectors: Mapping[str, object],
    *,
    token: str,
    timeout_sec: float = 30.0,
    stable_sec: float = 2.0,
) -> dict:
    deadline = time.monotonic() + max(10.0, float(timeout_sec))
    stable_since = 0.0
    previous_text = None
    busy_observed = False
    while time.monotonic() < deadline:
        newest = _matching_assistant(page, str(selectors["assistant_turn_selector"]), token)
        if newest is None:
            stable_since = 0.0
            previous_text = None
            page.wait_for_timeout(250)
            continue
        try:
            current_text = str(newest.inner_text() or newest.text_content() or "")
        except Exception:
            current_text = ""
        active = False
        for candidate in selectors["stop_selectors"]:
            try:
                active = active or _visible_last(page, candidate) is not None
            except Exception:
                pass
        for candidate in selectors["busy_selectors"]:
            try:
                active = active or bool(newest.query_selector_all(candidate))
            except Exception:
                pass
        busy_observed = busy_observed or active
        if active or not current_text or current_text != previous_text:
            stable_since = 0.0
        elif not stable_since:
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= max(1.0, float(stable_sec)):
            return {
                "response_complete": True,
                "activity_control_observed": busy_observed,
            }
        previous_text = current_text
        page.wait_for_timeout(250)
    raise CalibrationError("live_completion_state_not_quiet")


def validate_final_content(
    page, selectors: Mapping[str, object], *, token: str
) -> None:
    newest = _matching_assistant(page, str(selectors["assistant_turn_selector"]), token)
    if newest is None:
        raise CalibrationError("live_assistant_turn_not_confirmed")
    final_confirmed = False
    try:
        blocks = newest.query_selector_all(str(selectors["final_content_selector"]))
        final_confirmed = any(token in str(block.inner_text() or "") for block in blocks)
        if not final_confirmed:
            final_confirmed = bool(newest.evaluate(
                "(el, selector) => el.matches(selector)",
                str(selectors["final_content_selector"]),
            )) and token in str(newest.inner_text() or "")
    except Exception:
        final_confirmed = False
    if not final_confirmed:
        raise CalibrationError("live_final_content_not_confirmed")



def _start_scraper(provider: str, url: str, *, playwright=None):
    from .web_runtime import WebLLMScraper
    scraper = WebLLMScraper(service=provider, headless=False)
    scraper.cfg = dict(scraper.cfg)
    scraper.cfg["url"] = url
    scraper.start(show_browser=True, playwright=playwright)
    return scraper


def calibrate(planner_url: str, target_url: str) -> Path:
    from WebAgent.browser_bridge import execution_page_lease

    _reset_calibration_progress()
    planner_url = normalize_web_conversation_url(planner_url)
    target_url = normalize_web_conversation_url(target_url)
    if planner_url == target_url or same_conversation(planner_url, target_url):
        raise CalibrationError("planner_and_target_must_be_distinct")
    planner_provider = provider_from_url(planner_url)
    target_provider = provider_from_url(target_url)
    eligible = set(calibration_providers())
    if planner_provider not in eligible:
        raise CalibrationError(f"planner_provider_not_calibratable:{planner_provider}")
    if target_provider not in eligible:
        raise CalibrationError(f"target_provider_not_calibratable:{target_provider}")


    execution_identity = {
        "module_file": str(Path(__file__).resolve()),
        "python_executable": str(Path(sys.executable).resolve()),
        "log_root": str(log_root()),
        "browser_operator_root": str(browser_operator_root()),
    }
    print("[adapterUI] execution_identity=" + json.dumps(execution_identity, ensure_ascii=False))
    startup_log_ok = _calibration_log("startup_execution_identity", **execution_identity)
    print(f"[adapterUI] startup_log_write={'PASS' if startup_log_ok else 'FAIL'}")

    with exclusive_process_lock(
        web_ui_calibration_lock_path(), timeout_sec=5.0,
        label="Web UI calibration", legacy_kind="web-ui-calibration-v1",
    ), execution_page_lease(timeout_sec=5.0, label="Web UI calibration pages"):
        planner = None
        target = None
        target_page = None
        try:
            planner = _start_scraper(planner_provider, planner_url)
            planner._execution_page_lease_owned = True
            planner_adapter = create_web_ui_for_page(planner._page, provider=planner_provider)
            planner_auth = planner_adapter.authentication_state()
            if not planner_auth.authenticated:
                raise CalibrationError(
                    f"planner_login_not_confirmed:{planner_provider}:"
                    f"{planner_auth.status}:{planner_auth.reason}"
                )
            print(f"[adapterUI] ????????謅Ｗ?lanner {planner_provider} = {planner_auth.status}")
            planner_adapter.require_compatible()
            if target_provider == planner_provider:
                target_page = planner._browser.new_page()
                target_page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                target_page.wait_for_timeout(2000)
            else:
                target = _start_scraper(
                    target_provider, target_url, playwright=planner._pw
                )
                target._execution_page_lease_owned = True
                target_page = target._page

            try:
                landed_provider = provider_from_url(str(getattr(target_page, "url", "") or ""))
            except Exception as exc:
                raise CalibrationError("target_page_provider_unavailable") from exc
            if landed_provider != target_provider:
                raise CalibrationError(
                    f"target_page_provider_mismatch:expected={target_provider}:actual={landed_provider}"
                )
            target_adapter = create_web_ui_for_page(target_page, provider=target_provider)
            target_auth = target_adapter.authentication_state()
            if not target_auth.authenticated:
                raise CalibrationError(
                    f"target_login_not_confirmed:{target_provider}:"
                    f"{target_auth.status}:{target_auth.reason}"
                )
            print(f"[adapterUI] ????????謅?arget {target_provider} = {target_auth.status}")
            if not target_adapter.page_reachable():
                raise CalibrationError(f"{target_provider.upper()}_PAGE_UNREACHABLE")

            startup_screenshot = capture_page_evidence_image(target_page, "startup_health")
            print(f"[adapterUI] startup_screenshot={startup_screenshot or '<FAILED>'}")
            _calibration_log(
                "startup_screenshot_health",
                success=bool(startup_screenshot),
                path=startup_screenshot,
            )


            idle_snapshot = capture_calibration_state(target_page, "idle")
            evidence = idle_snapshot["evidence"]
            feedback = ""
            candidate = None
            counts = None
            for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
                _calibration_log("planner_attempt_begin", attempt=attempt, provider=target_provider, feedback=feedback)
                response = planner.ask(
                    _planner_prompt(target_provider, evidence, feedback),
                    image_paths=_snapshot_image_paths(idle_snapshot),
                )
                response_text = str(response or "")
                _calibration_log("planner_response", attempt=attempt, provider=target_provider, response_length=len(response_text), schema_marker_present=PLAN_SCHEMA in response_text, response_preview=response_text[:2000])
                try:
                    parsed = parse_planner_response(response_text, expected_provider=target_provider)
                    candidate = parsed["selectors"]
                    counts = validate_static_candidate(target_page, candidate)
                    _mark_calibration_progress("composer_selector")
                    _calibration_log("planner_candidate_valid", attempt=attempt, provider=target_provider)
                    break
                except (CalibrationError, ProfileValidationError) as exc:
                    feedback = f"attempt={attempt}; local_error={exc}"
                    _calibration_log("planner_candidate_invalid", attempt=attempt, provider=target_provider, error_type=type(exc).__name__, error=str(exc))
                    if attempt >= MAX_PLAN_ATTEMPTS:
                        raise CalibrationError("planner_candidate_validation_failed:" + str(exc)) from exc
            if candidate is None:
                raise CalibrationError("planner_candidate_missing")

            active_draft = "[SMARTAGENT_UI_CALIBRATION_DRAFT]"
            _calibration_log("active_send_evidence_begin", provider=target_provider)
            _write_composer(target_page, str(candidate["composer_selector"]), active_draft)
            try:
                active_snapshot = capture_calibration_state(target_page, "composer_active")
                active_evidence = active_snapshot["evidence"]
                active_feedback = (
                    "ACTIVE STATE: the target composer currently contains a calibration draft and the "
                    "state-dependent send control should now be visible. Return the full selector profile "
                    "from this active-state evidence. The send_selector must match the currently visible "
                    "send control; do not use a fail-closed or never-match selector for send_selector."
                )
                active_response = planner.ask(
                    _planner_prompt(target_provider, active_evidence, active_feedback),
                    image_paths=_snapshot_image_paths(active_snapshot),
                )
                active_response_text = str(active_response or "")
                _calibration_log(
                    "active_send_planner_response", provider=target_provider,
                    response_length=len(active_response_text),
                    schema_marker_present=PLAN_SCHEMA in active_response_text,
                    response_preview=active_response_text[:2000],
                )
                active_parsed = parse_planner_response(active_response_text, expected_provider=target_provider)
                active_candidate = active_parsed["selectors"]
                if _visible_last(target_page, str(active_candidate["send_selector"])) is None:
                    raise CalibrationError("active_send_control_missing")
                # Composer-active state owns only the state-dependent send control.
                # Keep the idle-state structural selectors until their own state exists.
                merged_active = dict(candidate)
                merged_active["send_selector"] = active_candidate["send_selector"]
                candidate = merged_active
                counts = validate_static_candidate(target_page, candidate)
                _mark_calibration_progress("send_selector")
                _calibration_log("active_send_candidate_valid", provider=target_provider)
            finally:
                _clear_composer(target_page, str(candidate["composer_selector"]))

            # POST-SEND STATE: send one unique probe first, then discover the
            # user-turn structure from a DOM in which that turn actually exists.
            probe = begin_live_probe(target_page, candidate)
            _calibration_log("post_send_evidence_begin", provider=target_provider)
            post_send_snapshot = capture_calibration_state(target_page, "post_send")
            post_send_evidence = post_send_snapshot["evidence"]
            post_send_feedback = (
                "POST-SEND STATE: a unique calibration probe has already been sent and the new user turn "
                "is now present in the target DOM. Return the full selector profile from this post-send "
                "evidence. user_turn_selector and user_content_selector must identify the newly sent user "
                "message containing the calibration probe; do not use fail-closed, never-match, or "
                "unobserved selectors for those fields. Reconfirm assistant_turn_selector only when the "
                "post-send evidence structurally supports it."
            )
            post_send_response = planner.ask(
                _planner_prompt(target_provider, post_send_evidence, post_send_feedback),
                image_paths=_snapshot_image_paths(post_send_snapshot),
            )
            post_send_response_text = str(post_send_response or "")
            _calibration_log(
                "post_send_planner_response", provider=target_provider,
                response_length=len(post_send_response_text),
                schema_marker_present=PLAN_SCHEMA in post_send_response_text,
                response_preview=post_send_response_text[:2000],
            )
            post_send_parsed = parse_planner_response(
                post_send_response_text, expected_provider=target_provider
            )
            post_send_candidate = post_send_parsed["selectors"]
            merged_post_send = dict(candidate)
            merged_post_send["user_turn_selector"] = post_send_candidate["user_turn_selector"]
            merged_post_send["user_content_selector"] = post_send_candidate["user_content_selector"]
            try:
                if target_page.query_selector_all(str(post_send_candidate["assistant_turn_selector"])):
                    merged_post_send["assistant_turn_selector"] = post_send_candidate["assistant_turn_selector"]
            except Exception:
                pass
            post_users = _texts(target_page, str(merged_post_send["user_turn_selector"]))
            if not any(probe["prompt"] in text for text in post_users):
                raise CalibrationError("post_send_user_turn_not_found")
            post_user_content = _texts(target_page, str(merged_post_send["user_content_selector"]))
            if not any(probe["prompt"] in text for text in post_user_content):
                raise CalibrationError("post_send_user_content_not_found")
            _mark_calibration_progress("user_turn_selector")
            _mark_calibration_progress("user_content_selector")
            candidate = merged_post_send
            counts = validate_static_candidate(target_page, candidate)
            _calibration_log("post_send_candidate_valid", provider=target_provider)

            _calibration_log("live_validation_begin", provider=target_provider)
            probe_live = wait_for_probe_turns(
                target_page, candidate,
                token=str(probe["token"]), prompt=str(probe["prompt"]),
            )
            _mark_calibration_progress("assistant_turn_selector")
            completion = wait_for_response_complete(
                target_page, candidate, token=str(probe["token"]),
            )
            _mark_calibration_progress("response_complete")

            _calibration_log("response_complete_evidence_begin", provider=target_provider)
            response_snapshot = capture_calibration_state(target_page, "response_complete")
            response_feedback = (
                "RESPONSE-COMPLETE STATE: the unique calibration probe has a completed assistant "
                "response visible in the target DOM. Return the full selector profile from this "
                "completed-response evidence. final_content_selector must identify the rendered final "
                "assistant content containing the calibration response; do not use fail-closed, "
                "never-match, or unobserved selectors for final_content_selector. Reconfirm "
                "assistant_turn_selector only when this evidence structurally supports it."
            )
            response_planner = planner.ask(
                _planner_prompt(target_provider, response_snapshot["evidence"], response_feedback),
                image_paths=_snapshot_image_paths(response_snapshot),
            )
            response_text = str(response_planner or "")
            _calibration_log(
                "response_complete_planner_response", provider=target_provider,
                response_length=len(response_text),
                schema_marker_present=PLAN_SCHEMA in response_text,
                response_preview=response_text[:2000],
            )
            response_parsed = parse_planner_response(
                response_text, expected_provider=target_provider
            )
            response_candidate = response_parsed["selectors"]
            merged_response = dict(candidate)
            merged_response["final_content_selector"] = response_candidate["final_content_selector"]
            try:
                response_assistants = _texts(
                    target_page, str(response_candidate["assistant_turn_selector"])
                )
                if any(str(probe["token"]) in value for value in response_assistants):
                    merged_response["assistant_turn_selector"] = response_candidate["assistant_turn_selector"]
            except Exception:
                pass
            candidate = merged_response
            counts = validate_static_candidate(target_page, candidate)
            validate_final_content(target_page, candidate, token=str(probe["token"]))
            _mark_calibration_progress("final_content_selector")
            _calibration_log("response_complete_candidate_valid", provider=target_provider)

            activity_observed = bool(
                probe_live.get("activity_control_observed")
                or completion.get("activity_control_observed")
            )
            live = {
                "passed": True,
                "probe_token": str(probe["token"]),
                "user_turn_confirmed": True,
                "assistant_turn_confirmed": True,
                "final_content_confirmed": True,
                "response_complete_confirmed": True,
                "activity_control_observed": activity_observed,
                "activity_control_status": (
                    "OBSERVED" if activity_observed else "NOT_OBSERVED_FAST_RESPONSE"
                ),
            }
            _calibration_log("live_validation_pass", provider=target_provider)
            live["static_counts"] = counts
            document = build_profile_document(
                target_provider,
                candidate,
                source_url=target_url,
                planner_url=planner_url,
                validation=live,
            )
            active_path = publish_profile(document)
            _mark_calibration_progress("profile_published", result="SUCCESS")
            _calibration_log("profile_published", provider=target_provider, path=str(active_path))
            return active_path
        except Exception as exc:
            _calibration_log("terminal_error", planner_provider=planner_provider, target_provider=target_provider, error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
            elif target_page is not None:
                try:
                    target_page.close()
                except Exception:
                    pass
            if planner is not None:
                try:
                    planner.close()
                except Exception:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartAgent Web UI calibration")
    parser.add_argument("--planner-url", default="")
    parser.add_argument("--target-url", default="")
    parser.add_argument("--rollback", choices=calibration_providers(), default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        assert provider_from_url("https://chatgpt.com/c/example") == "chatgpt"
        assert provider_from_url("https://gemini.google.com/u/1/app/example") == "gemini"
        assert provider_from_url("https://claude.ai/chat/example") == "claude"
        print("SMARTAGENT_UI_CALIBRATION_CLI_OK")
        return 0
    if args.rollback:
        try:
            active = rollback_profile(args.rollback)
        except Exception as exc:
            print(f"[adapterUI] ROLLBACK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(f"[adapterUI] ROLLBACK PASS: {active}")
        return 0
    planner_url = args.planner_url.strip() or input("Planner URL: ").strip()
    target_url = args.target_url.strip() or input("Target / repickup URL: ").strip()
    try:
        active = calibrate(planner_url, target_url)
    except Exception as exc:
        _calibration_log("cli_error", error_type=type(exc).__name__, error=str(exc))
        _emit_calibration_progress(result="FAILED", exit_code=2)
        print(f"[adapterUI] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"[adapterUI] PASS: active profile published to {active}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
