"""Opt-in real-site IT: click picker, save config, reopen through install_smart_agent/launchers/writeSendBox.bat.

This writes a real provider dom_config.json. It never submits a message.
Run: python -B -m agent_core.web_ui.tests.live_composer_it --url https://chatgpt.com/
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from ..dom_config import config_path, load_dom_config
from ..manual_browser import active_agent_endpoint, ensure_agent_browser
from ..provider_registry import provider_from_url


@contextmanager
def driver_context(provider: str, endpoint: str):
    """Use the same reusable Agent browser as real calibration launchers."""
    from playwright.sync_api import sync_playwright
    from ...shared_browser import shared_browser_enabled, shared_browser_endpoint, connect_shared_browser

    with sync_playwright() as playwright:
        started = False
        if not endpoint and shared_browser_enabled():
            endpoint = shared_browser_endpoint()
        elif not endpoint:
            endpoint = active_agent_endpoint(provider)
        if not endpoint:
            endpoint, started = ensure_agent_browser(playwright)
        browser = connect_shared_browser(playwright, endpoint)[0] if shared_browser_enabled() else playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        bootstrap_pages = []
        context._manual_it_bootstrap_pages = bootstrap_pages
        def retire_bootstrap(work_page):
            for bootstrap in bootstrap_pages:
                if bootstrap is not work_page and not bootstrap.is_closed() and bootstrap.url == "about:blank":
                    bootstrap.close()
        context.on("page", retire_bootstrap)
        yield context, endpoint


def run(url: str, *, endpoint: str = "") -> dict:
    from ...paths import log_root

    provider = provider_from_url(url)
    evidence = log_root() / "manual_dom_it" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"provider": provider, "real_site": True, "passed": False, "submitted": False}
    app_root = Path(__file__).resolve().parents[4]
    env = dict(os.environ, SMARTAGENT_TEST_NO_PAUSE="1", PYTHONIOENCODING="utf-8")
    try:
        with driver_context(provider, endpoint) as (context, endpoint):
            existing_pages = [page for page in context.pages if page not in context._manual_it_bootstrap_pages]
            args = ["cmd.exe", "/c", str(app_root / "install_smart_agent/launchers/adapterUI.bat"), "--manual", "--url", url,
                    "--cdp-endpoint", endpoint]
            # The real CLI installs the picker and owns the save/polling lifecycle.
            with context.expect_page(timeout=60000) as opened:
                process = subprocess.Popen(args, cwd=app_root, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, encoding="utf-8")
            page = opened.value
            panel = page.locator("#smartagent-composer-picker")
            panel.wait_for(state="visible", timeout=60000)
            # Generic editable semantics identify a test click target, not a saved selector.
            candidates = page.locator('textarea:visible,[contenteditable="true"]:visible,[contenteditable="plaintext-only"]:visible')
            candidates.first.wait_for(state="visible", timeout=60000)
            editables = [item for item in candidates.all() if item.is_editable()]
            if len(editables) != 1:
                raise AssertionError(f"live_it_requires_one_visible_composer:{len(editables)}")
            target = editables[0]
            panel.get_by_role("button", name="開始選取", exact=True).click()
            target.click()
            selection = page.evaluate("() => window.__smartAgentComposerPicker.selected")
            if not selection:
                raise AssertionError("live_picker_no_selection")
            same = target.evaluate("(el, selector) => document.querySelector(selector) === el", selection["selector"])
            if not same:
                raise AssertionError("live_picker_selected_wrong_element")
            page.screenshot(path=str(evidence / "01_selected_composer.png"))
            panel.get_by_role("button", name="儲存輸入框").click()
            output, _ = process.communicate(timeout=45)
            (evidence / "adapterUI.log").write_text(output, encoding="utf-8")
            if process.returncode or "RESULT=SAVED_COMPOSER" not in output:
                raise AssertionError(f"adapterUI_exit={process.returncode}: {output[-1800:]}")
            document = load_dom_config(provider)
            if document["elements"]["composer"]["selector"] != selection["selector"]:
                raise AssertionError("manual_cli_saved_selector_mismatch")
            report.update(config_path=str(config_path(provider)), selector=selection["selector"],
                          selected_element_matches=True, manual_batch_completed=True)
            # A separate process and fresh tab exercise the retained writer launcher.
            args = ["cmd.exe", "/c", str(app_root / "install_smart_agent/launchers/writeSendBox.bat"), "--url", url, "--no-wait",
                    "--screenshot", str(evidence / "02_written_sample.png"), "--cdp-endpoint", endpoint]
            completed = subprocess.run(args, cwd=app_root, env=env, capture_output=True, encoding="utf-8", timeout=240)
            (evidence / "writeSendBox.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
            if completed.returncode:
                raise AssertionError(f"writeSendBox_exit={completed.returncode}: {completed.stdout[-1800:]} {completed.stderr[-800:]}")
            if '"roundtrip_match": true' not in completed.stdout or '"runtime_config_loaded": true' not in completed.stdout:
                raise AssertionError("writeSendBox_missing_roundtrip_evidence")
            if any(page.is_closed() for page in existing_pages):
                raise AssertionError("manual_test_closed_preexisting_tab")
            report.update(passed=True, reopened_by_batch=True, roundtrip_match=True,
                          runtime_config_loaded=True, shared_tabs_preserved=True)
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**report, "evidence": str(evidence)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cdp-endpoint", default="")
    arguments = parser.parse_args()
    run(arguments.url, endpoint=arguments.cdp_endpoint)
