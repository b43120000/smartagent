"""One URL, one page: select composer, save provider config, verify input."""
from __future__ import annotations

import argparse
from .factory import normalize_web_conversation_url
from .manual_adapter import prompt_url, select_composer_on_page
from .manual_browser import open_manual_page
from .provider_registry import provider_from_url
from .tests.write_send_box import verify_composer_on_page


def calibrate_on_page(page, *, provider: str = "", no_wait: bool = False, screenshot: str = "") -> dict | None:
    provider = provider or provider_from_url(page.url)
    path = select_composer_on_page(page, provider,
                                  completion_message="輸入框已儲存，正在自動驗證輸入…")
    if path is None:
        return None
    # Selection has finished. Remove its overlay before exercising the editor.
    page.evaluate("() => window.__smartAgentComposerPicker.hide()")
    print("[writeSendBox] 已儲存輸入框，開始在同一頁驗證…", flush=True)
    result = verify_composer_on_page(page, no_wait=no_wait, screenshot=screenshot)
    return {**result, "config_path": str(path), "verified_in_same_page": True}


def run(url: str, *, endpoint: str = "", no_wait: bool = False, screenshot: str = "") -> dict | None:
    url = normalize_web_conversation_url(url)
    with open_manual_page(url, endpoint=endpoint) as page:
        return calibrate_on_page(page, provider=provider_from_url(url), no_wait=no_wait, screenshot=screenshot)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="writeSendBox — 點選輸入框、儲存與驗證一次完成")
    parser.add_argument("--url", default="")
    parser.add_argument("--cdp-endpoint", default="")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--screenshot", default="")
    args = parser.parse_args(argv)
    try:
        url = args.url or prompt_url("writeSendBox — 輸入框校準與驗證")
        result = run(url, endpoint=args.cdp_endpoint, no_wait=args.no_wait, screenshot=args.screenshot)
        if result:
            print(f"[writeSendBox] CONFIG={result['config_path']}")
            print("[writeSendBox] RESULT=CALIBRATED_AND_VERIFIED")
        else:
            print("[writeSendBox] RESULT=CANCELLED")
        return 0
    except (InterruptedError, KeyboardInterrupt):
        print("[writeSendBox] RESULT=CANCELLED")
        return 0
    except Exception as exc:
        print(f"[writeSendBox] FAILED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
