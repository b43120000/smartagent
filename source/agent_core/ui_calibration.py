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

from .paths import web_ui_calibration_lock_path
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
MAX_PLAN_ATTEMPTS = 2
DOM_EVIDENCE_LIMIT = 240


class CalibrationError(RuntimeError):
    pass


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema") == PLAN_SCHEMA:
            return value
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
        "You are the planner for SmartAgent Web UI calibration. Analyze only the bounded "
        "structural DOM evidence below. Return exactly one JSON object and no markdown. "
        "Do not return Python, JavaScript, shell commands, URLs, XPath, text selectors, or "
        "instructions. Selectors must be standard CSS usable by document.querySelectorAll. "
        "Use stable semantic attributes and keep provider-specific details inside this profile.\n"
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


def validate_live_candidate(page, selectors: Mapping[str, object], *, timeout_sec: float = 180.0) -> dict:
    token = "SMARTAGENT_CALIBRATION_OK_" + uuid.uuid4().hex[:12].upper()
    prompt = f"[SMARTAGENT_UI_CALIBRATION] Reply with exactly: {token}"
    before_users = len(page.query_selector_all(selectors["user_turn_selector"]))
    before_assistants = len(page.query_selector_all(selectors["assistant_turn_selector"]))
    _write_composer(page, str(selectors["composer_selector"]), prompt)
    try:
        _send_probe(page, str(selectors["send_selector"]))
    except Exception:
        _clear_composer(page, str(selectors["composer_selector"]))
        raise

    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    user_confirmed = False
    assistant_confirmed = False
    busy_observed = False
    while time.monotonic() < deadline:
        users = _texts(page, str(selectors["user_turn_selector"]))
        assistants = _texts(page, str(selectors["assistant_turn_selector"]))
        user_confirmed = len(users) > before_users and any(prompt in text for text in users[before_users:])
        assistant_confirmed = (
            len(assistants) > before_assistants
            and any(token in text for text in assistants[before_assistants:])
        )
        for field in ("stop_selectors", "busy_selectors"):
            for candidate in selectors[field]:
                try:
                    if _visible_last(page, candidate) is not None:
                        busy_observed = True
                except Exception:
                    pass
        if user_confirmed and assistant_confirmed:
            break
        page.wait_for_timeout(250)
    if not user_confirmed:
        raise CalibrationError("live_user_turn_not_confirmed")
    if not assistant_confirmed:
        raise CalibrationError("live_assistant_turn_not_confirmed")

    # The final-content selector must find the probe token inside the newly
    # observed assistant turn, not merely anywhere in the page.
    assistants = page.query_selector_all(str(selectors["assistant_turn_selector"]))
    newest = assistants[-1] if assistants else None
    final_confirmed = False
    if newest is not None:
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
    quiet_since = 0.0
    quiet_deadline = time.monotonic() + 30.0
    while time.monotonic() < quiet_deadline:
        active = False
        for candidate in selectors["stop_selectors"]:
            try:
                active = active or _visible_last(page, candidate) is not None
            except Exception:
                pass
        try:
            for candidate in selectors["busy_selectors"]:
                active = active or bool(newest.query_selector_all(candidate))
        except Exception:
            pass
        if active:
            quiet_since = 0.0
        elif not quiet_since:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= 2.0:
            break
        page.wait_for_timeout(250)
    else:
        raise CalibrationError("live_completion_state_not_quiet")
    return {
        "passed": True,
        "probe_token": token,
        "user_turn_confirmed": True,
        "assistant_turn_confirmed": True,
        "final_content_confirmed": True,
        "activity_control_observed": busy_observed,
        "activity_control_status": "OBSERVED" if busy_observed else "NOT_OBSERVED_FAST_RESPONSE",
    }


def _start_scraper(provider: str, url: str, *, playwright=None):
    from .web_runtime import WebLLMScraper
    scraper = WebLLMScraper(service=provider, headless=False)
    scraper.cfg = dict(scraper.cfg)
    scraper.cfg["url"] = url
    scraper.start(show_browser=True, playwright=playwright)
    return scraper


def calibrate(planner_url: str, target_url: str) -> Path:
    from WebAgent.browser_bridge import execution_page_lease

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
            print(f"[adapterUI] 登入確認：Planner {planner_provider} = {planner_auth.status}")
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
            print(f"[adapterUI] 登入確認：Target {target_provider} = {target_auth.status}")
            if not target_adapter.page_reachable():
                raise CalibrationError(f"{target_provider.upper()}_PAGE_UNREACHABLE")

            evidence = capture_structural_evidence(target_page)
            feedback = ""
            candidate = None
            counts = None
            for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
                response = planner.ask(_planner_prompt(target_provider, evidence, feedback))
                try:
                    parsed = parse_planner_response(response, expected_provider=target_provider)
                    candidate = parsed["selectors"]
                    counts = validate_static_candidate(target_page, candidate)
                    break
                except (CalibrationError, ProfileValidationError) as exc:
                    feedback = f"attempt={attempt}; local_error={exc}"
                    if attempt >= MAX_PLAN_ATTEMPTS:
                        raise CalibrationError("planner_candidate_validation_failed:" + str(exc)) from exc
            if candidate is None:
                raise CalibrationError("planner_candidate_missing")

            live = validate_live_candidate(target_page, candidate)
            live["static_counts"] = counts
            document = build_profile_document(
                target_provider,
                candidate,
                source_url=target_url,
                planner_url=planner_url,
                validation=live,
            )
            return publish_profile(document)
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
        print(f"[adapterUI] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"[adapterUI] PASS: active profile published to {active}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
