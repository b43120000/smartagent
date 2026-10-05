"""Isolated CDP worker for priority RemoteAgent browser controls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_core.conversation_identity import conversation_id
from agent_core.web_ui import create_web_ui_for_page
from RemoteAgent.snapshot_worker import capture


def find_execution_page(browser, target_url: str):
    """Resolve exactly one page by conversation identity without changing it."""
    target_id = conversation_id(target_url)
    if not target_id:
        raise RuntimeError("remote_control_target_missing")
    matches = [
        page
        for context in browser.contexts
        for page in context.pages
        if not page.is_closed() and conversation_id(page.url) == target_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"remote_control_execution_page_not_unique:matches={len(matches)}"
        )
    return matches[0]


def current_conversation_status(page) -> dict:
    """Read the last visible semantic turn without mutating the conversation."""
    web_ui = create_web_ui_for_page(page)
    last = web_ui.latest_conversation_turn()
    if last is None:
        return {
            "last_message": "",
            "last_role": "",
            "generation_active": False,
        }
    role = last.role
    text = web_ui.extract_final_text(last) if role == "assistant" else last.raw_text
    if not text:
        text = last.raw_text
    return {
        "last_message": text,
        "last_role": role,
        "generation_active": web_ui.generation_active(),
    }


def cancel_current_generation(page) -> dict:
    """Stop only the active WebGPT generation and preserve the page/session."""
    web_ui = create_web_ui_for_page(page)
    for button in web_ui.stop_controls():
        try:
            button.click(timeout=3000)
            return {"message": "WebGPT generation stopped.", "stopped": True}
        except Exception:
            continue
    return {"message": "WebGPT generation was already idle.", "stopped": False}


def execute(browser, target_url: str, command: str, output: Path) -> dict:
    command = str(command or "").strip().casefold()
    if command == "snapshot webgpt":
        return capture(browser, target_url, output)
    if command in {"refresh", "重新整理"}:
        page = find_execution_page(browser, target_url)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        if conversation_id(page.url) != conversation_id(target_url):
            raise RuntimeError("remote_control_refresh_conversation_changed")
        return {
            "message": "WebGPT refreshed without resubmitting the original prompt.",
            "reloaded": True,
        }
    if command == "查看現在工作狀態":
        return current_conversation_status(find_execution_page(browser, target_url))
    if command == "cancel webgpt":
        return cancel_current_generation(find_execution_page(browser, target_url))
    raise ValueError(f"unsupported_remote_control:{command}")


def main() -> int:
    # This worker communicates JSON over captured pipes.  Force the wire
    # encoding instead of inheriting the Windows console code page (for
    # example CP950), because status text can contain arbitrary Unicode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    # This process owns only its Playwright client.  Disconnecting it never
    # closes the shared Chromium browser, context, or WebGPT page.
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp, timeout=10000)
        result = execute(browser, args.url, args.command, Path(args.output))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
