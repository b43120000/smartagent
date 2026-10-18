"""Click-only composer calibration. No planner or compatible old DOM required."""
from __future__ import annotations

import argparse
from pathlib import Path
import time

from .dom_config import save_composer
from .factory import normalize_web_conversation_url
from .manual_browser import open_manual_page
from .provider_registry import provider_from_url

PICKER_SCRIPT = Path(__file__).with_name("composer_picker.js").read_text(encoding="utf-8")


def prompt_url(title: str) -> str:
    import tkinter as tk
    from tkinter import simpledialog

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        value = simpledialog.askstring(title, "貼上目標網頁 URL（ChatGPT / Gemini / Claude / M365 / Other URL）：", parent=window)
    finally:
        window.destroy()
    if value is None:
        raise InterruptedError("使用者取消")
    return normalize_web_conversation_url(value)


def install_picker(page) -> None:
    page.add_init_script(script=PICKER_SCRIPT)
    try:
        page.evaluate(PICKER_SCRIPT)
    except Exception as exc:
        # M365 can redirect immediately after DOMContentLoaded. The registered
        # init script installs the picker in the next document.
        if "Execution context was destroyed" not in str(exc):
            raise


def unique_composer(page, selector: str):
    locator = page.locator(selector)
    if locator.count() != 1:
        raise ValueError("composer_not_unique:請重新標記輸入框")
    if not locator.is_visible() or not locator.is_editable():
        raise ValueError("composer_not_editable:請確認已登入且輸入框可用")
    valid = locator.evaluate("""el => el.matches('textarea,input:not([type]),input[type="text"],input[type="search"]') || el.isContentEditable""")
    if not valid:
        raise ValueError("composer_unsupported_type")
    return locator


def save_selection(page, provider: str, selection: dict, *, root: Path | None = None) -> Path:
    if provider_from_url(page.url) != provider:
        raise ValueError("manual_dom_page_provider_changed")
    unique_composer(page, selection["selector"])
    return save_composer(provider, page.url, selection["selector"], selection, root=root)


def select_composer_on_page(page, provider: str, *, timeout: float = 900,
                            completion_message: str = "輸入框設定已儲存。請執行 writeSendBox.bat 驗證輸入。") -> Path | None:
    install_picker(page)
    print(f"[manualAdapterUI] provider={provider}; 請在網頁右上角開始選取輸入框。", flush=True)
    deadline = time.monotonic() + timeout
    while not page.is_closed() and time.monotonic() < deadline:
        try:
            state = page.evaluate("() => {const s=window.__smartAgentComposerPicker; return s ? {action:s.action,selected:s.selected} : null;}")
        except Exception:
            # Login/navigation replaces the execution context; init script reinstalls UI.
            if page.is_closed():
                return None
            page.wait_for_timeout(200)
            continue
        if state is None:
            install_picker(page)
            page.wait_for_timeout(150)
            continue
        if state and state["action"] == "cancel":
            return None
        if state and state["action"] == "save":
            try:
                path = save_selection(page, provider, state["selected"])
            except Exception as exc:
                page.evaluate("message => {const s=window.__smartAgentComposerPicker; s.action=''; s.finish(message,false,true);}", "儲存失敗，請關閉後重試：" + str(exc))
                raise
            print(f"[manualAdapterUI] SAVED={path}", flush=True)
            page.evaluate("message => window.__smartAgentComposerPicker.finish(message)", completion_message)
            return path
        page.wait_for_timeout(150)
    if page.is_closed():
        return None
    raise TimeoutError("manual_composer_selection_timeout")


def capture_composer(url: str, *, endpoint: str = "", timeout: float = 900) -> Path | None:
    url = normalize_web_conversation_url(url)
    with open_manual_page(url, endpoint=endpoint) as page:
        path = select_composer_on_page(page, provider_from_url(url), timeout=timeout)
        if path:
            print("[manualAdapterUI] 下一步：開啟 writeSendBox.bat 驗證輸入。", flush=True)
            page.wait_for_timeout(1500)
        return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="manualAdapterUI — 手動點選輸入框")
    parser.add_argument("--url", default="")
    parser.add_argument("--cdp-endpoint", default="")
    args = parser.parse_args(argv)
    try:
        url = normalize_web_conversation_url(args.url) if args.url else prompt_url("manualAdapterUI — 目標網頁")
        path = capture_composer(url, endpoint=args.cdp_endpoint)
        print("[manualAdapterUI] RESULT=" + ("SAVED_COMPOSER" if path else "CANCELLED"))
        return 0
    except (InterruptedError, KeyboardInterrupt):
        print("[manualAdapterUI] CANCELLED")
        return 0
    except Exception as exc:
        print(f"[manualAdapterUI] FAILED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
