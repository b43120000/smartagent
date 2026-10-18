"""Open URL, load the provider's manual config, write/read sample. Never send."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import time

from ..dom_config import load_dom_config
from ..factory import create_web_ui_for_page, normalize_web_conversation_url
from ..manual_adapter import prompt_url, unique_composer
from ..manual_browser import open_manual_page
from ..provider_registry import provider_from_url
from ..contracts import normalize_ui_text

SAMPLE_TEXT = "SmartAgent 輸入框功能測試：這是一段範本文字。\nDOM config 已載入；本測試只輸入，不送出。"
PROBE_RE = re.compile(r"(?:\[WEBUI_SUBMIT_ID\]\s*WUS-[A-F0-9]{32}\s*\[/WEBUI_SUBMIT_ID\]\s*)?SmartAgent DOM calibration test (DOMPROBE_[A-F0-9]{12})\. Please reply with exactly: \1 OK")


def read_text(locator) -> str:
    return locator.evaluate("el => 'value' in el ? el.value : el.innerText").replace("\r\n", "\n").strip()


def write_sample(page, *, text: str = SAMPLE_TEXT, replace_known_sample: bool = False) -> dict:
    provider = provider_from_url(page.url)
    document = load_dom_config(provider)
    if document is None:
        raise ValueError("manual_composer_config_missing:請先執行 adapterUI.bat 的 manualAdapterUI")
    selector = document["elements"]["composer"]["selector"]
    composer = unique_composer(page, selector)
    # Prove the runtime factory consumes the very same saved selector.
    adapter = create_web_ui_for_page(page, provider=provider)
    if adapter.profile.composer_selector != selector:
        raise AssertionError("runtime_manual_composer_config_not_loaded")
    if composer.evaluate("el => el.tagName.toLowerCase()") == "input":
        text = text.replace("\n", " ")
    previous = adapter.composer_snapshot().semantic_text.strip()
    if previous and not adapter.validate_composer(text).matched and not (
        replace_known_sample and (adapter.validate_composer(SAMPLE_TEXT).matched or PROBE_RE.fullmatch(normalize_ui_text(previous)))
    ):
        raise ValueError("composer_contains_draft:輸入框已有文字，請清空或使用空白對話後重試")
    # Real editors bind their state through focus/input events. Match runtime's
    # keyboard path instead of replacing content through Locator.fill().
    composer.click(timeout=10000)
    composer = unique_composer(page, selector)
    if not composer.evaluate("el => el === document.activeElement || el.contains(document.activeElement)"):
        raise AssertionError("composer_focus_lost")
    if previous:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    page.keyboard.insert_text(text)
    attempts = 1
    # Reacquire after framework rendering; don't read a detached selected node.
    deadline = time.monotonic() + 5
    matched_since = None
    empty_since = None
    while time.monotonic() < deadline:
        unique_composer(page, selector)
        validation = adapter.validate_composer(text)
        if validation.matched:
            matched_since = matched_since or time.monotonic()
            if time.monotonic() - matched_since >= 0.5:
                break
        else:
            matched_since = None
            # M365 may replace its initial editor after the first beforeinput.
            # Reconcile only an editor proven empty; never overwrite partial or
            # different text, and never cross the submit boundary.
            empty = not adapter.composer_snapshot().semantic_text.strip()
            empty_since = (empty_since or time.monotonic()) if empty else None
            if empty_since is not None and time.monotonic() - empty_since >= 0.3 and attempts < 3:
                composer = unique_composer(page, selector)
                composer.click(timeout=10000)
                if adapter.composer_snapshot().semantic_text.strip():
                    raise AssertionError("composer_changed_before_retry")
                if not composer.evaluate("el => el === document.activeElement || el.contains(document.activeElement)"):
                    raise AssertionError("composer_focus_lost")
                page.keyboard.insert_text(text)
                attempts += 1
                empty_since = None
        page.wait_for_timeout(100)
    else:
        raise AssertionError("composer_roundtrip_mismatch")
    return {"passed": True, "provider": provider, "selector": selector,
            "config_revision": document["revision"], "runtime_config_loaded": True,
            "roundtrip_match": True, "matched_view": validation.matched_view,
            "write_attempts": attempts, "reused_sample": bool(previous), "submitted": False}


def verify_composer_on_page(page, *, no_wait: bool = False, screenshot: str = "") -> dict:
    provider = provider_from_url(page.url)
    document = load_dom_config(provider)
    if document is None:
        raise ValueError("manual_composer_config_missing:請先執行 manualAdapterUI")
    selector = document["elements"]["composer"]["selector"]
    print("[writeSendBox] 等待輸入框，必要時請先登入…", flush=True)
    # Allows login and delayed app hydration without old adapter compatibility.
    try:
        page.locator(selector).wait_for(state="visible", timeout=180000)
        result = write_sample(page)
        result["blank_pages_in_context"] = sum(item.url == "about:blank" for item in page.context.pages)
    except Exception as exc:
        from ...paths import log_root
        from ..contracts import text_digest
        report_path = log_root() / "manual_dom_it" / ("write_failure_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        report_path.mkdir(parents=True, exist_ok=True)
        report = {"passed": False, "provider": provider, "selector": selector,
                  "error": f"{type(exc).__name__}: {exc}", "submitted": False}
        try:
            snapshot = create_web_ui_for_page(page).composer_snapshot()
            report.update(primary_length=len(snapshot.primary_text), semantic_length=len(snapshot.semantic_text),
                          primary_digest=text_digest(snapshot.primary_text), semantic_digest=text_digest(snapshot.semantic_text))
            page.screenshot(path=str(report_path / "failure.png"))
        except Exception as capture_error:
            report["capture_error"] = type(capture_error).__name__
        (report_path / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[writeSendBox] DIAGNOSTICS={report_path}", flush=True)
        raise
    has_picker = page.evaluate('''() => {
        const picker=window.__smartAgentComposerPicker;
        if(!picker)return false;
        picker.finish('輸入與讀回測試成功，尚未送出訊息。',true);
        return true;
    }''')
    if screenshot:
        path = Path(screenshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
    print("[writeSendBox] " + json.dumps(result, ensure_ascii=False), flush=True)
    if not no_wait:
        if not has_picker:
            page.evaluate("""() => {
            const button=document.createElement('button');
            button.textContent='輸入測試成功（未送出）— 關閉測試';
            button.style.cssText='position:fixed;top:16px;right:16px;z-index:2147483647;padding:14px;background:#142238;color:white;border:2px solid #22c55e;border-radius:9px;font:14px system-ui;cursor:pointer';
            button.onclick=()=>window.__smartAgentWriteTestClosed=true;
            document.documentElement.append(button);
            }""")
        while not page.is_closed():
            try:
                if page.evaluate("() => window.__smartAgentWriteTestClosed === true || window.__smartAgentComposerPicker?.action === 'cancel'"):
                    break
                page.wait_for_timeout(200)
            except Exception:
                if page.is_closed():
                    break
                raise
    return result



def run(url: str, *, endpoint: str = "", no_wait: bool = False, screenshot: str = "") -> dict:
    url = normalize_web_conversation_url(url)
    if load_dom_config(provider_from_url(url)) is None:
        raise ValueError("manual_composer_config_missing:請先執行 manualAdapterUI")
    with open_manual_page(url, endpoint=endpoint) as page:
        return verify_composer_on_page(page, no_wait=no_wait, screenshot=screenshot)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="writeSendBox — 輸入框功能測試（不送出）")
    parser.add_argument("--url", default="")
    parser.add_argument("--cdp-endpoint", default="")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--screenshot", default="")
    args = parser.parse_args(argv)
    try:
        run(args.url or prompt_url("writeSendBox — 輸入框測試 URL"), endpoint=args.cdp_endpoint,
            no_wait=args.no_wait, screenshot=args.screenshot)
        return 0
    except (InterruptedError, KeyboardInterrupt):
        print("[writeSendBox] CANCELLED")
        return 0
    except Exception as exc:
        print(f"[writeSendBox] FAILED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
