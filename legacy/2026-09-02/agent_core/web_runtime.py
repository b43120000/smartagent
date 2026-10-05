#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_core/web_runtime.py
用真實瀏覽器操作 ChatGPT / Gemini 網頁版，繞過 API 限制。
使用者需要在瀏覽器中登入一次後，此模組會重用登入狀態（Cookie/Session）。

支援：
  - ChatGPT (chatgpt.com)
  - Gemini  (gemini.google.com)

使用方式：
  from web_llm_scraper import WebLLMScraper

  scraper = WebLLMScraper(service="chatgpt", headless=False)
  scraper.start()
  reply = scraper.ask("你好，幫我寫個冒泡排序")
  print(reply)
  scraper.close()
"""

import time
import json
import hashlib
import os
import sys
import traceback
import threading
import re
import urllib.request
from pathlib import Path
from typing import Optional

try:
    from .webgpt_rate_governor import WebGPTRateGovernor
except ImportError:  # standalone execution compatibility
    from webgpt_rate_governor import WebGPTRateGovernor

try:
    from .process_file_lock import exclusive_process_lock
except ImportError:  # standalone execution compatibility
    from process_file_lock import exclusive_process_lock

try:
    from .artifact_transfer import (
        download_latest_artifact, download_latest_artifact_with_evidence, file_evidence,
        snapshot_artifact_signatures, has_fresh_artifact,
    )
except ImportError:  # standalone execution compatibility
    from artifact_transfer import (
        download_latest_artifact, download_latest_artifact_with_evidence, file_evidence,
        snapshot_artifact_signatures, has_fresh_artifact,
    )

# ─── Chromium user data dir (保留登入 Cookie) ────────────────────────────────
# 每個 service 用獨立的 profile，避免 session 衝突
PROFILE_DIR = Path(os.environ.get("APPDATA", Path.home())) / "WebLLMScraper"
DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / ".agents" / "web_llm_scraper_debug.log"
CHATGPT_CDP_PORT = 1272
CHATGPT_CDP_ENDPOINT = f"http://127.0.0.1:{CHATGPT_CDP_PORT}"
CANONICAL_PAGE_MARKER_PREFIX = "SMARTAGENT_CANONICAL_CONVERSATION:"
REMOTE_PAGE_START_LOCK = Path(__file__).resolve().parent.parent / ".agents" / "remote_page_start.lock"


def _debug_log(message: str) -> None:
    """Append timestamped scraper diagnostics without recording prompt contents."""
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        thread_name = threading.current_thread().name
        line = f"[{stamp}] [{thread_name}] {message}"
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


SERVICE_CONFIG = {
    "chatgpt": {
        "url":          "https://chatgpt.com/",
        "new_chat_url": "https://chatgpt.com/",
        # 輸入框
        "input_selector":    "#prompt-textarea",
        # 發送按鈕（輸入後按 Enter 或點擊）
        "send_by_enter":     True,
        # Composer / conversation state selectors.  Completion is NOT inferred
        # from any one selector; ask() also requires a new user turn, a new
        # assistant turn, generation-inactive state, and stable new response.
        "done_selector":     "button[data-testid='send-button']",
        "stop_selector":     "button[aria-label='Stop streaming']",
        "stop_selectors": [
            "button[data-testid='stop-button']",
            "button[aria-label='Stop streaming']",
            "button[aria-label='Stop responding']",
            "button[aria-label*='Stop']",
        ],
        "user_turn_selector": "[data-message-author-role='user']",
        "assistant_turn_selector": "[data-message-author-role='assistant']",
        # Final answer content inside the assistant turn.  Thinking placeholders
        # generally do not contain .markdown, which prevents premature return.
        "response_selector": ".markdown, [data-message-author-role='assistant'] .markdown",
        "profile_subdir":    "chatgpt",
    },
    "gemini": {
        "url":          "https://gemini.google.com/app",
        "new_chat_url": "https://gemini.google.com/app",
        "input_selector":    "rich-textarea .ql-editor",
        "send_by_enter":     True,
        "done_selector":     "button.send-button",
        "stop_selector":     "button.stop-button",
        "stop_selectors":    ["button.stop-button"],
        "user_turn_selector": "user-query",
        "assistant_turn_selector": "model-response",
        "response_selector": "model-response .markdown",
        "profile_subdir":    "gemini",
    },
}


class WebScraperStageError(RuntimeError):
    """Web UI failure with enough stage information to decide retry safety.

    ``safe_to_retry`` is True only when the current prompt is known not to have
    reached the submit boundary.  Once submit is attempted, callers must never
    automatically resend the same prompt because delivery may already have
    succeeded even if acknowledgement/response detection later fails.
    """

    def __init__(self, message: str, *, stage: str, safe_to_retry: bool):
        super().__init__(message)
        self.stage = stage
        self.safe_to_retry = bool(safe_to_retry)

class WebLLMScraper:
    """Browser-based LLM scraper that reuses browser login sessions."""

    def __init__(
        self,
        service: str = "chatgpt",
        headless: bool = False,
        timeout_sec: int = 120,
        slow_mo: int = 50,
    ):
        if service not in SERVICE_CONFIG:
            raise ValueError(f"service must be one of: {list(SERVICE_CONFIG.keys())}")

        self.service = service
        self.cfg = SERVICE_CONFIG[service]
        self.headless = headless
        self.timeout = timeout_sec * 1000  # playwright uses ms
        self.slow_mo = slow_mo

        self._pw = None
        self._browser = None
        self._page = None
        self._cdp_browser = None
        self._attached_over_cdp = False
        self._owns_attached_page = False
        self._isolated_browser = None
        self._profile_dir = PROFILE_DIR / self.cfg["profile_subdir"]
        self._profile_dir.mkdir(parents=True, exist_ok=True)

        # Generation lifecycle control. There is deliberately no normal hard
        # wall-clock timeout for a request. Two watchdog tiers are used instead:
        #   * inactive/no-final stall: relatively short, because WebGPT no longer
        #     claims to be working and no final content is emerging;
        #   * active/media emergency stall: much longer, because image generation
        #     and deep reasoning can legitimately keep the same visible DOM for
        #     several minutes while the Stop control still says work is active.
        self._cancel_requested = threading.Event()
        self._generation_stall_sec = max(180.0, float(timeout_sec))
        self._active_generation_warn_sec = max(300.0, self._generation_stall_sec)
        self._active_generation_emergency_sec = max(1200.0, self._generation_stall_sec * 4.0)
        self._completion_stable_sec = 1.25
        # UI lifecycle is the first hard gate.  SmartAgent must not inspect or
        # trust turn_commit while ChatGPT is still thinking/generating/processing
        # media.  Require a continuously quiet assistant UI window before the
        # protocol layer is even allowed to inspect smartagent_tool JSON.
        self._ui_idle_grace_sec = 5.0
        # Once UI_CONFIRMED_IDLE is reached, a matching turn_commit becomes the
        # final protocol commit gate.  This short second debounce only protects
        # against a response changing immediately after the commit was parsed.
        self._protocol_commit_stable_sec = 1.0
        # ACK timeout begins only after UI_CONFIRMED_IDLE, never while image/file
        # generation is legitimately still active.
        self._protocol_commit_timeout_sec = 600.0
        # Passive-first protocol recovery.  For media/tool turns ChatGPT may
        # finish the image/file UI without appending the requested text ACK.
        # Never probe immediately: first require a long, continuously quiet UI
        # window, then allow at most one same-turn ACK-only retransmission.
        self._protocol_recovery_quiet_sec = 30.0
        self._protocol_recovery_max_attempts = 1
        # A failed recovery is reported after one short, quiet observation;
        # there is no second hidden correction turn.
        self._protocol_recovery_failure_quiet_sec = 5.0
        self._activity_observer_installed = False
        # Stage 3.1: scope artifact downloads to the most recent ask() request.
        self._last_artifact_scope: Optional[dict] = None
        self._status_callback = None
        self._rate_limited_until = 0.0
        self._rate_limit_dialog_dismissed_at = 0.0
        self._rate_limit_dialog_repeat_window_sec = 120.0
        self._rate_limit_dialog_streak = 0
        self._rate_governor = None
        self._request_state = "READY_IDLE"
        self._pending_submit_context = None
        self._submit_click_attempted = False

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback

    def _emit_status(self, stage: str, **kwargs):
        callback = getattr(self, "_status_callback", None)
        if not callback:
            return None
        try:
            return callback(stage, **kwargs)
        except Exception:
            return None

    def _capture_agent2_evidence(self, label: str) -> str:
        """Capture the browser viewport for Agent 2/CV adapters and auditing."""
        if self._page is None:
            return ""
        try:
            evidence_dir = Path(__file__).resolve().parent.parent / ".agents" / "browser_operator"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(label or "ui"))[:40]
            target = evidence_dir / f"{int(time.time() * 1000)}_{safe_label}.png"
            self._page.screenshot(path=str(target), full_page=False)
            return str(target)
        except Exception:
            return ""

    def _request_agent2_action(
        self, *, task_phase: str, observed_state: str, error_code: str,
        detail: str = "", retryable: bool = True, retry_budget: int = 1,
        primary_method: str = "DOM_EVENT",
    ) -> str:
        """Ask Agent 2 for one non-visual, whitelisted recovery action."""
        diagnostic = {
            "operation": str(task_phase or ""),
            "observed_state": str(observed_state or ""),
            "error_code": str(error_code or ""),
            "service": str(getattr(self, "service", "") or ""),
            "page_url": str(getattr(getattr(self, "_page", None), "url", "") or "")[:1200],
            "input_selector": str(getattr(self, "cfg", {}).get("input_selector", "") or "")[:500],
            "send_selector": str(getattr(self, "cfg", {}).get("done_selector", "") or "")[:500],
            "caller_detail": str(detail or "")[:3000],
        }
        try:
            diagnostic["page_title"] = str(self._page.title() or "")[:500] if self._page is not None else ""
        except Exception:
            diagnostic["page_title"] = ""
        try:
            diagnostic["screenshot"] = self._capture_agent2_evidence(error_code)
        except Exception:
            diagnostic["screenshot"] = ""
        evidence_detail = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
        result = self._emit_status(
            "UI_ESCALATION_REQUIRED", message=f"Agent 2 檢查 {task_phase} UI",
            task_phase=task_phase, observed_state=observed_state,
            ui_confidence="UNCERTAIN", primary_method=primary_method,
            error_code=error_code, retryable=retryable,
            retry_budget=retry_budget, detail=evidence_detail,
        )
        # Keep the deterministic pre-Agent2 behavior when no status/controller
        # callback is installed (unit harnesses and standalone scraper use).
        fallback = "RETRY_ONCE" if retryable and retry_budget > 0 else "SAFE_STOP"
        action = str(result.get("action", fallback) if isinstance(result, dict) else fallback).upper()
        if action not in {"INSPECT_ONLY", "PRESS_ESCAPE", "DISMISS_DIALOG", "RETRY_ONCE", "SAFE_STOP"}:
            action = "SAFE_STOP"
        self._log_stage("agent2_action", f"phase={task_phase} error={error_code} action={action}")
        return action

    def _apply_agent2_ui_action(self, action: str) -> bool:
        """Execute one safe UI mutation. RETRY_ONCE is performed by the caller."""
        action = str(action or "SAFE_STOP").upper()
        if action == "PRESS_ESCAPE":
            try:
                self._page.keyboard.press("Escape")
                return True
            except Exception:
                return False
        if action == "DISMISS_DIALOG":
            return bool(self._dismiss_known_blocking_dialogs())
        if action == "INSPECT_ONLY":
            return True
        return action == "RETRY_ONCE"

    @staticmethod
    def _status_from_web_stage(stage: str, detail: str = "") -> tuple[str, str]:
        key = str(stage or "").lower()
        if key in {"composer_before_write", "composer_insert_begin", "composer_insert_returned", "composer_verified", "composer_ready", "composer_claimed", "send_ready"}:
            return "PREPARING_PROMPT", "準備傳送訊息"
        if key == "attachment_state":
            return (("ATTACHMENT_READY", "附件已就緒") if "state=READY" in detail
                    else ("UPLOADING_ATTACHMENT", "上傳附件中"))
        if key in {"submit_begin", "submit_returned", "composer_immediate_post_submit"}:
            return "SUBMITTING", "傳送訊息中"
        if key in {"user_sent", "user_sent_composer"}:
            return "WAITING_BRAIN", "等待 ChatGPT 開始回覆"
        if key == "assistant_started":
            return "BRAIN_THINKING", "ChatGPT 思考中"
        if key == "generating":
            image_match = re.search(r"images=(\d+)/(\d+)", detail)
            if image_match and int(image_match.group(2)) > 0:
                return "BRAIN_GENERATING_IMAGE", "ChatGPT 圖片生成中"
            text_match = re.search(r"text_len=(\d+)", detail)
            # ChatGPT renders short localized placeholders (for example
            # "思考中") inside the assistant turn while it is still thinking.
            # Do not report those few characters as an actual streamed reply.
            if text_match and int(text_match.group(1)) > 8:
                return "BRAIN_RESPONDING", "ChatGPT 回覆生成中"
            return "BRAIN_THINKING", "ChatGPT 思考中"
        if key == "image_generating":
            return "BRAIN_GENERATING_IMAGE", "ChatGPT 圖片生成中"
        if key == "media_processing":
            return "BRAIN_RESPONDING", "ChatGPT 媒體處理中"
        if key in {"ui_idle_settling", "ui_confirmed_idle", "settling", "protocol_commit_settling"}:
            return "VALIDATING_PROTOCOL", "驗證 ChatGPT 回覆"
        if key in {"protocol_commit_complete", "complete"}:
            return "RESPONSE_RECEIVED", "已收到 ChatGPT 回覆"
        if "failed" in key or key in {"timeout", "emergency_stalled"}:
            return "FAILED", "ChatGPT 網頁操作失敗"
        return "", ""

    def start(self, show_browser: bool = True):
        """Launch browser with persistent context (keeps login cookies)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )

        print(f"[WebScraper] 啟動瀏覽器 → {self.service} (profile: {self._profile_dir})")
        self._pw = sync_playwright().start()

        # Use persistent context = saves cookies between sessions.  Visible
        # Chromium must use its native viewport so ChatGPT responds to every
        # user resize instead of remaining locked to a fixed CSS viewport.
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # Persistent Chromium profiles can retain an unclean-exit marker
            # when Agent0 or the host is restarted.  The browser chrome bubble
            # is outside the page DOM, so Playwright cannot dismiss it later.
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
        ]
        isolated_worker = (
            self.service == "chatgpt"
            and os.environ.get("SMARTAGENT_ISOLATED_BROWSER", "") == "1"
        )
        if self.service == "chatgpt" and not isolated_worker:
            # Stage 7: expose the authenticated Chromium context on localhost so
            # RemoteAgent-0 can attach without a second browser/profile.
            browser_args.extend([
                f"--remote-debugging-port={CHATGPT_CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
            ])
        if self.headless:
            viewport = {"width": 1280, "height": 900}
        else:
            browser_args.append("--start-maximized")
            # None disables Playwright's emulated fixed viewport. Chromium then
            # reports the actual content area and updates it as the window moves
            # or resizes, allowing the page's responsive UI to reflow normally.
            viewport = None

        attach_cdp = (
            self.service == "chatgpt"
            and not isolated_worker
            and os.environ.get("SMARTAGENT_ATTACH_CDP", "") == "1"
        )
        if isolated_worker:
            state_path = Path(os.environ.get("SMARTAGENT_REMOTE_BROWSER_STATE", "")).resolve()
            if not state_path.is_file():
                raise RuntimeError("isolated worker browser state missing")
            try:
                storage_state = json.loads(state_path.read_text(encoding="utf-8"))
            finally:
                try:
                    state_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not isinstance(storage_state, dict):
                raise RuntimeError("isolated worker browser state invalid")
            self._log_stage("isolated_browser_start", f"task={os.environ.get('SMARTAGENT_REMOTE_WORKER_TASK', '')}")
            self._emit_status("CDP_CONNECTING", message="啟動 Agent1 獨立瀏覽器", detail="isolated_process")
            self._isolated_browser = self._pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=browser_args,
            )
            self._browser = self._isolated_browser.new_context(
                storage_state=storage_state,
                ignore_https_errors=True,
                **({"viewport": viewport} if self.headless else {"no_viewport": True}),
            )
        elif attach_cdp:
            endpoint = os.environ.get("SMARTAGENT_CHATGPT_CDP", CHATGPT_CDP_ENDPOINT)
            self._log_stage("cdp_connecting", f"endpoint={endpoint}")
            self._emit_status("CDP_CONNECTING", message="連線共用瀏覽器", detail=endpoint)
            self._cdp_browser = self._pw.chromium.connect_over_cdp(endpoint)
            if not self._cdp_browser.contexts:
                raise RuntimeError("CDP browser has no context")
            self._browser = self._cdp_browser.contexts[0]
            self._attached_over_cdp = True
        else:
            self._browser = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=browser_args,
                ignore_https_errors=True,
                **({"viewport": viewport} if self.headless else {"no_viewport": True}),
            )

            # LocalAgent owns its persistent profile and foreground page.
            pages = self._browser.pages
            self._page = pages[0] if pages else self._browser.new_page()

        # Chromium/CDP can accept concurrent clients, but creating and navigating
        # several fresh targets at the same instant is not reliable. Serialize
        # only this short bootstrap section; once each page commits, Agent1
        # workers continue concurrently on their own marked target.
        startup_lock = (
            exclusive_process_lock(
                REMOTE_PAGE_START_LOCK, timeout_sec=120.0,
                label="RemoteAgent page startup", legacy_kind="remote-page-start-v1",
            ) if attach_cdp else None
        )
        if attach_cdp:
            self._log_stage("page_start_wait", f"lock={REMOTE_PAGE_START_LOCK}")
            self._emit_status("PAGE_START_WAIT", message="等待配置獨立網頁")
        from contextlib import nullcontext
        with (startup_lock if startup_lock is not None else nullcontext()):
            isolated_page = attach_cdp or isolated_worker
            startup_attempts = 3 if isolated_page else 1
            for startup_attempt in range(startup_attempts):
                try:
                    if isolated_page:
                        marker = os.environ.get("SMARTAGENT_REMOTE_WORKER_TASK", "REMOTE_WORKER")
                        target_id=self._conversation_id_from_url(self.cfg.get("url",""))
                        matches=[page for page in list(self._browser.pages) if self._conversation_id_from_url(str(getattr(page,"url","") or ""))==target_id] if target_id else []
                        if matches:
                            matches.sort(
                                key=lambda page: not self._page_has_canonical_marker(
                                    page, target_id
                                )
                            )
                            self._page=matches[0]
                            self._owns_attached_page=False
                            for duplicate in matches[1:]:
                                try: duplicate.close(run_before_unload=False)
                                except Exception: pass
                        else:
                            self._page = self._browser.new_page()
                            self._owns_attached_page=True
                            self._mark_page_canonical(self._page, target_id)
                        self._log_stage(
                            "page_starting",
                            f"attempt={startup_attempt + 1} pages={len(self._browser.pages)} marker={marker} isolated={isolated_worker}",
                        )
                        self._emit_status(
                            "PAGE_STARTING", message="建立獨立網頁",
                            progress={"current": startup_attempt + 1, "total": startup_attempts},
                        )
                    target_id = self._conversation_id_from_url(self.cfg.get("url", ""))
                    current_id = self._conversation_id_from_url(
                        str(getattr(self._page, "url", "") or "")
                    )
                    if not target_id or current_id != target_id:
                        self._page.goto(
                            self.cfg["url"],
                            wait_until="commit" if attach_cdp else "domcontentloaded",
                            timeout=30000,
                        )
                    time.sleep(2)
                    if isolated_page:
                        page_url = str(getattr(self._page, "url", "") or "")
                        if isolated_worker and (
                            not page_url.startswith(("https://", "http://"))
                            or not self._is_logged_in()
                        ):
                            raise RuntimeError(
                                f"isolated_worker_page_not_ready: url={page_url or 'EMPTY'}"
                            )
                        self._log_stage("page_ready", f"url={page_url}")
                        self._emit_status("PAGE_READY", message="獨立網頁已就緒", detail=page_url)
                    if self.service == "chatgpt" and target_id:
                        self._mark_page_canonical(self._page, target_id)
                    break
                except Exception as exc:
                    self._log_stage(
                        "cdp_startup_retry" if startup_attempt + 1 < startup_attempts else "page_start_failed",
                        f"attempt={startup_attempt + 1} error={type(exc).__name__}: {exc}",
                    )
                    if (
                        isolated_page
                        and self._page is not None
                        and self._owns_attached_page
                    ):
                        try:
                            self._page.close(run_before_unload=False)
                        except Exception:
                            pass
                    if isolated_page:
                        self._page = None
                        self._owns_attached_page = False
                    if isolated_page and startup_attempt + 1 < startup_attempts:
                        time.sleep(1.5 * (startup_attempt + 1))
                        continue
                    if attach_cdp:
                        try:
                            self._pw.stop()
                        except Exception:
                            pass
                        self._pw = None
                    raise

        self._dismiss_known_blocking_dialogs()

        # Check if logged in
        if not self._is_logged_in():
            if isolated_worker:
                raise RuntimeError("isolated worker authentication state rejected by ChatGPT")
            print(f"\n[!] 尚未登入 {self.service}！")
            print(f"    請在開啟的瀏覽器視窗中手動完成登入...")
            while not self._is_logged_in():
                time.sleep(3)
                print("    等待登入完成...")
            print("    偵測到登入完成！繼續執行...")
            self._page.goto(self.cfg["url"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

        print(f"[WebScraper] 已連接 {self.service}")

    def get_conversation_display_name(self) -> str:
        """Best-effort visible ChatGPT conversation name for startup menus."""
        if self._page is None:
            return ""
        selectors = [
            '[data-testid="conversation-title"]',
            'nav a[aria-current="page"]',
            'aside a[aria-current="page"]',
            'h1',
        ]
        for selector in selectors:
            try:
                locator = self._page.locator(selector)
                for idx in range(locator.count() - 1, -1, -1):
                    item = locator.nth(idx)
                    if not item.is_visible():
                        continue
                    value = (item.inner_text() or "").strip()
                    if value and len(value) <= 160 and value.lower() not in {"chatgpt", "new chat"}:
                        return value
            except Exception:
                continue
        try:
            title = (self._page.title() or "").strip()
        except Exception:
            title = ""
        for suffix in (" - ChatGPT", " | ChatGPT", " — ChatGPT"):
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()
        if title.lower() in {"", "chatgpt", "new chat"}:
            return ""
        return title[:160]

    def get_conversation_display_name_for_url(self, conversation_url: str) -> str:
        """Read a saved conversation title without navigating away from the active page."""
        if self._page is None or self.service != "chatgpt":
            return ""
        match = re.search(r"/c/([0-9a-fA-F-]{16,})", str(conversation_url or ""))
        if not match:
            return ""
        conversation_id = match.group(1)
        try:
            result = self._page.evaluate(
                """async (conversationId) => {
                    const response = await fetch(
                        `/backend-api/conversation/${encodeURIComponent(conversationId)}`,
                        {credentials: 'include'}
                    );
                    if (!response.ok) return '';
                    const payload = await response.json();
                    return typeof payload?.title === 'string' ? payload.title.trim() : '';
                }""",
                conversation_id,
            )
        except Exception:
            return ""
        title = str(result or "").strip()
        return title[:160] if title else ""

    @staticmethod
    def _conversation_id_from_url(conversation_url: str) -> str:
        match = re.search(r"/c/([0-9a-zA-Z-]{3,})", str(conversation_url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _page_has_canonical_marker(page, conversation_id: str) -> bool:
        if not conversation_id:
            return False
        try:
            marker = str(page.evaluate("() => window.name") or "")
        except Exception:
            return False
        return marker == CANONICAL_PAGE_MARKER_PREFIX + conversation_id

    @staticmethod
    def _mark_page_canonical(page, conversation_id: str) -> None:
        if page is None or not conversation_id:
            return
        try:
            page.evaluate(
                "value => { window.name = value; }",
                CANONICAL_PAGE_MARKER_PREFIX + conversation_id,
            )
        except Exception:
            pass

    @staticmethod
    def _project_url_for_conversation(conversation_url: str) -> str:
        target = str(conversation_url or "").strip()
        marker = target.find("/c/")
        if marker < 0:
            return ""
        prefix = target[:marker].rstrip("/")
        return f"{prefix}/project" if "/g/" in prefix else ""

    def _conversation_ready(self, conversation_id: str, timeout_sec: float = 6.0) -> bool:
        """Require both the exact conversation route and a usable composer."""
        if self._page is None or not conversation_id:
            return False
        deadline = time.monotonic() + max(0.2, float(timeout_sec))
        while time.monotonic() < deadline:
            self._dismiss_known_blocking_dialogs()
            current_id = self._conversation_id_from_url(str(self._page.url or ""))
            if current_id == conversation_id:
                try:
                    composer = self._page.locator(self.cfg["input_selector"])
                    if composer.count() and composer.first.is_visible():
                        return True
                except Exception:
                    pass
            time.sleep(0.25)
        return False

    def _click_project_conversation_link(self, conversation_id: str) -> bool:
        """Open a project-managed conversation through its visible project card."""
        if self._page is None or not conversation_id:
            return False
        try:
            links = self._page.locator(f'a[href*="/c/{conversation_id}"]')
            for idx in range(links.count()):
                link = links.nth(idx)
                if link.is_visible():
                    link.click(force=True, no_wait_after=True, timeout=5000)
                    self._log_stage("conversation_project_link_clicked", conversation_id)
                    return True
        except Exception as exc:
            self._log_stage("conversation_project_link_failed", type(exc).__name__)
        return False

    def navigate_to_conversation(self, conversation_url: str) -> None:
        """Navigate to one exact conversation and fail closed on project redirects."""
        target = str(conversation_url or "").strip()
        if not target or self._page is None:
            return
        conversation_id = self._conversation_id_from_url(target)
        if not conversation_id:
            raise ValueError(f"ChatGPT conversation URL 缺少 /c/<conversation_id>: {target}")

        if self._conversation_ready(conversation_id, timeout_sec=1.0):
            return

        self._dismiss_known_blocking_dialogs()
        self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if self._conversation_ready(conversation_id):
            self._log_stage("conversation_navigation_ready", conversation_id)
            return

        # ChatGPT can probabilistically redirect project conversations to the
        # project directory.  Resolve the exact conversation card and click it
        # through the SPA before one final direct retry.
        project_url = self._project_url_for_conversation(target)
        if project_url and "/project" not in str(self._page.url or ""):
            self._page.goto(project_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1)
        if self._click_project_conversation_link(conversation_id):
            if self._conversation_ready(conversation_id, timeout_sec=8.0):
                self._log_stage("conversation_navigation_ready", f"{conversation_id} via_project")
                return

        self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
        if self._conversation_ready(conversation_id):
            self._log_stage("conversation_navigation_ready", f"{conversation_id} retry")
            return

        current = str(self._page.url or "")
        self._log_stage("conversation_navigation_failed", f"target={conversation_id} current={current}")
        action = self._request_agent2_action(
            task_phase="NAVIGATION", observed_state=(
                "PROJECT_INDEX_PAGE" if "/project" in current else "CONVERSATION_NOT_VERIFIED"
            ), error_code="NAVIGATION_REDIRECTED",
            detail=f"target={conversation_id} current={current}", retry_budget=1,
        )
        if action in {"PRESS_ESCAPE", "DISMISS_DIALOG"}:
            self._apply_agent2_ui_action(action)
        if action in {"RETRY_ONCE", "PRESS_ESCAPE", "DISMISS_DIALOG", "INSPECT_ONLY"}:
            if action != "INSPECT_ONLY":
                self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
            if self._conversation_ready(conversation_id, timeout_sec=8.0):
                self._log_stage("conversation_navigation_ready", f"{conversation_id} agent2")
                return
        raise RuntimeError(
            f"conversation_navigation_failed: target={conversation_id} current={current}"
        )

    def get_cdp_endpoint(self, timeout_sec: float = 8.0) -> str:
        """Return the verified localhost CDP endpoint for this ChatGPT runtime."""
        if self.service != "chatgpt":
            raise RuntimeError("CDP endpoint is only enabled for the ChatGPT runtime")
        if self._browser is None:
            raise RuntimeError("ChatGPT browser is not running")

        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        version_url = CHATGPT_CDP_ENDPOINT + "/json/version"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(version_url, timeout=1.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
                if websocket_url.startswith("ws://127.0.0.1:"):
                    return CHATGPT_CDP_ENDPOINT
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise RuntimeError(
            f"ChatGPT CDP endpoint unavailable at {CHATGPT_CDP_ENDPOINT}: {last_error}"
        )


    def _dismiss_known_blocking_dialogs(self) -> bool:
        """Dismiss known transient ChatGPT overlays that block composer/download UI.

        Currently targets the observed "太多要求" / "Too many requests" modal.
        The guard is conservative: it clicks an acknowledgement button only when
        the visible dialog itself contains a known blocking phrase.
        """
        if self._page is None or self.service != "chatgpt":
            return False
        phrases = ("太多要求", "too many requests")
        button_names = ("知道了", "got it", "ok", "okay")
        roots = []
        for selector in ('[role="dialog"]', '[data-testid*="modal"]', '[class*="modal"]'):
            try:
                roots.extend(self._page.query_selector_all(selector))
            except Exception:
                pass
        for root in roots:
            try:
                if not root.is_visible():
                    continue
                text = str(root.inner_text() or "").strip()
            except Exception:
                continue
            low = text.lower()
            if not any(token in low for token in phrases):
                continue
            try:
                buttons = list(root.query_selector_all('button, [role="button"]'))
            except Exception:
                buttons = []
            for button in buttons:
                try:
                    if not button.is_visible():
                        continue
                    label = " ".join(filter(None, [
                        str(button.inner_text() or "").strip(),
                        str(button.get_attribute("aria-label") or "").strip(),
                    ])).lower()
                    if any(name in label for name in button_names):
                        button.click()
                        self._record_rate_limit_dialog_dismissal(text[:500])
                        self._log_stage("blocking_dialog_dismissed", text[:160].replace("\n", " "))
                        time.sleep(0.15)
                        return True
                except Exception:
                    continue

        # ChatGPT occasionally renders the same blocker without role=dialog.
        # Only use the page-global button fallback when the blocker phrase itself
        # is visibly present in the page text.
        try:
            body_text = str(self._page.locator("body").inner_text(timeout=1000) or "")
        except Exception:
            body_text = ""
        if any(token in body_text.lower() for token in phrases):
            for selector in ('button', '[role="button"]'):
                try:
                    buttons = list(self._page.query_selector_all(selector))
                except Exception:
                    buttons = []
                for button in buttons:
                    try:
                        if not button.is_visible():
                            continue
                        label = " ".join(filter(None, [
                            str(button.inner_text() or "").strip(),
                            str(button.get_attribute("aria-label") or "").strip(),
                        ])).lower()
                        if any(name in label for name in button_names):
                            button.click()
                            self._record_rate_limit_dialog_dismissal(
                                "page-global too-many-requests blocker"
                            )
                            self._log_stage("blocking_dialog_dismissed", "page-global too-many-requests blocker")
                            time.sleep(0.15)
                            return True
                    except Exception:
                        continue
        return False

    def _record_rate_limit_dialog_dismissal(self, detail: str, *, now: float | None = None) -> bool:
        """Dismiss and count dialogs; trip cooldown only at the configured threshold."""
        now = time.time() if now is None else float(now)
        previous = float(getattr(self, "_rate_limit_dialog_dismissed_at", 0.0) or 0.0)
        window = float(getattr(self, "_rate_limit_dialog_repeat_window_sec", 120.0) or 120.0)
        consecutive = bool(previous and 0.0 <= now - previous <= window)
        local_streak = (int(getattr(self, "_rate_limit_dialog_streak", 0) or 0) + 1) if consecutive else 1
        self._rate_limit_dialog_streak = local_streak
        self._rate_limit_dialog_dismissed_at = now
        governor = getattr(self, "_rate_governor", None)
        if governor is not None:
            result = governor.record_rate_limit(detail)
            triggered = bool(result.get("triggered", False))
            streak = int(result.get("streak", local_streak) or local_streak)
            threshold = int(result.get("threshold", 5) or 5)
            cooldown_until = float(result.get("cooldown_until", 0.0) or 0.0)
        else:
            threshold = max(1, int(os.environ.get("SMARTAGENT_WEBGPT_DISMISSALS_BEFORE_COOLDOWN", "5")))
            streak = local_streak
            triggered = streak >= threshold
            cooldown_until = now + 600.0 if triggered else 0.0
        if triggered:
            self._rate_limited_until = max(
                float(getattr(self, "_rate_limited_until", 0.0) or 0.0), cooldown_until
            )
            message = f"ChatGPT 限流提示已連續關閉 {streak} 次，啟動全域冷卻"
            stage = "UI_ESCALATION_REQUIRED"
            observed_state = "RATE_LIMITED"
            error_code = "RATE_LIMITED"
            retry_budget = 0
        else:
            message = f"已關閉 ChatGPT 限流提示（{streak}/{threshold}），尚未進入冷卻"
            stage = "UI_ESCALATION_RESULT"
            observed_state = "RATE_LIMIT_DIALOG_DISMISSED"
            error_code = "RATE_LIMIT_DIALOG_DISMISSED"
            retry_budget = 1
        print(f"  [WebGPT Governor] {message}", flush=True)
        self._emit_status(
            stage, message=message,
            task_phase="COMPOSER", observed_state=observed_state,
            ui_confidence="CONFIRMED", primary_method="DOM",
            error_code=error_code, retryable=True, retry_budget=retry_budget,
            detail=str(detail or "")[:500],
        )
        return triggered

    def _is_logged_in(self) -> bool:
        """Check if the user is logged in by looking for the input box."""
        try:
            self._page.wait_for_selector(self.cfg["input_selector"], timeout=8000)
            return True
        except Exception:
            return False

    def _log_stage(self, stage: str, detail: str = "") -> None:
        suffix = f" | {detail}" if detail else ""
        line = f"stage={stage}{suffix}"
        print(f"  [WebScraper] {line}", flush=True)
        _debug_log(f"service={self.service} {line} url={getattr(self._page, 'url', '')}")
        status_stage, message = self._status_from_web_stage(stage, detail)
        if status_stage:
            self._emit_status(status_stage, message=message, detail=detail)

    def _composer_debug_state(self) -> dict:
        """Best-effort composer/send state snapshot; never logs prompt text itself."""
        state = {
            "input_found": False,
            "input_visible": False,
            "composer_len": -1,
            "composer_sha": "",
            "send_found": False,
            "send_visible": False,
            "send_enabled": False,
            "generation_active": False,
        }
        try:
            box = self._page.query_selector(self.cfg["input_selector"])
            state["input_found"] = bool(box)
            if box:
                try:
                    state["input_visible"] = bool(box.is_visible())
                except Exception:
                    pass
                text = ""
                for getter in (
                    lambda: box.input_value(),
                    lambda: box.inner_text(),
                    lambda: box.text_content(),
                ):
                    try:
                        value = getter()
                        if value is not None:
                            text = str(value)
                            break
                    except Exception:
                        continue
                state["composer_len"] = len(text)
                if text:
                    state["composer_sha"] = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
        except Exception:
            pass
        try:
            send = self._page.query_selector(self.cfg.get("done_selector", ""))
            state["send_found"] = bool(send)
            if send:
                try:
                    state["send_visible"] = bool(send.is_visible())
                except Exception:
                    pass
                try:
                    state["send_enabled"] = bool(send.is_enabled())
                except Exception:
                    pass
        except Exception:
            pass
        try:
            state["generation_active"] = bool(self._is_generation_active())
        except Exception:
            pass
        return state

    def _log_composer_state(self, stage: str) -> dict:
        state = self._composer_debug_state()
        self._log_stage(stage, json.dumps(state, ensure_ascii=False, sort_keys=True))
        return state

    def _set_request_state(self, state: str, detail: str = "") -> None:
        self._request_state = str(state or "READY_IDLE")
        self._log_stage("request_state", f"state={self._request_state}" + (f" detail={detail}" if detail else ""))

    @staticmethod
    def _normalize_composer_text(value: str) -> str:
        """Normalize DOM/editor whitespace for reliable prompt verification."""
        value = str(value or "").replace("\u00a0", " ")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\s+", " ", value).strip()

    def _composer_locator(self):
        """Return a live Locator for the current composer, never a cached ElementHandle."""
        selector = self.cfg["input_selector"]
        loc = self._page.locator(selector)
        if loc.count() < 1:
            return None
        # Re-resolve the DOM on every Locator action. ChatGPT may replace the
        # ProseMirror node after focus/state updates, which can stale ElementHandle.
        return loc.last

    def _read_composer_text(self) -> str:
        """Read current composer content without requiring Playwright editability."""
        loc = self._composer_locator()
        if loc is None:
            return ""
        try:
            return str(loc.evaluate(
                "el => (typeof el.value === 'string' ? el.value : (el.innerText || el.textContent || ''))"
            ) or "")
        except Exception:
            return ""

    def _composer_matches_prompt(self, prompt: str) -> bool:
        current = self._normalize_composer_text(self._read_composer_text())
        self._dismiss_known_blocking_dialogs()
        target = self._normalize_composer_text(prompt)
        return bool(target) and current == target

    def _focus_composer_dom(self):
        """Focus the current composer without Playwright's editable/actionability wait."""
        loc = self._composer_locator()
        if loc is None:
            raise WebScraperStageError(
                "[WEB_COMPOSER_UNAVAILABLE] 找不到目前的輸入框。",
                stage="composer",
                safe_to_retry=True,
            )
        try:
            loc.wait_for(state="visible", timeout=10000)
            loc.evaluate("el => el.focus()")
            return loc
        except Exception as exc:
            raise WebScraperStageError(
                f"[WEB_COMPOSER_FOCUS_ERROR] 無法聚焦輸入框: {type(exc).__name__}",
                stage="composer",
                safe_to_retry=True,
            ) from exc

    def _clear_composer_keyboard(self) -> None:
        """Clear a focused input/contenteditable without ElementHandle.fill()."""
        self._page.keyboard.press("Control+A")
        self._page.keyboard.press("Backspace")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not self._normalize_composer_text(self._read_composer_text()):
                return
            time.sleep(0.1)

        # ProseMirror can swallow a keyboard clear while replacing its DOM node.
        # Re-focus the live node and use the browser editing command as a local
        # reconciliation step. This does not cross the submit boundary.
        loc = self._focus_composer_dom()
        try:
            loc.evaluate(
                """el => {
                    el.focus();
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('delete', false, null);
                }"""
            )
        except Exception:
            pass

    def _write_prompt_to_composer(self, prompt: str, timeout_sec: float = 18.0) -> bool:
        """Stage prompt in ChatGPT/Gemini composer and verify the DOM content.

        Returns True when this call mutated/reconciled composer state.  A prompt
        already present from an earlier partial attempt is reused rather than
        filled again.  ChatGPT intentionally avoids ElementHandle.fill(), which
        can wait 30s for an editability state even while React has already
        changed the draft DOM.
        """
        target = self._normalize_composer_text(prompt)
        if not target:
            raise WebScraperStageError(
                "[WEB_COMPOSER_ERROR] prompt 為空。",
                stage="composer",
                safe_to_retry=True,
            )

        self._log_composer_state("composer_before_write")
        if self._composer_matches_prompt(prompt):
            self._log_stage("composer_reused", f"prompt_len={len(prompt)}")
            return False

        self._focus_composer_dom()
        composer_touched = False
        try:
            self._clear_composer_keyboard()
            composer_touched = True
            self._focus_composer_dom()

            started = time.time()
            self._log_stage("composer_insert_begin", f"prompt_len={len(prompt)}")
            if self.service == "chatgpt":
                # insert_text emits one text insertion into the currently focused
                # ProseMirror editor and avoids fill()'s editable actionability wait.
                self._page.keyboard.insert_text(prompt)
            else:
                self._page.keyboard.insert_text(prompt)
            self._log_stage("composer_insert_returned", f"elapsed={time.time()-started:.3f}s")

            deadline = time.time() + timeout_sec
            next_diag = 0.0
            while time.time() < deadline:
                if self._composer_matches_prompt(prompt):
                    state = self._log_composer_state("composer_verified")
                    self._log_stage(
                        "composer_ready",
                        f"composer_len={state.get('composer_len', -1)} prompt_len={len(prompt)}",
                    )
                    return True
                now = time.time()
                if now >= next_diag:
                    self._log_composer_state("waiting_composer_verify")
                    next_diag = now + 1.0
                time.sleep(0.15)
        except WebScraperStageError:
            raise
        except Exception as exc:
            self._log_composer_state("composer_write_exception")
            raise WebScraperStageError(
                f"[WEB_COMPOSER_ERROR] 輸入 prompt 時發生 {type(exc).__name__}；composer 已可能被修改，不自動重啟重送。",
                stage="composer",
                safe_to_retry=False,
            ) from exc

        self._log_composer_state("composer_verify_timeout")
        action = self._request_agent2_action(
            task_phase="COMPOSER", observed_state="COMPOSER_BLOCKED",
            error_code="COMPOSER_VERIFY_TIMEOUT",
            detail=f"prompt_len={len(prompt)} touched={composer_touched}", retry_budget=0,
        )
        if action in {"PRESS_ESCAPE", "DISMISS_DIALOG"}:
            self._apply_agent2_ui_action(action)
        if self._composer_matches_prompt(prompt):
            self._log_stage("composer_ready", "verified_after_agent2_inspection")
            return composer_touched
        raise WebScraperStageError(
            "[WEB_COMPOSER_TIMEOUT] prompt 已嘗試寫入，但輸入框內容未在期限內驗證一致；不會自動重啟或送出。",
            stage="composer",
            safe_to_retry=False if composer_touched else True,
        )

    def _wait_for_send_ready(self, prompt: str, timeout_sec: float = 20.0, _agent2_attempted: bool = False):
        """Require verified composer content and an enabled send button before submit."""
        deadline = time.time() + timeout_sec
        next_diag = 0.0
        while time.time() < deadline:
            self._dismiss_known_blocking_dialogs()
            if not self._composer_matches_prompt(prompt):
                self._log_composer_state("send_ready_composer_changed")
                raise WebScraperStageError(
                    "[WEB_COMPOSER_CHANGED] 等待發送時 composer 已不再等於本輪 prompt；不會送出。",
                    stage="send_ready",
                    safe_to_retry=False,
                )

            if not self._is_generation_active():
                try:
                    send = self._page.locator(self.cfg.get("done_selector", ""))
                    if send.count() > 0:
                        send = send.last
                        if send.is_visible() and send.is_enabled():
                            self._log_stage("send_ready")
                            return send
                except Exception:
                    pass

            now = time.time()
            if now >= next_diag:
                self._log_composer_state("waiting_send_ready")
                next_diag = now + 1.0
            time.sleep(0.2)

        self._log_composer_state("send_ready_timeout")
        if not _agent2_attempted:
            state = self._composer_debug_state()
            observed = "GENERATION_ACTIVE" if state.get("generation_active") else "COMPOSER_BLOCKED"
            action = self._request_agent2_action(
                task_phase="COMPOSER", observed_state=observed,
                error_code="SEND_BUTTON_UNAVAILABLE", detail=json.dumps(state, sort_keys=True),
                retry_budget=1,
            )
            if action in {"PRESS_ESCAPE", "DISMISS_DIALOG"}:
                self._apply_agent2_ui_action(action)
            if action in {"RETRY_ONCE", "PRESS_ESCAPE", "DISMISS_DIALOG"}:
                return self._wait_for_send_ready(prompt, timeout_sec=5.0, _agent2_attempted=True)
        raise WebScraperStageError(
            "[WEB_SEND_READY_TIMEOUT] prompt 已在 composer 中，但發送按鈕未在期限內進入可用狀態；不會自動重啟或重填。",
            stage="send_ready",
            safe_to_retry=False,
        )

    def _submit_verified_prompt(
        self, send_locator, *, prompt: str = "", attachment_paths: list | None = None
    ) -> None:
        """Cross the submit boundary exactly once after final UI validation."""
        self._log_stage("submit_begin", "mode=send_button")
        rate_lease = (
            getattr(self, "_rate_submit_lease", None)
            if self.service == "chatgpt" else None
        )
        if rate_lease is not None:
            rate_lease.before_submit()
            self._log_stage("global_send_interval_ready")

        attachments = list(attachment_paths or [])
        if self.service == "chatgpt" and attachments:
            if not self._wait_for_attachment_ui(attachments, timeout_sec=600.0, no_progress_timeout_sec=30.0, stable_ready_sec=1.5):
                raise WebScraperStageError(
                    "[WEB_ATTACHMENT_PRECLICK_CHANGED] 全域 Send 等待後附件未保持穩定 READY；禁止送出。 " + str(getattr(self, "_last_attachment_wait_reason", "unknown")),
                    stage="attachment_preclick_revalidate", safe_to_retry=False,
                )
            self._log_stage("attachment_set_ready", f"count={len(attachments)} phase=pre_click_stable")

        if prompt:
            send_locator = self._wait_for_send_ready(prompt, timeout_sec=5.0)
            self._log_stage("final_send_ready")

        click_completed = False
        self._submit_click_attempted = False
        try:
            self._log_stage("send_click", "mode=send_button")
            self._submit_click_attempted = True
            self._set_request_state("SUBMIT_ATTEMPTED")
            if self.service == "chatgpt":
                try:
                    self._page.evaluate("() => { window.__webAgentAutomationSubmit = true; }")
                except Exception:
                    pass
                try:
                    send_locator.click(timeout=5000)
                finally:
                    try:
                        self._page.evaluate("() => { window.__webAgentAutomationSubmit = false; }")
                    except Exception:
                        pass
            elif self.cfg.get("send_by_enter"):
                self._page.keyboard.press("Enter")
            else:
                send_locator.click(timeout=5000)
            click_completed = True
        except Exception as exc:
            raise WebScraperStageError(
                f"[WEB_SUBMIT_ERROR] 發送動作失敗或結果不確定: {type(exc).__name__}；為避免 duplicate 不自動重送。",
                stage="submit",
                safe_to_retry=False,
            ) from exc
        finally:
            if rate_lease is not None and click_completed:
                rate_lease.record_submit()
        self._set_request_state("WAIT_USER_TURN_ACK")
        self._log_stage("submit_returned")

    def _reconcile_submit_delivery(self, snapshot: dict, prompt: str, timeout_sec: float = 3.0) -> str:
        """Classify a failed click as sent / not_sent / ambiguous before aborting."""
        deadline = time.time() + max(0.5, float(timeout_sec))
        while time.time() < deadline:
            users = self._turn_elements("user")
            if len(users) > snapshot.get("user_count", 0):
                return "sent"
            if users and snapshot.get("user_count", 0) > 0:
                if self._element_fingerprint(users[-1]) != snapshot.get("last_user_fp", ""):
                    return "sent"
            composer_matches = self._composer_matches_prompt(prompt)
            active = self._is_generation_active()
            composer_empty = not self._normalize_composer_text(self._read_composer_text())
            if composer_empty and active:
                return "sent"
            if composer_matches and not active:
                time.sleep(0.15)
                continue
            time.sleep(0.15)
        if self._composer_matches_prompt(prompt) and not self._is_generation_active():
            return "not_sent"
        return "ambiguous"

    def _reset_pre_submit_to_ready_idle(self, attachment_paths: list | None = None) -> bool:
        if self._is_generation_active():
            self._set_request_state("RECOVERY_REQUIRED", "generation_active")
            return False
        try:
            if attachment_paths:
                self._clear_composer_attachments()
                time.sleep(0.1)
            self._focus_composer_dom()
            self._clear_composer_keyboard()
        except Exception as exc:
            self._set_request_state("RECOVERY_REQUIRED", f"cleanup_exception={type(exc).__name__}")
            return False
        if self._normalize_composer_text(self._read_composer_text()):
            self._set_request_state("RECOVERY_REQUIRED", "composer_not_empty_after_cleanup")
            return False
        self._pending_submit_context = None
        self._submit_click_attempted = False
        self._set_request_state("READY_IDLE", "cleanup_complete")
        return True

    def _recover_not_sent_to_ready_idle(self, prompt: str, attachment_paths: list | None = None) -> bool:
        if self._is_generation_active() or not self._composer_matches_prompt(prompt):
            self._set_request_state("RECOVERY_REQUIRED", "not_sent_cleanup_guard_failed")
            return False
        return self._reset_pre_submit_to_ready_idle(attachment_paths)

    def _reconcile_pending_submit_before_request(self) -> None:
        if getattr(self, "_request_state", "READY_IDLE") != "RECOVERY_REQUIRED":
            return
        context = getattr(self, "_pending_submit_context", None)
        if not isinstance(context, dict):
            raise WebScraperStageError("[WEB_RECOVERY_REQUIRED] 缺少前一輪 submit context；禁止新 request。", stage="recovery_required", safe_to_retry=False)
        snapshot=context.get("snapshot") or {}
        prompt=str(context.get("prompt") or "")
        attachments=list(context.get("attachments") or [])
        delivery=self._reconcile_submit_delivery(snapshot,prompt,timeout_sec=5.0)
        self._log_stage("pending_submit_reconcile",f"classification={delivery}")
        if delivery == "not_sent" and self._recover_not_sent_to_ready_idle(prompt,attachments):
            return
        if delivery == "sent":
            self._pending_submit_context=None
            self._submit_click_attempted=False
            self._wait_for_idle_before_submit()
            self._set_request_state("READY_IDLE","prior_submit_confirmed_sent")
            return
        self._set_request_state("RECOVERY_REQUIRED",f"classification={delivery}")
        raise WebScraperStageError("[WEB_RECOVERY_REQUIRED] 前一輪 submit 仍無法安全判定；禁止新 request。",stage="recovery_required",safe_to_retry=False)

    def _visible_element(self, selector: str):
        """Return the first visible element for a selector, else None."""
        try:
            for element in self._page.query_selector_all(selector):
                try:
                    if element.is_visible():
                        return element
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _is_generation_active(self) -> bool:
        """Best-effort generation signal; never used as the sole completion test."""
        selectors = self.cfg.get("stop_selectors") or [self.cfg.get("stop_selector", "")]
        for selector in selectors:
            if selector and self._visible_element(selector) is not None:
                return True
        return False


    def _check_cancel_requested(self, stage: str = "generation") -> None:
        """Abort a wait loop cooperatively when LocalAgent requested cancellation."""
        if self._cancel_requested.is_set():
            raise WebScraperStageError(
                "[WEB_CANCELLED] 使用者已要求中止目前 WebGPT 動作；不重送本輪 prompt。",
                stage=stage,
                safe_to_retry=False,
            )

    @staticmethod
    def _safe_visible(element) -> bool:
        try:
            return bool(element and element.is_visible())
        except Exception:
            return False

    def _ensure_activity_observer(self) -> None:
        """Install a lightweight DOM MutationObserver for assistant activity.

        Polling can miss brief DOM changes between samples.  This observer only
        counts structural/text/relevant-state changes inside assistant turns and
        intentionally ignores style/class animation churn.  The counter is a
        supplemental progress signal; it is never sufficient by itself to mark
        a response complete.
        """
        if self._page is None:
            return
        try:
            self._page.evaluate(
                """() => {
                    if (window.__smartAgentActivityObserverInstalled) return;
                    window.__smartAgentActivity = window.__smartAgentActivity || {
                        count: 0,
                        lastMutationAt: 0,
                        lastKind: ''
                    };
                    const isAssistantRelated = (node) => {
                        let el = null;
                        if (!node) return false;
                        if (node.nodeType === Node.ELEMENT_NODE) el = node;
                        else el = node.parentElement;
                        if (!el) return false;
                        return !!el.closest('[data-message-author-role="assistant"], model-response, .agent-turn');
                    };
                    const observer = new MutationObserver((mutations) => {
                        let changed = false;
                        let kind = '';
                        for (const m of mutations) {
                            if (!isAssistantRelated(m.target)) continue;
                            if (m.type === 'attributes') {
                                if (!['src','aria-busy','aria-label','data-state','data-testid'].includes(m.attributeName || '')) continue;
                                kind = 'attribute:' + (m.attributeName || '');
                            } else {
                                kind = m.type;
                            }
                            changed = true;
                            break;
                        }
                        if (changed) {
                            window.__smartAgentActivity.count += 1;
                            window.__smartAgentActivity.lastMutationAt = Date.now();
                            window.__smartAgentActivity.lastKind = kind;
                        }
                    });
                    observer.observe(document.documentElement || document.body, {
                        subtree: true,
                        childList: true,
                        characterData: true,
                        attributes: true,
                        attributeFilter: ['src','aria-busy','aria-label','data-state','data-testid']
                    });
                    window.__smartAgentActivityObserverInstalled = true;
                    window.__smartAgentActivityObserver = observer;
                }"""
            )
            self._activity_observer_installed = True
        except Exception:
            self._activity_observer_installed = False

    def _activity_observer_state(self) -> dict:
        """Return the browser-side assistant mutation counter if available."""
        self._ensure_activity_observer()
        if self._page is None:
            return {"mutation_count": 0, "last_mutation_at": 0, "last_mutation_kind": ""}
        try:
            value = self._page.evaluate(
                """() => {
                    const s = window.__smartAgentActivity || {};
                    return {
                        mutation_count: Number(s.count || 0),
                        last_mutation_at: Number(s.lastMutationAt || 0),
                        last_mutation_kind: String(s.lastKind || '')
                    };
                }"""
            ) or {}
        except Exception:
            value = {}
        return {
            "mutation_count": int(value.get("mutation_count") or 0),
            "last_mutation_at": int(value.get("last_mutation_at") or 0),
            "last_mutation_kind": str(value.get("last_mutation_kind") or ""),
        }

    @staticmethod
    def _is_relevant_assistant_image(info: dict) -> bool:
        """Exclude decorative/empty <img> nodes from the generation gate."""
        if not bool(info.get("visible")):
            return False
        semantic = " ".join(str(info.get(key) or "") for key in (
            "alt", "aria_label", "testid", "class_name",
        )).lower()
        generation_marker = any(marker in semantic for marker in (
            "generated", "generating", "imagegen", "image-gen", "dall-e", "dalle",
            "產生", "生成",
        ))
        natural_width = int(info.get("naturalWidth") or 0)
        natural_height = int(info.get("naturalHeight") or 0)
        rendered_width = float(info.get("renderedWidth") or 0)
        rendered_height = float(info.get("renderedHeight") or 0)
        # A real completed assistant image has useful intrinsic dimensions.
        # A pending image is accepted only when the DOM explicitly identifies
        # it as generation UI or reserves a substantial visible image area.
        substantial = (
            natural_width >= 64 and natural_height >= 64
        ) or (
            rendered_width >= 96 and rendered_height >= 96
        )
        return bool(generation_marker or substantial)

    def _assistant_media_state(self, assistant_turn) -> dict:
        """Inspect media/busy state scoped to the fresh assistant turn.

        This deliberately looks at semantic loading signals (image readiness,
        progress/busy nodes, loading/generating state attributes) rather than a
        fixed sleep.  It avoids treating an assistant's partially-rendered text
        as final while an image/tool result is still being produced.
        """
        state = {
            "image_count": 0,
            "image_ready": 0,
            "image_pending": 0,
            "video_count": 0,
            "canvas_count": 0,
            "busy_count": 0,
            "media_pending": False,
            "media_fingerprint": "",
        }
        root = assistant_turn
        if root is None:
            return state

        media_parts = []
        try:
            images = list(root.query_selector_all("img"))
        except Exception:
            images = []
        state["image_count"] = len(images)
        relevant_images = []
        for image in images:
            try:
                info = image.evaluate(
                    """el => ({
                        src: el.currentSrc || el.src || '',
                        complete: !!el.complete,
                        naturalWidth: Number(el.naturalWidth || 0),
                        naturalHeight: Number(el.naturalHeight || 0),
                        renderedWidth: Number(el.getBoundingClientRect().width || 0),
                        renderedHeight: Number(el.getBoundingClientRect().height || 0),
                        visible: !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'),
                        alt: el.getAttribute('alt') || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        testid: el.getAttribute('data-testid') || '',
                        class_name: String(el.className || '')
                    })"""
                ) or {}
            except Exception:
                info = {}
            if not self._is_relevant_assistant_image(info):
                continue
            relevant_images.append(image)
            ready = bool(
                info.get("complete")
                and int(info.get("naturalWidth") or 0) > 0
                and int(info.get("naturalHeight") or 0) > 0
            )
            if ready:
                state["image_ready"] += 1
            else:
                state["image_pending"] += 1
            media_parts.append(
                "img|{}|{}|{}x{}".format(
                    str(info.get("src") or "")[:300],
                    int(bool(info.get("complete"))),
                    int(info.get("naturalWidth") or 0),
                    int(info.get("naturalHeight") or 0),
                )
            )

        state["image_count"] = len(relevant_images)

        for selector, key in (("video", "video_count"), ("canvas", "canvas_count")):
            try:
                elements = list(root.query_selector_all(selector))
            except Exception:
                elements = []
            state[key] = len(elements)
            for element in elements:
                try:
                    media_parts.append(
                        f"{selector}|"
                        + str(element.get_attribute("src") or element.get_attribute("poster") or "")[:300]
                    )
                except Exception:
                    media_parts.append(selector)

        busy_selectors = (
            '[aria-busy="true"]',
            '[role="progressbar"]',
            '[data-state="loading"]',
            '[data-state="generating"]',
            '[data-state="processing"]',
            '[data-state="thinking"]',
            '[data-state="working"]',
            '[data-state="creating"]',
            '[data-state="preparing"]',
            '[data-testid*="loading"]',
            '[data-testid*="generat"]',
            '[data-testid*="think"]',
            '[data-testid*="working"]',
            '[aria-label*="Thinking"]',
            '[aria-label*="Working"]',
            '[aria-label*="Generating"]',
            '[aria-label*="Processing"]',
            '[aria-label*="Creating"]',
            '[aria-label*="Preparing"]',
            '[class*="loading"]',
            '[class*="generating"]',
            '[class*="thinking"]',
            '[class*="working"]',
            '[class*="creating"]',
            '[class*="processing"]',
            '[class*="skeleton"]',
            '[class*="shimmer"]',
        )
        seen_busy = set()
        for selector in busy_selectors:
            try:
                elements = root.query_selector_all(selector)
            except Exception:
                elements = []
            for element in elements:
                try:
                    # Ignore hidden layout/template nodes.
                    if not element.is_visible():
                        continue
                    ident = (
                        element.get_attribute("data-testid")
                        or element.get_attribute("aria-label")
                        or element.get_attribute("data-state")
                        or element.get_attribute("class")
                        or selector
                    )
                except Exception:
                    continue
                ident = str(ident or selector)[:180]
                if ident not in seen_busy:
                    seen_busy.add(ident)
                    media_parts.append("busy|" + ident)

        state["busy_count"] = len(seen_busy)
        state["media_pending"] = bool(
            state["image_pending"] > 0
            or state["busy_count"] > 0
        )
        state["media_fingerprint"] = hashlib.sha256(
            "\n".join(media_parts).encode("utf-8", errors="replace")
        ).hexdigest()
        return state

    def _assistant_activity_state(self, snapshot: dict, assistant_turn) -> dict:
        """Build a progress signature for the current fresh assistant response."""
        generation_active = bool(self._is_generation_active())
        text = self._current_new_response_text(snapshot, assistant_turn)
        media = self._assistant_media_state(assistant_turn)

        assistant_fp = self._element_fingerprint(assistant_turn) if assistant_turn is not None else ""
        text_fp = hashlib.sha256(
            str(text or "").encode("utf-8", errors="replace")
        ).hexdigest()

        observer = self._activity_observer_state()
        state = {
            "generation_active": generation_active,
            "assistant_fp": assistant_fp,
            "text_len": len(text or ""),
            "text_fp": text_fp,
            **media,
            **observer,
        }
        state_blob = json.dumps(state, ensure_ascii=False, sort_keys=True)
        state["progress_signature"] = hashlib.sha256(
            state_blob.encode("utf-8", errors="replace")
        ).hexdigest()
        return state

    def cancel_current_generation(self, timeout_sec: float = 8.0) -> dict:
        """Best-effort cooperative cancellation without closing the browser.

        Order:
          1. click the visible Stop/Stop responding control;
          2. press Escape as a UI-level fallback;
          3. reload the same conversation page if the generation control is
             still active. Browser/context restart is intentionally left to the
             manager as the final recovery tier.
        """
        self._cancel_requested.set()
        result = {
            "status": "not_running",
            "stopped": False,
            "reloaded": False,
            "browser_alive": False,
        }

        page = self._page
        if page is None:
            result["status"] = "page_unavailable"
            return result

        try:
            # A dead Playwright page throws here.
            _ = page.url
            result["browser_alive"] = True
        except Exception:
            result["status"] = "page_unavailable"
            return result

        try:
            active_before = bool(self._is_generation_active())
        except Exception:
            active_before = False

        if not active_before:
            result["status"] = "already_idle"
            self._cancel_requested.clear()
            return result

        self._log_stage("cancel_requested", "attempt=stop_button")

        selectors = self.cfg.get("stop_selectors") or [self.cfg.get("stop_selector", "")]
        clicked = False
        for selector in selectors:
            if not selector:
                continue
            try:
                loc = page.locator(selector)
                count = loc.count()
                for idx in range(count - 1, -1, -1):
                    candidate = loc.nth(idx)
                    if candidate.is_visible() and candidate.is_enabled():
                        candidate.evaluate("el => el.click()")
                        clicked = True
                        break
                if clicked:
                    break
            except Exception:
                continue

        deadline = time.time() + max(1.0, float(timeout_sec))
        while time.time() < deadline:
            try:
                if not self._is_generation_active():
                    result.update({"status": "stopped", "stopped": True})
                    self._log_stage("cancel_complete", "method=stop_button")
                    self._cancel_requested.clear()
                    return result
            except Exception:
                break
            time.sleep(0.15)

        # UI fallback before any restart/reload.
        try:
            self._log_stage("cancel_fallback", "method=escape")
            page.keyboard.press("Escape")
        except Exception:
            pass

        escape_deadline = time.time() + 2.0
        while time.time() < escape_deadline:
            try:
                if not self._is_generation_active():
                    result.update({"status": "stopped", "stopped": True})
                    self._log_stage("cancel_complete", "method=escape")
                    self._cancel_requested.clear()
                    return result
            except Exception:
                break
            time.sleep(0.15)

        # Stop control could not settle the UI. Reload the same conversation
        # while preserving the persistent browser context/login/profile.
        try:
            current_url = page.url
            self._log_stage("cancel_recover", "method=reload_same_conversation")
            page.reload(wait_until="domcontentloaded", timeout=15000)
            result["reloaded"] = True
            result["browser_alive"] = True
            result["status"] = "reloaded"
            if current_url and page.url != current_url:
                try:
                    page.goto(current_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            self._cancel_requested.clear()
            return result
        except Exception as exc:
            result["status"] = "restart_required"
            result["error"] = f"{type(exc).__name__}: {exc}"
            self._log_stage("cancel_recover_failed", result["error"])
            return result

    def _turn_elements(self, role: str) -> list:
        key = "user_turn_selector" if role == "user" else "assistant_turn_selector"
        selector = self.cfg.get(key, "")
        if not selector:
            return []
        try:
            return list(self._page.query_selector_all(selector))
        except Exception:
            return []

    @staticmethod
    def _element_fingerprint(element) -> str:
        """Stable-enough DOM fingerprint for freshness checks without exposing content."""
        if element is None:
            return ""
        parts = []
        for attr in ("data-message-id", "data-testid", "id"):
            try:
                value = element.get_attribute(attr)
            except Exception:
                value = None
            if value:
                parts.append(f"{attr}={value}")
        try:
            parts.append(element.inner_text() or "")
        except Exception:
            pass
        return hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()

    def _snapshot_turn_state(self) -> dict:
        users = self._turn_elements("user")
        assistants = self._turn_elements("assistant")
        try:
            responses = self._page.query_selector_all(self.cfg["response_selector"])
        except Exception:
            responses = []
        return {
            "user_count": len(users),
            "assistant_count": len(assistants),
            "response_count": len(responses),
            "last_user_fp": self._element_fingerprint(users[-1]) if users else "",
            "last_assistant_fp": self._element_fingerprint(assistants[-1]) if assistants else "",
            "last_response_fp": self._element_fingerprint(responses[-1]) if responses else "",
        }

    def _wait_for_idle_before_submit(self) -> None:
        """Never submit while a prior generation is active.

        This is progress-aware: a previous generation may continue beyond the
        nominal timeout while its observable assistant/media state changes.
        """
        if not self._is_generation_active():
            return

        self._log_stage("preflight_wait", "previous generation still active")
        last_signature = ""
        last_progress_at = time.time()
        last_log = 0.0

        while True:
            self._dismiss_known_blocking_dialogs()
            self._check_cancel_requested("preflight_generation")
            if not self._is_generation_active():
                self._log_stage("preflight_ready")
                return

            assistants = self._turn_elements("assistant")
            assistant_turn = assistants[-1] if assistants else None
            assistant_fp = self._element_fingerprint(assistant_turn) if assistant_turn else ""
            media = self._assistant_media_state(assistant_turn)
            state = {
                "generation_active": True,
                "assistant_fp": assistant_fp,
                "media_fingerprint": media.get("media_fingerprint", ""),
                "image_count": media.get("image_count", 0),
                "image_ready": media.get("image_ready", 0),
                "busy_count": media.get("busy_count", 0),
            }
            signature = hashlib.sha256(
                json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
            ).hexdigest()
            now = time.time()
            if signature != last_signature:
                last_signature = signature
                last_progress_at = now

            stalled_for = now - last_progress_at
            if stalled_for >= self._active_generation_emergency_sec:
                self._log_stage(
                    "emergency_stalled",
                    f"stage=preflight_generation no_progress={stalled_for:.1f}s generation_active=true",
                )
                raise WebScraperStageError(
                    "[WEB_GENERATION_EMERGENCY_STALLED] 前一輪仍明確顯示 generation active，"
                    "但超過 emergency watchdog 都沒有任何可觀察進展；未送出新的 prompt。",
                    stage="preflight_generation_emergency_stalled",
                    safe_to_retry=False,
                )

            if now - last_log >= 2.0:
                self._log_stage(
                    "preflight_wait",
                    (
                        f"no_progress={stalled_for:.1f}s "
                        f"images={state['image_ready']}/{state['image_count']} "
                        f"busy={state['busy_count']} "
                        f"watchdog={'warning-only' if stalled_for >= self._active_generation_warn_sec else 'active'}"
                    ),
                )
                last_log = now
            time.sleep(0.25)

    def _wait_for_user_sent(self, snapshot: dict, timeout_sec: float = 45.0) -> None:
        """Require a genuinely new user turn before looking at assistant content."""
        deadline = time.time() + timeout_sec
        next_diag = 0.0
        while time.time() < deadline:
            users = self._turn_elements("user")
            if len(users) > snapshot.get("user_count", 0):
                self._log_stage("user_sent", f"user_turns={len(users)}")
                self._log_composer_state("user_sent_composer")
                return
            # Some UIs may recycle the last turn node.  A changed fingerprint is
            # accepted only when a user turn already existed in the snapshot.
            if users and snapshot.get("user_count", 0) > 0:
                if self._element_fingerprint(users[-1]) != snapshot.get("last_user_fp", ""):
                    self._log_stage("user_sent", "user turn fingerprint changed")
                    self._log_composer_state("user_sent_composer")
                    return
            now = time.time()
            if now >= next_diag:
                state = self._composer_debug_state()
                self._log_stage(
                    "waiting_user_sent",
                    f"users={len(users)} old_users={snapshot.get('user_count', 0)} composer={json.dumps(state, ensure_ascii=False, sort_keys=True)}",
                )
                next_diag = now + 1.0
            time.sleep(0.25)
        self._log_composer_state("user_sent_timeout_composer")
        self._log_stage("timeout", "stage=user_sent")
        raise WebScraperStageError(
            "[WEB_SEND_TIMEOUT] submit 已嘗試，但未確認本輪新的 user turn 出現在對話 DOM；不得讀取舊 assistant 回應。",
            stage="user_sent",
            safe_to_retry=False,
        )

    def _wait_for_new_assistant_turn(self, snapshot: dict, timeout_sec: float = None):
        """Wait for the fresh assistant turn without a fixed normal deadline.

        ``timeout_sec`` is accepted for API compatibility but is treated only as
        a minimum no-progress stall window. If ChatGPT keeps changing observable
        state, waiting continues.
        """
        stall_window = max(
            self._generation_stall_sec,
            float(timeout_sec) if timeout_sec is not None else 0.0,
        )
        last_signature = ""
        last_progress_at = time.time()
        last_log = 0.0

        while True:
            self._check_cancel_requested("assistant_started")
            assistants = self._turn_elements("assistant")
            old_count = snapshot.get("assistant_count", 0)
            if len(assistants) > old_count:
                self._log_stage("assistant_started", f"assistant_turns={len(assistants)}")
                return assistants[-1]
            if assistants and old_count > 0:
                if self._element_fingerprint(assistants[-1]) != snapshot.get("last_assistant_fp", ""):
                    self._log_stage("assistant_started", "assistant turn fingerprint changed")
                    return assistants[-1]

            try:
                responses = self._page.query_selector_all(self.cfg["response_selector"])
            except Exception:
                responses = []
            if len(responses) > snapshot.get("response_count", 0):
                self._log_stage("assistant_started", f"response_blocks={len(responses)}")
                return None

            # A visible Stop control is evidence that the request is still being
            # worked on even if the assistant placeholder has not been mounted.
            state = {
                "assistant_count": len(assistants),
                "response_count": len(responses),
                "generation_active": bool(self._is_generation_active()),
                "last_assistant_fp": self._element_fingerprint(assistants[-1]) if assistants else "",
            }
            signature = hashlib.sha256(
                json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
            ).hexdigest()
            now = time.time()
            if signature != last_signature:
                last_signature = signature
                last_progress_at = now

            stalled_for = now - last_progress_at
            active = bool(state["generation_active"])
            stall_limit = self._active_generation_emergency_sec if active else stall_window
            if stalled_for >= stall_limit:
                marker = "WEB_ASSISTANT_START_EMERGENCY_STALLED" if active else "WEB_ASSISTANT_START_STALLED"
                stage_name = "assistant_started_emergency_stalled" if active else "assistant_started_stalled"
                self._log_stage(
                    "emergency_stalled" if active else "stalled",
                    f"stage=assistant_started no_progress={stalled_for:.1f}s state={json.dumps(state, ensure_ascii=False)}",
                )
                raise WebScraperStageError(
                    f"[{marker}] 已確認 user turn 送出，但 assistant 啟動狀態長時間沒有進展；"
                    "不會回傳上一輪回答，也不自動重送。",
                    stage=stage_name,
                    safe_to_retry=False,
                )

            if now - last_log >= 2.0:
                self._log_stage(
                    "waiting_assistant_started",
                    f"generation_active={state['generation_active']} no_progress={stalled_for:.1f}s "
                    f"watchdog={'warning-only' if active and stalled_for >= self._active_generation_warn_sec else 'active'}",
                )
                last_log = now
            time.sleep(0.25)

    def _extract_response_from_turn(self, assistant_turn) -> str:
        """Extract final answer content scoped to one assistant turn."""
        if assistant_turn is not None:
            try:
                blocks = assistant_turn.query_selector_all(".markdown")
            except Exception:
                blocks = []
            if blocks:
                texts = []
                for block in blocks:
                    try:
                        text = (block.inner_text() or "").strip()
                    except Exception:
                        text = ""
                    if text:
                        texts.append(text)
                if texts:
                    return "\n\n".join(texts).strip()

            # For non-ChatGPT services the assistant root itself may be the
            # response container.  ChatGPT intentionally does not use this
            # fallback because thinking placeholders can contain non-answer UI.
            if self.service != "chatgpt":
                try:
                    return (assistant_turn.inner_text() or "").strip()
                except Exception:
                    pass
        return ""

    def _current_new_response_text(self, snapshot: dict, assistant_turn) -> str:
        """Return only response content proven fresh for the current request.

        ChatGPT frequently updates/replaces the last assistant/markdown node in
        place, so freshness cannot rely only on DOM element counts increasing.
        A changed fingerprint relative to the pre-submit snapshot is also valid.
        """
        text = self._extract_response_from_turn(assistant_turn)
        if text:
            return text

        try:
            responses = self._page.query_selector_all(self.cfg["response_selector"])
        except Exception:
            responses = []

        if responses:
            last = responses[-1]
            last_fp = self._element_fingerprint(last)
            response_is_fresh = (
                len(responses) > snapshot.get("response_count", 0)
                or last_fp != snapshot.get("last_response_fp", "")
            )
            if response_is_fresh:
                try:
                    text = (last.inner_text() or "").strip()
                except Exception:
                    text = ""
                if text:
                    return text

        # Final fallback for current ChatGPT DOM variants where the completed
        # answer is present on the fresh assistant turn but no `.markdown`
        # descendant is exposed. This is only used after freshness was proven
        # by the assistant fingerprint, so an old turn cannot be returned.
        if assistant_turn is not None:
            current_fp = self._element_fingerprint(assistant_turn)
            if current_fp and current_fp != snapshot.get("last_assistant_fp", ""):
                try:
                    return (assistant_turn.inner_text() or "").strip()
                except Exception:
                    pass
        return ""

    @staticmethod
    def _protocol_commit_candidates(text: str) -> list[dict]:
        """Extract simple turn_commit JSON objects from rendered assistant text."""
        source = str(text or "")
        candidates = []
        pattern = re.compile(r'\{[^{}]{0,2400}?"tool"\s*:\s*"turn_commit"[^{}]{0,2400}?\}', re.DOTALL)
        for match in pattern.finditer(source):
            raw = match.group(0)
            try:
                value = json.loads(raw)
            except Exception:
                continue
            if isinstance(value, dict) and value.get("tool") == "turn_commit":
                candidates.append(value)
        return candidates

    @classmethod
    def _matching_protocol_commit(cls, text: str, expected: dict | None):
        if not expected:
            return None
        wanted = {
            "run_id": str(expected.get("run_id", "")),
            "turn_id": int(expected.get("turn_id", 0)),
            "ack_local_nonce": str(expected.get("local_nonce", "")),
            "ack_result_id": str(expected.get("ack_result_id", "")),
            "ack_web_ack_id": str(expected.get("ack_web_ack_id", "")),
        }
        for commit in reversed(cls._protocol_commit_candidates(text)):
            web_ack_id = str(commit.get("web_ack_id", "") or "").strip()
            fresh_web_ack = bool(web_ack_id and web_ack_id != wanted["ack_web_ack_id"])
            if fresh_web_ack and all(commit.get(key) == value for key, value in wanted.items()):
                return commit
        return None

    @classmethod
    def _protocol_commit_diagnostic(cls, text: str, expected: dict | None) -> dict:
        """Explain why the latest rendered turn_commit is not acceptable."""
        candidates = cls._protocol_commit_candidates(text)
        if not expected:
            return {"kind": "not_expected", "candidate_count": len(candidates)}
        if not candidates:
            return {"kind": "missing", "candidate_count": 0}
        wanted = {
            "run_id": str(expected.get("run_id", "")),
            "turn_id": int(expected.get("turn_id", 0)),
            "ack_local_nonce": str(expected.get("local_nonce", "")),
            "ack_result_id": str(expected.get("ack_result_id", "")),
            "ack_web_ack_id": str(expected.get("ack_web_ack_id", "")),
        }
        observed = candidates[-1]
        mismatches = {
            key: {"expected": value, "observed": observed.get(key)}
            for key, value in wanted.items()
            if observed.get(key) != value
        }
        web_ack_id = str(observed.get("web_ack_id", "") or "").strip()
        if not web_ack_id:
            mismatches["web_ack_id"] = {"expected": "non-empty-new-id", "observed": observed.get("web_ack_id")}
        elif web_ack_id == wanted["ack_web_ack_id"]:
            mismatches["web_ack_id"] = {"expected": "fresh-id", "observed": web_ack_id}
        tool_markers = len(re.findall(r'"tool"\s*:', str(text or "")))
        ack_only = bool(
            len(candidates) == 1
            and tool_markers == 1
            and observed.get("action_count") == 0
        )
        return {
            "kind": "mismatch" if mismatches else "matching",
            "candidate_count": len(candidates),
            "ack_only": ack_only,
            "mismatches": mismatches,
            "observed": {
                "run_id": observed.get("run_id"),
                "turn_id": observed.get("turn_id"),
                "ack_local_nonce": observed.get("ack_local_nonce"),
                "ack_result_id": observed.get("ack_result_id"),
                "ack_web_ack_id": observed.get("ack_web_ack_id"),
                "web_ack_id": observed.get("web_ack_id"),
                "action_count": observed.get("action_count"),
            },
        }

    @classmethod
    def run_ui_first_ack_self_tests(cls) -> dict:
        expected = {
            "run_id": "SA-TEST",
            "turn_id": 2,
            "local_nonce": "nonce-2",
            "ack_result_id": "RES-1",
            "ack_web_ack_id": "WEBACK-1",
        }
        good = (
            'smartagent_tool\n'
            '{"tool":"final_response","action_id":"A-2","content":"ok"}\n'
            'smartagent_tool\n'
            '{"tool":"turn_commit","run_id":"SA-TEST","turn_id":2,'
            '"ack_local_nonce":"nonce-2","ack_result_id":"RES-1",'
            '"ack_web_ack_id":"WEBACK-1","web_ack_id":"WEBACK-2","action_count":1}'
        )
        bad_chain = good.replace('"ack_web_ack_id":"WEBACK-1"', '"ack_web_ack_id":"OLD"')
        missing_new_ack = good.replace('"web_ack_id":"WEBACK-2",', '')
        results = {
            "matching_extended_commit": bool(cls._matching_protocol_commit(good, expected)),
            "reject_wrong_local_ack_chain": cls._matching_protocol_commit(bad_chain, expected) is None,
            "require_new_web_ack_id": cls._matching_protocol_commit(missing_new_ack, expected) is None,
        }
        results["all_passed"] = all(results.values())
        return results

    @staticmethod
    def _protocol_recovery_prompt(expected: dict) -> str:
        """Build the one allowed same-turn protocol retransmission.

        This is not a new logical SmartAgent turn.  It reuses the outstanding
        Local Commit identity so strict WebACK -> LocalACK alternation is not
        advanced merely because ChatGPT's media tool ended the assistant turn
        before textual control envelopes were appended.
        """
        expected = dict(expected or {})
        local_commit = {
            "run_id": str(expected.get("run_id", "")),
            "turn_id": int(expected.get("turn_id", 0) or 0),
            "local_nonce": str(expected.get("local_nonce", "")),
            "ack_result_id": str(expected.get("ack_result_id", "")),
            "ack_web_ack_id": str(expected.get("ack_web_ack_id", "")),
        }
        return (
            "[SMARTAGENT_PROTOCOL_RECOVERY_RETRANSMIT]\n"
            f"[WEBAGENT_REQUEST_TRACE] request_id={local_commit['run_id']} "
            f"round={local_commit['turn_id']} attempt=2 "
            f"previous_ack_id={local_commit['ack_web_ack_id']} [/WEBAGENT_REQUEST_TRACE]\n"
            "This is the only protocol repair attempt for the SAME request and round.\n"
            "No local action from the rejected response was executed. Do NOT regenerate any image/file, "
            "do NOT repeat completed WebGPT-side work, and do NOT start a new logical turn.\n"
            "Re-emit the same complete SmartAgent decision as valid control envelope(s). "
            "If that completed result is a newly generated image/file the user asked to save locally, emit "
            "download_artifact for THIS fresh result before turn_commit. Never reference or reuse an artifact "
            "from an earlier assistant turn. Then finish with the matching turn_commit. Reuse the exact "
            "run_id/turn_id/local_nonce/ACK chain below and create a fresh web_ack_id.\n"
            "[SMARTAGENT_LOCAL_COMMIT] "
            + json.dumps(local_commit, ensure_ascii=False, separators=(",", ":"))
        )

    def _protocol_recovery_admission_state(self) -> tuple[bool, dict]:
        """Check whether an ACK-only recovery probe is safe to place in composer.

        Empty-composer is mandatory so an automatic recovery probe never
        overwrites text a human typed while LocalAgent was waiting.  The actual
        send-button enablement is verified again after staging the short probe.
        """
        self._dismiss_known_blocking_dialogs()
        composer = self._composer_debug_state()
        assistants = self._turn_elements("assistant")
        latest = assistants[-1] if assistants else None
        media = self._assistant_media_state(latest)
        composer_text = self._normalize_composer_text(self._read_composer_text())
        ready = bool(
            composer.get("input_found")
            and composer.get("input_visible")
            and not composer.get("generation_active")
            and not media.get("media_pending")
            and int(media.get("busy_count") or 0) == 0
            and not composer_text
        )
        detail = {
            "ready": ready,
            "composer_empty": not bool(composer_text),
            "input_visible": bool(composer.get("input_visible")),
            "send_found": bool(composer.get("send_found")),
            "send_visible": bool(composer.get("send_visible")),
            "send_enabled": bool(composer.get("send_enabled")),
            "generation_active": bool(composer.get("generation_active")),
            "media_pending": bool(media.get("media_pending")),
            "busy_count": int(media.get("busy_count") or 0),
            "image_ready": int(media.get("image_ready") or 0),
            "image_count": int(media.get("image_count") or 0),
        }
        return ready, detail

    def _send_protocol_recovery_probe(self, expected: dict) -> dict:
        """Send exactly one bounded same-turn protocol repair."""
        prompt = self._protocol_recovery_prompt(expected)
        stage_prefix = "protocol_recovery"
        prompt_sha = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]
        self._log_stage(
            stage_prefix + "_probe_begin",
            f"turn_id={expected.get('turn_id', '')} prompt_sha={prompt_sha}",
        )

        # Revalidate idle state immediately before touching the composer.
        ready, admission = self._protocol_recovery_admission_state()
        if not ready:
            raise WebScraperStageError(
                "[WEB_PROTOCOL_RECOVERY_NOT_READY] recovery admission changed before send; "
                + json.dumps(admission, ensure_ascii=False, sort_keys=True),
                stage="protocol_recovery_not_ready",
                safe_to_retry=False,
            )

        self._write_prompt_to_composer(prompt)
        send_locator = self._wait_for_send_ready(prompt)
        snapshot = self._snapshot_turn_state()
        try:
            self._submit_verified_prompt(send_locator, prompt=prompt)
        except WebScraperStageError as submit_exc:
            delivery = self._reconcile_submit_delivery(snapshot, prompt, timeout_sec=3.0)
            self._log_stage(stage_prefix + "_submit_reconcile", f"classification={delivery}")
            if delivery == "sent":
                pass
            elif delivery == "not_sent":
                raise WebScraperStageError(
                    "[WEB_PROTOCOL_RECOVERY_NOT_SENT] ACK-only recovery click failed and the same probe "
                    "remains intact in composer; no second automatic recovery send will be attempted.",
                    stage="protocol_recovery_not_sent",
                    safe_to_retry=False,
                ) from submit_exc
            else:
                raise
        self._wait_for_user_sent(snapshot)
        self._log_stage(
            stage_prefix + "_probe_sent",
            f"turn_id={expected.get('turn_id', '')} same_logical_turn=true",
        )
        return snapshot

    @classmethod
    def run_protocol_recovery_self_tests(cls) -> dict:
        expected = {
            "run_id": "SA-RECOVERY",
            "turn_id": 4,
            "local_nonce": "nonce-4",
            "ack_result_id": "RES-3",
            "ack_web_ack_id": "WEBACK-3",
        }
        prompt = cls._protocol_recovery_prompt(expected)
        results = {
            "same_turn_marker": "[SMARTAGENT_PROTOCOL_RECOVERY_RETRANSMIT]" in prompt,
            "forbid_regeneration": "Do NOT regenerate" in prompt,
            "contains_local_commit": "[SMARTAGENT_LOCAL_COMMIT]" in prompt,
            "preserves_turn": '"turn_id":4' in prompt,
            "preserves_nonce": '"local_nonce":"nonce-4"' in prompt,
            "preserves_result_ack": '"ack_result_id":"RES-3"' in prompt,
            "preserves_web_ack": '"ack_web_ack_id":"WEBACK-3"' in prompt,
            "visible_request_id": "request_id=SA-RECOVERY" in prompt,
            "visible_round": "round=4" in prompt,
            "single_repair_attempt": "attempt=2" in prompt,
            "no_second_correction_layer": "STALE_ACK_CORRECTION" not in prompt,
        }
        results["all_passed"] = all(results.values())
        return results

    @staticmethod
    def _artifact_scope_from_snapshot(snapshot: dict) -> dict:
        return {
            "assistant_count_before": int(snapshot.get("assistant_count", 0) or 0),
            "last_assistant_fp_before": str(snapshot.get("last_assistant_fp", "") or ""),
            "user_count_before": int(snapshot.get("user_count", 0) or 0),
            "artifact_signatures_before": list(snapshot.get("artifact_signatures_before") or []),
        }

    def _wait_for_response_complete(self, snapshot: dict, protocol_expected: dict = None) -> str:
        """Wait for a fresh assistant response with UI-first, ACK-second gating.

        SmartAgent completion is intentionally two-layered:

          Layer 1 -- UI lifecycle gate (authoritative for activity):
            * thinking / Stop / generation controls must be inactive;
            * image/tool/file/media processing indicators must be inactive;
            * the fresh assistant UI must remain unchanged for
              ``_ui_idle_grace_sec`` continuously.

          Layer 2 -- protocol commit gate (authoritative for execution):
            * only after UI_CONFIRMED_IDLE may rendered text be inspected for a
              matching ``turn_commit``;
            * the commit must match the current LocalAgent run/turn/nonce/result
              acknowledgement chain;
            * the returned text must remain stable for the short protocol
              debounce before LocalAgent may parse/execute it.

        A matching ACK can never override an active/stale-looking WebGPT UI.
        ACK timeout starts only after UI_CONFIRMED_IDLE, so long image generation
        does not consume the protocol timeout budget.  If media/tool activity was
        observed and ChatGPT ends that tool turn without a textual turn_commit,
        LocalAgent stays passive for a 30-second quiet period.  Only then, and
        only while composer/UI remain ready, one same-turn ACK-only retransmission
        is allowed.  It is never repeated automatically.
        """
        self._cancel_requested.clear()
        assistant_turn = self._wait_for_new_assistant_turn(snapshot)

        stable_text = ""
        stable_since = None
        ui_idle_since = None
        ui_idle_signature = ""
        protocol_started_at = None
        last_log = 0.0
        last_signature = ""
        last_progress_at = time.time()
        last_stage = ""
        saw_recovery_eligible_activity = False
        protocol_recovery_attempts = 0
        artifact_save_expected = bool((protocol_expected or {}).get("artifact_save_expected"))
        artifact_scope = self._artifact_scope_from_snapshot(snapshot)
        fresh_artifact_seen = False

        while True:
            self._dismiss_known_blocking_dialogs()
            self._check_cancel_requested("generation")

            # ChatGPT can replace the assistant placeholder while transitioning
            # between thinking, tool/image generation, and final answer states.
            assistants = self._turn_elements("assistant")
            if assistants:
                latest_assistant = assistants[-1]
                latest_fp = self._element_fingerprint(latest_assistant)
                if (
                    len(assistants) > snapshot.get("assistant_count", 0)
                    or latest_fp != snapshot.get("last_assistant_fp", "")
                ):
                    assistant_turn = latest_assistant

            state = self._assistant_activity_state(snapshot, assistant_turn)
            signature = state["progress_signature"]
            now = time.time()

            if signature != last_signature:
                last_signature = signature
                last_progress_at = now

            stalled_for = now - last_progress_at
            active = bool(state["generation_active"])
            media_pending = bool(state["media_pending"])
            if (
                media_pending
                or int(state.get("image_count") or 0) > 0
                or int(state.get("video_count") or 0) > 0
                or int(state.get("canvas_count") or 0) > 0
                or int(state.get("busy_count") or 0) > 0
            ):
                saw_recovery_eligible_activity = True

            # ChatGPT may mount generated images/file cards outside the assistant
            # turn subtree.  Stage 3.1 previously missed those and left
            # recovery_eligible=False forever.  Compare against the pre-submit
            # artifact baseline so only genuinely new UI can unlock recovery.
            if not fresh_artifact_seen and not active and not media_pending:
                fresh_artifact_seen = has_fresh_artifact(self._page, artifact_scope)
                if fresh_artifact_seen:
                    saw_recovery_eligible_activity = True
                    self._log_stage("fresh_artifact_detected", "request-scoped page/assistant artifact appeared")

            # Active UI has absolute priority over ACK.  Do not even inspect
            # smartagent_tool JSON while these states are present.
            if active or media_pending:
                ui_idle_since = None
                ui_idle_signature = ""
                protocol_started_at = None
                stable_text = ""
                stable_since = None

                stall_limit = self._active_generation_emergency_sec
                if stalled_for >= stall_limit:
                    detail = (
                        f"no_progress={stalled_for:.1f}s generation_active={active} "
                        f"media_pending={media_pending} "
                        f"images={state['image_ready']}/{state['image_count']} "
                        f"busy={state['busy_count']} text_len={state['text_len']} "
                        f"mutations={state.get('mutation_count', 0)}"
                    )
                    self._log_stage("emergency_stalled", detail)
                    raise WebScraperStageError(
                        "[WEB_GENERATION_EMERGENCY_STALLED] WebGPT 仍顯示 thinking/generation/media processing，"
                        "但超過 emergency watchdog 都沒有可觀察進展；不讀取 ACK、不執行 action、也不自動重送。 "
                        + detail,
                        stage="generation_emergency_stalled",
                        safe_to_retry=False,
                    )

                if active:
                    current_stage = "generating"
                    detail = (
                        f"UI gate active; no_progress={stalled_for:.1f}s "
                        f"images={state['image_ready']}/{state['image_count']} "
                        f"busy={state['busy_count']} text_len={state['text_len']} "
                        f"mutations={state.get('mutation_count', 0)} "
                        f"watchdog={'warning-only' if stalled_for >= self._active_generation_warn_sec else 'active'}"
                    )
                else:
                    current_stage = "image_generating" if state["image_count"] else "media_processing"
                    detail = (
                        f"UI media gate active; no_progress={stalled_for:.1f}s "
                        f"images={state['image_ready']}/{state['image_count']} "
                        f"busy={state['busy_count']} text_len={state['text_len']} "
                        f"mutations={state.get('mutation_count', 0)} "
                        f"watchdog={'warning-only' if stalled_for >= self._active_generation_warn_sec else 'active'}"
                    )
            else:
                # No active UI flag is present.  Do not immediately trust that
                # snapshot: ChatGPT image/tool UI can mount a few seconds after
                # the text/thinking phase appears to stop.  Any assistant state
                # mutation restarts the continuous idle window.
                if signature != ui_idle_signature:
                    ui_idle_signature = signature
                    ui_idle_since = now
                    protocol_started_at = None
                    stable_text = ""
                    stable_since = None

                idle_for = 0.0 if ui_idle_since is None else now - ui_idle_since
                if idle_for < self._ui_idle_grace_sec:
                    current_stage = "ui_idle_settling"
                    detail = (
                        f"UI inactive but waiting continuous idle grace; idle_for={idle_for:.2f}s/"
                        f"{self._ui_idle_grace_sec:.2f}s images={state['image_ready']}/{state['image_count']} "
                        f"busy={state['busy_count']} mutations={state.get('mutation_count', 0)}"
                    )
                else:
                    # UI_CONFIRMED_IDLE is the only point where the protocol
                    # layer is allowed to inspect SmartAgent JSON/turn_commit.
                    if protocol_started_at is None:
                        protocol_started_at = now
                        self._log_stage(
                            "ui_confirmed_idle",
                            f"idle_for={idle_for:.2f}s turn_id={(protocol_expected or {}).get('turn_id', '')}",
                        )

                    text = self._current_new_response_text(snapshot, assistant_turn)

                    if protocol_expected:
                        matching_commit = self._matching_protocol_commit(text, protocol_expected)
                        protocol_wait = now - protocol_started_at

                        if matching_commit:
                            current_stage = "protocol_commit_settling"
                            if text == stable_text:
                                if stable_since is None:
                                    stable_since = now
                                elif now - stable_since >= self._protocol_commit_stable_sec:
                                    self._log_stage(
                                        "protocol_commit_complete",
                                        f"turn_id={matching_commit.get('turn_id')} "
                                        f"web_ack_id={matching_commit.get('web_ack_id')} chars={len(text)} "
                                        f"ui_idle_for={idle_for:.2f}s images={state['image_ready']}/{state['image_count']}",
                                    )
                                    self._cancel_requested.clear()
                                    return text
                            else:
                                stable_text = text
                                stable_since = now
                            stable_for = 0.0 if stable_since is None else now - stable_since
                            detail = (
                                f"UI confirmed idle + matching turn_commit; stable_for={stable_for:.2f}s "
                                f"protocol_wait={protocol_wait:.1f}s"
                            )
                        else:
                            stable_text = ""
                            stable_since = None

                            diagnostic = self._protocol_commit_diagnostic(text, protocol_expected)
                            if diagnostic.get("kind") == "mismatch":
                                self._log_stage(
                                    "protocol_commit_mismatch",
                                    json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
                                )

                            # Passive-first recovery is attempted at most once
                            # after a long quiet window. A non-empty assistant
                            # response with no commit is eligible even when no
                            # media/tool UI was observed: no local action can have
                            # executed before the commit gate, so asking ChatGPT to
                            # retransmit the SAME decision and outstanding Local
                            # Commit is safe and cannot duplicate local work.
                            # Media/artifact evidence remains useful when the text
                            # envelope itself is empty or selectors missed it.
                            recovery_eligible = bool(
                                text
                                or saw_recovery_eligible_activity
                                or fresh_artifact_seen
                                or artifact_save_expected
                            )
                            recovery_due = bool(
                                recovery_eligible
                                and protocol_recovery_attempts < self._protocol_recovery_max_attempts
                                and protocol_wait >= self._protocol_recovery_quiet_sec
                            )
                            if recovery_due:
                                recovery_ready, recovery_state = self._protocol_recovery_admission_state()
                                if recovery_ready:
                                    protocol_recovery_attempts += 1
                                    self._log_stage(
                                        "protocol_recovery_armed",
                                        f"quiet={protocol_wait:.1f}s attempt={protocol_recovery_attempts}/"
                                        f"{self._protocol_recovery_max_attempts} state="
                                        + json.dumps(recovery_state, ensure_ascii=False, sort_keys=True),
                                    )
                                    recovery_snapshot = self._send_protocol_recovery_probe(protocol_expected)

                                    # The probe creates a fresh browser user/assistant
                                    # pair but deliberately keeps the same logical
                                    # protocol identity.  Start UI-first gating again
                                    # from that fresh assistant turn.
                                    snapshot = recovery_snapshot
                                    assistant_turn = self._wait_for_new_assistant_turn(snapshot)
                                    stable_text = ""
                                    stable_since = None
                                    ui_idle_since = None
                                    ui_idle_signature = ""
                                    protocol_started_at = None
                                    last_signature = ""
                                    last_progress_at = time.time()
                                    last_stage = "protocol_recovery_probe_sent"
                                    last_log = 0.0
                                    self._log_stage(
                                        "protocol_recovery_wait",
                                        f"turn_id={protocol_expected.get('turn_id', '')} waiting matching commit",
                                    )
                                    continue
                                else:
                                    current_stage = "protocol_recovery_wait_ready"
                                    detail = (
                                        f"assistant response missing commit; quiet={protocol_wait:.1f}s but composer/UI "
                                        "is not safe for the one allowed recovery probe; state="
                                        + json.dumps(recovery_state, ensure_ascii=False, sort_keys=True)
                                    )
                            else:
                                current_stage = "waiting_protocol_commit" if text else "waiting_final_content"
                                detail = (
                                    f"UI confirmed idle; waiting matching turn_commit; text_len={len(text)} "
                                    f"protocol_wait={protocol_wait:.1f}s "
                                    f"recovery_eligible={recovery_eligible} "
                                    f"recovery_media_seen={saw_recovery_eligible_activity} "
                                    f"fresh_artifact_seen={fresh_artifact_seen} "
                                    f"artifact_save_expected={artifact_save_expected} "
                                    f"recovery_attempts={protocol_recovery_attempts} "
                                    f"commit_state={diagnostic.get('kind', 'missing')}"
                                )

                            if (
                                protocol_recovery_attempts >= self._protocol_recovery_max_attempts
                                and protocol_wait >= self._protocol_recovery_failure_quiet_sec
                            ):
                                raise WebScraperStageError(
                                    "[WEB_PROTOCOL_ACK_RECOVERY_FAILED] 同一 request/round 的唯一 ACK 修正"
                                    "仍未取得 matching turn_commit；不執行任何 action，也不再自動送出。 "
                                    f"request_id={protocol_expected.get('run_id', '')} "
                                    f"round={protocol_expected.get('turn_id', '')} "
                                    f"attempt=2 commit_state={diagnostic.get('kind', 'missing')}",
                                    stage="protocol_ack_recovery_failed",
                                    safe_to_retry=False,
                                )

                            if protocol_wait >= self._protocol_commit_timeout_sec:
                                raise WebScraperStageError(
                                    "[WEB_PROTOCOL_COMMIT_TIMEOUT] UI 已 confirmed idle，但 ACK hard timeout 內仍未取得 "
                                    "matching turn_commit；不執行任何 action，也不自動重送原需求。若 media/tool "
                                    "recovery 已使用，也不會自動送第二次。",
                                    stage="protocol_commit_timeout",
                                    safe_to_retry=False,
                                )
                    elif text:
                        # Standalone / non-SmartAgent compatibility.  Even here,
                        # UI idle is required before normal text stability can
                        # complete the request.
                        current_stage = "settling"
                        if text == stable_text:
                            if stable_since is None:
                                stable_since = now
                            elif now - stable_since >= self._completion_stable_sec:
                                self._log_stage(
                                    "complete",
                                    f"chars={len(text)} ui_idle_for={idle_for:.2f}s "
                                    f"images={state['image_ready']}/{state['image_count']} busy={state['busy_count']}",
                                )
                                self._cancel_requested.clear()
                                return text
                        else:
                            stable_text = text
                            stable_since = now
                        stable_for = 0.0 if stable_since is None else now - stable_since
                        detail = (
                            f"UI confirmed idle; fresh response settling; stable_for={stable_for:.2f}s "
                            f"no_progress={stalled_for:.1f}s"
                        )
                    else:
                        current_stage = "waiting_final_content"
                        stable_text = ""
                        stable_since = None
                        detail = (
                            f"UI confirmed idle but no final content; no_progress={stalled_for:.1f}s "
                            f"images={state['image_ready']}/{state['image_count']} busy={state['busy_count']}"
                        )

                        if stalled_for >= self._generation_stall_sec:
                            raise WebScraperStageError(
                                "[WEB_GENERATION_STALLED] UI 已 confirmed idle，但長時間沒有 final content；"
                                "不回傳半成品/舊回答，也不自動重送。",
                                stage="generation_stalled",
                                safe_to_retry=False,
                            )

            if current_stage != last_stage or now - last_log >= 2.0:
                self._log_stage(current_stage, detail)
                last_log = now
                last_stage = current_stage

            time.sleep(0.25)

    def _extract_response(self) -> str:
        """Legacy/manual extractor. ask() no longer uses this for freshness."""
        try:
            elements = self._page.query_selector_all(self.cfg["response_selector"])
            if elements:
                return (elements[-1].inner_text() or "").strip()

            fallback_selectors = [
                "[data-message-author-role='assistant']",
                ".agent-turn",
                ".model-response-text",
                "model-response",
            ]
            for sel in fallback_selectors:
                elems = self._page.query_selector_all(sel)
                if elems:
                    return (elems[-1].inner_text() or "").strip()

            return "(無法提取回應，請確認頁面選擇器是否需要更新)"
        except Exception as e:
            return f"[提取失敗] {e}"

    def _normalize_attachment_paths(
        self,
        attachment_paths: list = None,
        file_paths: list = None,
        image_paths: list = None,
    ) -> list[str]:
        """Merge generic/legacy attachment arguments into validated file paths.

        ``image_paths`` is kept for backward compatibility.  New callers should
        use ``attachment_paths``; ``file_paths`` is accepted as an alias so the
        SmartAgent adapter can interoperate with either contract.
        """
        merged = []
        for group in (attachment_paths, file_paths, image_paths):
            if not group:
                continue
            if isinstance(group, (str, os.PathLike)):
                group = [group]
            for raw in group:
                if raw is None:
                    continue
                p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
                try:
                    p = p.resolve()
                except Exception:
                    p = p.absolute()
                if not p.exists():
                    raise RuntimeError(f"附件不存在: {p}")
                if not p.is_file():
                    raise RuntimeError(f"附件不是一般檔案: {p}")
                full = str(p)
                if full not in merged:
                    merged.append(full)
        return merged

    def _try_existing_file_inputs(self, path: str) -> bool:
        """Try file inputs belonging to the active composer, never stale page inputs."""
        inputs = self._page.query_selector_all(
            'form[data-type="unified-composer"] input[type="file"], '
            '[data-testid="composer"] input[type="file"], '
            '[data-testid*="composer"] input[type="file"]'
        )
        if not inputs:
            return False

        ranked = []
        for element in inputs:
            try:
                accept = (element.get_attribute("accept") or "").strip().lower()
            except Exception:
                accept = ""
            # Empty accept usually means a generic file picker.  Put image-only
            # inputs last so PDF/Python/Markdown/etc. get a chance to use the
            # generic picker first.
            image_only = bool(accept) and "image/" in accept and not any(
                token in accept
                for token in (
                    ".pdf", ".txt", ".md", ".py", ".doc", ".docx",
                    ".xls", ".xlsx", ".ppt", ".pptx", "application/",
                    "text/", "*/*",
                )
            )
            ranked.append((1 if image_only else 0, element))

        for _score, element in sorted(ranked, key=lambda item: item[0]):
            try:
                element.set_input_files(path)
                return True
            except Exception:
                continue
        return False

    def _find_attach_button(self):
        """Find the composer attachment/plus button across ChatGPT/Gemini UI variants."""
        selectors = [
            'button[data-testid="composer-plus-btn"]',
            'button[aria-label="Attach files"]',
            'button[aria-label="Upload file"]',
            'button[aria-label="Upload"]',
            'button[aria-label="Upload image"]',
            'button[aria-label*="Add files"]',
            'button[aria-label*="Attach"]',
            'button[aria-label*="Upload"]',
            'button[tooltip="Attach files"]',
        ]
        for selector in selectors:
            try:
                button = self._page.query_selector(selector)
                if button and button.is_visible():
                    return button
            except Exception:
                continue
        return None

    def _upload_one_attachment_once(self, path: str) -> None:
        """Upload one file using either an existing input or a file chooser."""
        if self._try_existing_file_inputs(path):
            return

        button = self._find_attach_button()
        if button is None:
            raise RuntimeError("找不到可用的附件 input 或上傳按鈕")

        # Some UI versions open the native chooser immediately; others first
        # open a menu (for example, "Upload from computer").  Support both.
        chooser_completed = False
        try:
            with self._page.expect_file_chooser(timeout=1800) as fc_info:
                button.click()
            fc_info.value.set_files(path)
            chooser_completed = True
        except Exception:
            # The click may have opened a menu rather than a chooser.
            pass

        if chooser_completed:
            return

        time.sleep(0.35)

        # A menu-opening click can create/reveal a file input dynamically.
        if self._try_existing_file_inputs(path):
            return

        menu_selectors = [
            'text="Upload from computer"',
            'text="Upload file"',
            'text="Attach files"',
            '[role="menuitem"]:has-text("Upload")',
            '[role="menuitem"]:has-text("Attach")',
        ]
        for selector in menu_selectors:
            try:
                item = self._page.query_selector(selector)
                if not item or not item.is_visible():
                    continue
                with self._page.expect_file_chooser(timeout=3000) as fc_info:
                    item.click()
                fc_info.value.set_files(path)
                return
            except Exception:
                continue

        raise RuntimeError(f"無法觸發附件選擇器: {Path(path).name}")

    def _upload_one_attachment(self, path: str) -> None:
        """Select one attachment exactly once; never auto-reselect it."""
        self._upload_one_attachment_once(path)

    def _attachment_ui_snapshot(self, paths: list[str]) -> dict:
        """Read observable attachment state without deciding how long to wait."""
        if self.service == "chatgpt":
            selector = 'button[data-testid="send-button"]'
        else:
            selector = self.cfg["done_selector"]
        btn = self._page.query_selector(selector)
        send_ready = bool(btn and btn.is_visible() and btn.is_enabled())

        # Only the active composer is authoritative.  Whole-page text can
        # contain filenames from earlier turns and caused false READY states.
        composer_state = {"text": "", "busy": [], "chips": [], "chip_records": [], "alerts": []}
        try:
            composer_state = self._page.evaluate(r"""() => {
                const send = document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
                const composer = send?.closest('form') || send?.closest('[data-testid*="composer"]') ||
                    document.querySelector('form[data-type="unified-composer"], [data-testid="composer"]');
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const alerts = [...document.querySelectorAll('[role="alert"], [data-sonner-toast], [data-testid*="toast"], [class*="toast"]')]
                    .filter(visible).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
                if (!composer) return {text: '', busy: [], chips: [], chip_records: [], alerts};
                const busySelector = [
                    '[role="progressbar"]', '[aria-busy="true"]',
                    '[data-state="loading"]', '[data-state="uploading"]',
                    '[data-state="processing"]', '[data-state="pending"]',
                    '[class*="uploading"]', '[class*="processing"]',
                    '[class*="spinner"]', '[class*="animate-spin"]',
                    '[class*="loading"]', '[class*="progress"]'
                ].join(',');
                const chipElements = [...composer.querySelectorAll('[data-testid*="attachment"], [class*="attachment"], [data-testid="file-thumbnail"], [data-testid="composer-file"], [data-file-name]')]
                    .filter(visible);
                const progressPercent = node => {
                    const value = Number(node.getAttribute('aria-valuenow') ?? node.value);
                    const maximum = Number(node.getAttribute('aria-valuemax') ?? node.max ?? 100);
                    return Number.isFinite(value) && Number.isFinite(maximum) && maximum > 0
                        ? Math.round((value / maximum) * 100) : null;
                };
                const activelyBusy = node => visible(node) && !(
                    node.getAttribute('role') === 'progressbar' && progressPercent(node) >= 100
                );
                const isRemoveControl = node => !!node.closest(
                    'button[aria-label*="Remove"], button[aria-label*="Delete"], '
                    + 'button[aria-label*="移除"], button[aria-label*="刪除"], button[data-testid*="remove"]'
                );
                const isUploadRing = node => {
                    if (!visible(node) || isRemoveControl(node)) return false;
                    if (node.getAttribute('role') === 'progressbar' && progressPercent(node) >= 100) return false;
                    const style = getComputedStyle(node);
                    const animated = style.animationName && style.animationName !== 'none'
                        && style.animationPlayState !== 'paused';
                    const semantics = [
                        node.getAttribute('role'), node.getAttribute('aria-label'),
                        node.getAttribute('data-state'), node.className?.baseVal || node.className || ''
                    ].filter(Boolean).join(' ').toLowerCase();
                    const semanticBusy = /(progress|upload|loading|processing|pending|spinner|animate-spin)/.test(semantics);
                    const svgAnimation = !!node.querySelector?.('animate, animateTransform');
                    const tag = String(node.tagName || '').toLowerCase();
                    const strokeDash = String(style.strokeDasharray || '').toLowerCase();
                    const svgProgressRing = (tag === 'circle' || tag === 'svg') && (
                        semanticBusy || animated || svgAnimation
                        || (strokeDash && strokeDash !== 'none' && strokeDash !== '0px')
                    );
                    const conicRing = String(style.backgroundImage || '').includes('conic-gradient');
                    return activelyBusy(node) && (semanticBusy || animated || svgAnimation || svgProgressRing || conicRing);
                };
                const indicatorSelector = busySelector + ', svg, circle, [style*="conic-gradient"]';
                const uploadRings = [...composer.querySelectorAll(indicatorSelector)].filter(isUploadRing);
                const busy = uploadRings.map(el =>
                    el.getAttribute('aria-valuenow') || el.getAttribute('data-state')
                    || el.getAttribute('aria-label') || el.tagName
                );
                const chipRecords = chipElements.map(el => {
                        const child = el.querySelector('[data-file-name], [aria-label], [title]');
                        const descriptor = [
                            el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
                            el.getAttribute('data-file-name'), child?.getAttribute('data-file-name'),
                            child?.getAttribute('aria-label'), child?.getAttribute('title')
                        ].filter(Boolean).join(' ').trim();
                        const localRings = [...el.querySelectorAll(indicatorSelector)].filter(isUploadRing);
                        const progressValues = [el, ...el.querySelectorAll('[aria-valuenow], progress')]
                            .map(node => {
                                return progressPercent(node);
                            }).filter(value => value !== null);
                        const stateText = [
                            el.getAttribute('data-state'), el.getAttribute('aria-label'),
                            el.className?.baseVal || el.className || ''
                        ].filter(Boolean).join(' ').toLowerCase();
                        const explicitComplete = progressValues.some(value => value >= 100) ||
                            /(^|[\s_-])(complete|completed|success|ready|uploaded)([\s_-]|$)/.test(stateText);
                        return {
                            descriptor,
                            processing: localRings.length > 0,
                            explicit_complete: explicitComplete,
                            progress: progressValues,
                        };
                    }).filter(record => record.descriptor);
                const chips = chipRecords.map(record => record.descriptor);
                return {
                    text: composer.innerText || '', busy, upload_ring_count: uploadRings.length,
                    chips, chip_records: chipRecords, alerts
                };
            }""") or composer_state
        except Exception:
            pass
        composer_text = str(composer_state.get("text", ""))
        busy_details = list(composer_state.get("busy", []) or [])
        attachment_chips = list(composer_state.get("chips", []) or [])
        chip_records = list(composer_state.get("chip_records", []) or [])
        alert_text = "\n".join(str(value) for value in (composer_state.get("alerts", []) or []))
        expected_names = [Path(path).name for path in paths]
        attachment_text = "\n".join(str(value) for value in attachment_chips)
        visible_names = [
            name for name in expected_names
            if name and (name in composer_text or name in attachment_text)
        ]
        attachment_states = {}
        for name in expected_names:
            records = [
                record for record in chip_records
                if name and name in str((record or {}).get("descriptor", ""))
            ]
            attachment_states[name] = {
                "seen": bool(name and (name in composer_text or records)),
                "processing": any(bool((record or {}).get("processing")) for record in records),
                "explicit_complete": any(bool((record or {}).get("explicit_complete")) for record in records),
                "progress": [
                    value for record in records
                    for value in list((record or {}).get("progress", []) or [])
                ],
            }

        error_text = ""
        error_terms = (
            "too many files", "too many attachments", "file limit", "attachment limit",
            "upload limit", "reached your upload limit", "upload failed", "couldn't upload",
            "檔案過多", "附件過多", "超過檔案", "上傳限制", "附件限制",
            "上傳失敗", "無法上傳",
        )
        lowered_body = (composer_text + "\n" + alert_text).lower()
        for term in error_terms:
            if term in lowered_body:
                error_text = term
                break
        set_mismatch = ""
        unexpected_attachment_chips = []
        if attachment_chips:
            expected_counts = {name: expected_names.count(name) for name in set(expected_names)}
            observed_counts = {
                name: sum(1 for value in attachment_chips if name in str(value))
                for name in expected_counts
            }
            if len(attachment_chips) != len(expected_names) or observed_counts != expected_counts:
                set_mismatch = (
                    f"attachment_set_mismatch:expected={len(expected_names)},"
                    f"chips={len(attachment_chips)},counts={observed_counts}"
                )
            # ChatGPT commonly exposes the same logical attachment through
            # nested DOM nodes, so raw chip count is not a readiness signal.
            # Only a chip that matches none of the requested filenames is an
            # actual unexpected attachment and must keep the send gate closed.
            unexpected_attachment_chips = [
                str(value) for value in attachment_chips
                if not any(name and name in str(value) for name in expected_names)
            ]

        upload_ring_count = int(
            composer_state.get("upload_ring_count", len(busy_details)) or 0
        )

        if error_text:
            state = "REJECTED"
        elif (
            send_ready
            and upload_ring_count == 0
            and len(visible_names) == len(expected_names)
            and not unexpected_attachment_chips
        ):
            state = "READY"
        elif visible_names or busy_details:
            state = "PROCESSING"
        else:
            state = "SELECTED"
        return {
            "state": state,
            "send_ready": send_ready,
            "visible_names": visible_names,
            "attachment_chips": attachment_chips,
            "attachment_states": attachment_states,
            "busy_details": busy_details,
            "upload_ring_count": upload_ring_count,
            "set_mismatch": set_mismatch,
            "unexpected_attachment_chips": unexpected_attachment_chips,
            "alert_text": alert_text,
            "error": error_text,
        }

    def _clear_composer_attachments(self) -> None:
        """Best-effort transaction rollback scoped to the active composer."""
        try:
            self._page.evaluate("""() => {
                const send = document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
                const composer = send?.closest('form') || send?.closest('[data-testid*="composer"]') ||
                    document.querySelector('form[data-type="unified-composer"], [data-testid="composer"]');
                if (!composer) return 0;
                const selectors = [
                    'button[aria-label*="Remove"]', 'button[aria-label*="移除"]',
                    'button[data-testid*="remove"]', 'button[aria-label*="Delete"]'
                ];
                let count = 0;
                for (let round = 0; round < 30; round++) {
                    const button = selectors.map(s => composer.querySelector(s)).find(Boolean);
                    if (!button) break;
                    button.click(); count++;
                }
                const input = composer.querySelector('input[type="file"]');
                if (input) input.value = '';
                return count;
            }""")
        except Exception:
            pass

    def _wait_for_attachment_ui(
        self,
        paths: list[str],
        timeout_sec: float = 600.0,
        no_progress_timeout_sec: float = 30.0,
        stable_ready_sec: float = 2.0,
    ) -> bool:
        """Wait passively for upload rings to disappear, bounded only by hard timeout."""
        if not paths:
            return True
        started = time.monotonic()
        hard_deadline = started + max(1.0, float(timeout_sec))
        last_signature = None
        last_snapshot = {"state": "SELECTED"}
        ready_since = None
        stable_required = max(0.0, float(stable_ready_sec))
        self._last_attachment_wait_reason = ""

        while time.monotonic() < hard_deadline:
            try:
                snapshot = self._attachment_ui_snapshot(paths)
                last_snapshot = snapshot
                state = str(snapshot.get("state", "SELECTED"))
                attachment_states = dict(snapshot.get("attachment_states", {}) or {})
                signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
                now = time.monotonic()
                if signature != last_signature:
                    last_signature = signature
                    self._log_stage(
                        "attachment_state",
                        f"state={state} files={len(paths)} "
                        f"visible={len(snapshot.get('visible_names', []))} "
                        f"upload_rings={int(snapshot.get('upload_ring_count', 0) or 0)} "
                        f"chips={len(snapshot.get('attachment_chips', []))} "
                        f"unexpected_chips={len(snapshot.get('unexpected_attachment_chips', []))} "
                        f"send_ready={bool(snapshot.get('send_ready'))}",
                    )
                if state == "READY":
                    if ready_since is None:
                        ready_since = now
                        self._log_stage(
                            "attachment_ready_settling",
                            f"files={len(paths)} mode=upload_ring_absent "
                            f"stable_required_sec={stable_required:.2f}",
                        )
                    if now - ready_since >= stable_required:
                        self._log_stage(
                            "attachment_stable_ready",
                            f"files={len(paths)} mode=upload_ring_absent "
                            f"stable_sec={now-ready_since:.2f}",
                        )
                        return True
                else:
                    ready_since = None
                if state == "REJECTED":
                    self._last_attachment_wait_reason = (
                        f"attachment_rejected: {snapshot.get('error', 'unknown')}"
                    )
                    return False
            except Exception as exc:
                # A transient DOM repaint is not itself an upload failure.  It
                # becomes a stall only if no observable state returns.
                ready_since = None
                last_snapshot = {"state": "INSPECTION_RETRY", "error": type(exc).__name__}
            time.sleep(0.25)

        self._last_attachment_wait_reason = (
            f"attachment_hard_timeout: state={last_snapshot.get('state', 'unknown')}; "
            f"elapsed_sec={time.monotonic() - started:.1f}"
        )
        return False

    def _upload_attachments(self, paths: list[str]) -> None:
        """Upload each file once and wait passively; never clear/reselect on timeout."""
        if not paths:
            return

        print(f"  [WebScraper] 準備上傳 {len(paths)} 個附件...")
        self._emit_status(
            "PREPARING_ATTACHMENTS", message="準備附件",
            progress={"current": 0, "total": len(paths)},
        )
        uploaded = []
        for index, path in enumerate(paths, 1):
            self._emit_status(
                "UPLOADING_ATTACHMENT", message="上傳附件中",
                progress={"current": index, "total": len(paths), "item": Path(path).name},
            )
            self._upload_one_attachment(path)
            uploaded.append(path)
            if not self._wait_for_attachment_ui(
                uploaded, timeout_sec=600.0, no_progress_timeout_sec=30.0, stable_ready_sec=1.0
            ):
                raise RuntimeError(
                    f"附件未就緒: {Path(path).name}; "
                    f"{getattr(self, '_last_attachment_wait_reason', 'unknown')}; "
                    "stage=attachment_ready; no_auto_retry=true"
                )
        # Final aggregate barrier catches replacement, duplicate, missing, or
        # still-uploading chips without mutating the composer.
        if not self._wait_for_attachment_ui(
            paths, timeout_sec=600.0, no_progress_timeout_sec=30.0, stable_ready_sec=2.0
        ):
            raise RuntimeError(
                "附件集合未完整就緒；禁止送出 prompt，且不自動重傳: "
                + getattr(self, "_last_attachment_wait_reason", "unknown")
            )

        print("  [WebScraper] 附件上傳就緒。")
        self._emit_status(
            "ATTACHMENT_READY", message="附件已就緒",
            progress={"current": len(paths), "total": len(paths)},
        )

    def ask(
        self,
        prompt: str,
        new_conversation: bool = False,
        image_paths: list = None,
        attachment_paths: list = None,
        file_paths: list = None,
        protocol_expected: dict = None,
    ) -> str:
        """Send one prompt with composer acknowledgement and fresh-response gating.

        ChatGPT is treated as a stateful UI, not a synchronous text box.  The
        request crosses four explicit gates: composer verified -> send ready ->
        new user turn -> new assistant turn/complete response.  No old response
        is returned and no ambiguous post-compose/post-submit failure is blindly
        retried by the manager.
        """
        if self._page is None:
            raise WebScraperStageError(
                "Call start() first",
                stage="prepare",
                safe_to_retry=True,
            )
        if time.time() < self._rate_limited_until:
            remaining = int(max(1, self._rate_limited_until - time.time()))
            raise WebScraperStageError(
                f"CHATGPT_RATE_LIMITED: cooldown_remaining_sec={remaining}",
                stage="rate_limit", safe_to_retry=False,
            )

        # A new ask owns a fresh cancellation lifecycle. A previous cooperative
        # cancel must never poison the next user request.
        self._cancel_requested.clear()
        self._last_artifact_scope = None
        self._reconcile_pending_submit_before_request()
        self._set_request_state("READY_IDLE", "request_begin")

        submit_attempted = False
        composer_claimed = False
        attachments = []
        stage = "prepare"
        prompt_sha = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]
        _debug_log(
            f"ASK_BEGIN service={self.service} prompt_len={len(prompt)} "
            f"prompt_sha={prompt_sha} new_conversation={new_conversation}"
        )
        try:
            attachments = self._normalize_attachment_paths(
                attachment_paths=attachment_paths,
                file_paths=file_paths,
                image_paths=image_paths,
            )

            if new_conversation:
                stage = "navigate"
                self._page.goto(
                    self.cfg["new_chat_url"],
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                time.sleep(1.5)

            # Never stack a new request on a still-running prior generation.
            stage = "preflight_generation"
            self._wait_for_idle_before_submit()

            # Composer gate.  For ChatGPT this uses a live Locator + DOM focus +
            # keyboard.insert_text(), avoiding ElementHandle.fill() entirely.
            stage = "composer"
            self._set_request_state("PREPARING_COMPOSER")
            mutated = self._write_prompt_to_composer(prompt)
            # Even if the exact prompt was already present from a prior partial
            # attempt, this call now owns that draft and must not blind-retry it.
            composer_claimed = True
            self._log_stage(
                "composer_claimed",
                f"mutated={mutated} prompt_sha={prompt_sha}",
            )

            stage = "attachments"
            self._set_request_state("UPLOADING_ATTACHMENTS")
            self._upload_attachments(attachments)
            if attachments:
                self._set_request_state("ATTACHMENT_STABLE_READY")

            # Attachments can transiently disable Send; re-check both composer
            # integrity and send readiness after all requested files settle.
            stage = "send_ready"
            self._set_request_state("READY_TO_SUBMIT")
            send_locator = self._wait_for_send_ready(prompt)

            # Snapshot immediately before the one allowed submit. Anything
            # already present now is stale by definition for this ask().
            snapshot = self._snapshot_turn_state()
            snapshot["artifact_signatures_before"] = snapshot_artifact_signatures(self._page)
            self._log_stage(
                "snapshot",
                f"user={snapshot['user_count']} assistant={snapshot['assistant_count']} "
                f"response={snapshot['response_count']} artifacts={len(snapshot['artifact_signatures_before'])}",
            )

            stage = "submit"
            self._pending_submit_context = {"snapshot": snapshot, "prompt": prompt, "attachments": list(attachments)}
            try:
                self._submit_verified_prompt(
                    send_locator, prompt=prompt, attachment_paths=attachments
                )
                submit_attempted = bool(getattr(self, "_submit_click_attempted", False))
            except WebScraperStageError as submit_exc:
                submit_attempted = bool(getattr(self, "_submit_click_attempted", False))
                delivery = self._reconcile_submit_delivery(snapshot, prompt, timeout_sec=3.0)
                self._log_stage("submit_reconcile", f"classification={delivery}")
                if delivery == "sent":
                    pass
                elif delivery == "not_sent":
                    recovered = self._recover_not_sent_to_ready_idle(prompt, attachments)
                    raise WebScraperStageError(
                        "[WEB_SUBMIT_NOT_SENT] reconciliation 確認未送出；" + ("已回復 READY_IDLE。" if recovered else "cleanup 未完成。"),
                        stage="submit_not_sent", safe_to_retry=False,
                    ) from submit_exc
                else:
                    self._set_request_state("RECOVERY_REQUIRED", "submit_ambiguous")
                    raise submit_exc
            self._log_composer_state("composer_immediate_post_submit")

            # Delivery acknowledgement gate: no assistant content is eligible
            # until a genuinely new user turn is observed.
            stage = "user_sent"
            try:
                self._wait_for_user_sent(snapshot)
            except WebScraperStageError as user_exc:
                delivery = self._reconcile_submit_delivery(snapshot, prompt, timeout_sec=3.0)
                self._log_stage("user_sent_reconcile", f"classification={delivery}")
                if delivery == "sent":
                    pass
                elif delivery == "not_sent":
                    recovered = self._recover_not_sent_to_ready_idle(prompt, attachments)
                    raise WebScraperStageError(
                        "[WEB_SEND_NOT_SENT_RECOVERED] user-turn ACK timeout，但確認未送出；" + ("已回復 READY_IDLE。" if recovered else "cleanup 未完成。"),
                        stage="user_sent_not_sent", safe_to_retry=False,
                    ) from user_exc
                else:
                    self._set_request_state("RECOVERY_REQUIRED", "user_sent_ambiguous")
                    raise
            self._pending_submit_context = None
            self._set_request_state("GENERATING")

            print(f"  [WebScraper] 等待 {self.service} 回應...", flush=True)
            stage = "generation"
            response = self._wait_for_response_complete(snapshot, protocol_expected=protocol_expected)
            # Preserve the ORIGINAL pre-submit assistant boundary even if protocol
            # recovery created an additional assistant control turn. A later
            # download_artifact may use any media created after this boundary,
            # but can never fall back to an older turn.
            self._last_artifact_scope = {
                "assistant_count_before": int(snapshot.get("assistant_count", 0) or 0),
                "last_assistant_fp_before": str(snapshot.get("last_assistant_fp", "") or ""),
                "user_count_before": int(snapshot.get("user_count", 0) or 0),
                "request_prompt_sha": prompt_sha,
                "artifact_signatures_before": list(snapshot.get("artifact_signatures_before") or []),
                "captured_at": time.time(),
            }
            self._set_request_state("COMPLETE")
            self._pending_submit_context = None
            self._submit_click_attempted = False
            self._set_request_state("READY_IDLE", "request_complete")
            print("  [WebScraper] 回應完成", flush=True)
            return response

        except KeyboardInterrupt:
            # Ctrl+C is a request to stop the current WebGPT action, not to tear
            # down the persistent browser/session. Try cooperative UI cancel
            # immediately while this scraper still owns the active page, then
            # let SmartAgent return control to the CLI.
            _debug_log(
                f"ASK_KEYBOARD_INTERRUPT stage={stage} submit_attempted={submit_attempted} "
                f"composer_claimed={composer_claimed}"
            )
            try:
                self.cancel_current_generation()
            except Exception as cancel_exc:
                _debug_log(
                    f"ASK_KEYBOARD_INTERRUPT_CANCEL_FAILED "
                    f"type={type(cancel_exc).__name__} message={str(cancel_exc)[:400]}"
                )
            raise
        except WebScraperStageError as exc:
            if composer_claimed and not submit_attempted and getattr(self, "_request_state", "") != "READY_IDLE":
                self._reset_pre_submit_to_ready_idle(attachments)
            # Once this prompt has occupied the composer, fail closed unless the
            # error itself is from an earlier stage. This prevents restart from
            # duplicating or racing a partially-staged request.
            if composer_claimed and exc.safe_to_retry:
                exc.safe_to_retry = False
            _debug_log(
                f"ASK_STAGE_ERROR stage={exc.stage} safe_to_retry={exc.safe_to_retry} "
                f"type={type(exc).__name__} message={str(exc).splitlines()[0][:600]}"
            )
            raise
        except Exception as exc:
            safe_to_retry = not submit_attempted and not composer_claimed
            _debug_log(
                f"ASK_EXCEPTION stage={stage} submit_attempted={submit_attempted} "
                f"composer_claimed={composer_claimed} type={type(exc).__name__} "
                f"message={str(exc).splitlines()[0][:600]}"
            )
            raise WebScraperStageError(
                f"[WEB_SCRAPER_ERROR] stage={stage}: {type(exc).__name__}: {str(exc).splitlines()[0][:500]}",
                stage=stage,
                safe_to_retry=safe_to_retry,
            ) from exc

    def _download_latest_artifact(self, output_path: str, timeout_sec: float = 45.0, expected_filename: str = "") -> str:
        """Compatibility method delegating all download logic to artifact_transfer."""
        if not self._last_artifact_scope:
            raise RuntimeError("NO_CURRENT_REQUEST_ARTIFACT_SCOPE: refuse stale/page-global artifact fallback")
        return download_latest_artifact(
            self._page,
            self._browser,
            output_path,
            timeout_sec=timeout_sec,
            expected_filename=expected_filename,
            scope=self._last_artifact_scope, strict_scope=True, popup_guard=self._dismiss_known_blocking_dialogs,
        )

    def download_latest_artifact(self, output_path: str, timeout_sec: float = 45.0, expected_filename: str = "") -> dict:
        """Download the latest file/generated image with structured evidence.

        This is the generic Stage-2 entry point used by SmartAgent when the
        WebGPT turn itself produced a file or image (not only web_edit_file).
        """
        if self.service != "chatgpt":
            return {"status": "ARTIFACT_DOWNLOAD_UNAVAILABLE", "error": "目前 generic artifact download 僅支援 ChatGPT"}
        try:
            if not self._last_artifact_scope:
                return {
                    "status": "ARTIFACT_DOWNLOAD_FAILED",
                    "error": "NO_CURRENT_REQUEST_ARTIFACT_SCOPE: refuse stale/page-global artifact fallback",
                }
            self._dismiss_known_blocking_dialogs()
            result = download_latest_artifact_with_evidence(
                self._page, self._browser, output_path,
                timeout_sec=timeout_sec, expected_filename=expected_filename,
                scope=self._last_artifact_scope, strict_scope=True, popup_guard=self._dismiss_known_blocking_dialogs,
            )
            return {**result, "status": "ARTIFACT_DOWNLOAD_SUCCESS"}
        except Exception as exc:
            action = self._request_agent2_action(
                task_phase="ARTIFACT_DOWNLOAD", observed_state="PREVIEW_OPEN",
                error_code="DOWNLOAD_STRATEGIES_EXHAUSTED",
                detail=f"{type(exc).__name__}: {exc}", retry_budget=1,
            )
            if action in {"PRESS_ESCAPE", "DISMISS_DIALOG"}:
                self._apply_agent2_ui_action(action)
            if action in {"RETRY_ONCE", "PRESS_ESCAPE", "DISMISS_DIALOG"}:
                try:
                    result = download_latest_artifact_with_evidence(
                        self._page, self._browser, output_path,
                        timeout_sec=min(20.0, timeout_sec), expected_filename=expected_filename,
                        scope=self._last_artifact_scope, strict_scope=True,
                        popup_guard=self._dismiss_known_blocking_dialogs,
                    )
                    return {**result, "status": "ARTIFACT_DOWNLOAD_SUCCESS"}
                except Exception as retry_exc:
                    exc = retry_exc
            return {
                "status": "ARTIFACT_DOWNLOAD_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def edit_file(self, source_path: str, instruction: str, output_path: str = None, run_id: str = None) -> dict:
        if self.service!='chatgpt': return {'status':'WEB_DIRECT_EDIT_UNAVAILABLE','error':'僅支援 ChatGPT web file fallback'}
        src=Path(source_path).expanduser().resolve()
        if not src.is_file(): return {'status':'WEB_DIRECT_EDIT_UNAVAILABLE','error':f'來源檔案不存在: {src}'}
        dst=Path(output_path).expanduser().resolve() if output_path else src
        before=file_evidence(src)['sha256']
        prompt=('[SmartAgent Web File Fallback]\nRUN_ID='+str(run_id or '')+'\nThe attached file is the source of truth. '+
                'Apply the requested modification directly to it. Do not return smartagent_tool JSON, patches, base64, or source blocks. '+
                'Return one complete downloadable file artifact and preserve its original format unless explicitly requested otherwise.\n\nRequested change:\n'+instruction)
        self.ask(prompt,attachment_paths=[str(src)],new_conversation=False)
        if not self._last_artifact_scope:
            raise RuntimeError("NO_CURRENT_REQUEST_ARTIFACT_SCOPE: web_edit_file refuses stale artifact fallback")
        try:
            transfer = download_latest_artifact_with_evidence(
                self._page, self._browser, str(dst), timeout_sec=45.0, expected_filename=dst.name,
                scope=self._last_artifact_scope, strict_scope=True, popup_guard=self._dismiss_known_blocking_dialogs,
            )
        except Exception as exc:
            action = self._request_agent2_action(
                task_phase="ARTIFACT_DOWNLOAD", observed_state="PREVIEW_OPEN",
                error_code="DOWNLOAD_STRATEGIES_EXHAUSTED",
                detail=f"{type(exc).__name__}: {exc}", retry_budget=1,
            )
            if action not in {"RETRY_ONCE", "PRESS_ESCAPE", "DISMISS_DIALOG"}:
                raise
            if action in {"PRESS_ESCAPE", "DISMISS_DIALOG"}:
                self._apply_agent2_ui_action(action)
            transfer = download_latest_artifact_with_evidence(
                self._page, self._browser, str(dst), timeout_sec=20.0, expected_filename=dst.name,
                scope=self._last_artifact_scope, strict_scope=True, popup_guard=self._dismiss_known_blocking_dialogs,
            )
        actual=Path(transfer['path']).resolve(); data=actual.read_bytes()
        if not data: raise RuntimeError('下載 artifact 為空檔案')
        h=file_evidence(actual)['sha256']
        return {
            'status':'WEB_DIRECT_EDIT_SUCCESS',
            'download_path':str(actual),
            'source_hash_before':before,
            'artifact_hash':h,
            'output_hash_after':h,
            'download_method':transfer.get('method',''),
            'download_attempts':transfer.get('attempts',[]),
        }

    def close(self):
        """Close browser (profile/cookies are saved automatically)."""
        self._cancel_requested.set()
        if self._attached_over_cdp:
            if self._page and self._owns_attached_page:
                try:
                    self._page.close()
                except Exception:
                    pass
        elif self._browser:
            self._browser.close()
        if getattr(self, "_isolated_browser", None):
            try:
                self._isolated_browser.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()
        self._page = None
        self._browser = None
        self._cdp_browser = None
        self._attached_over_cdp = False
        self._owns_attached_page = False
        self._isolated_browser = None
        self._pw = None
        self._activity_observer_installed = False
        print(f"[WebScraper] 瀏覽器已關閉（登入狀態已儲存）")

    def close_conversation_page(self, target_url: str) -> bool:
        """Explicitly close only pages matching one canonical conversation ID."""
        target_id = self._conversation_id_from_url(target_url)
        if not target_id or self._browser is None:
            return False
        closed = False
        for page in list(self._browser.pages):
            page_id = self._conversation_id_from_url(
                str(getattr(page, "url", "") or "")
            )
            if page_id != target_id:
                continue
            try:
                page.close(run_before_unload=False)
                closed = True
            except Exception:
                continue
        if (
            self._page is not None
            and self._conversation_id_from_url(
                str(getattr(self._page, "url", "") or "")
            )
            == target_id
        ):
            self._page = None
            self._owns_attached_page = False
        return closed

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()


# ─── Persistent Scraper Manager ──────────────────────────────────────────────

class ScraperManager:
    """
    Manages a pool of scrapers, keeping browser sessions alive
    so we don't re-open the browser on every query.
    """
    def __init__(self):
        self._scrapers: dict[str, WebLLMScraper] = {}
        # Status callbacks belong to the logical service, not one browser
        # scraper instance.  A safe reconnect replaces the scraper and must
        # keep reporting lifecycle events to the same StatusPublisher.
        self._status_callbacks: dict[str, object] = {}
        self._service_locks: dict[str, threading.Lock] = {}
        self._last_request_end: dict[str, float] = {}
        self._request_cooldown_sec = 3.0
        self._rate_governor = WebGPTRateGovernor(Path(__file__).resolve().parent.parent)

    def _service_lock(self, service: str) -> threading.Lock:
        lock = self._service_locks.get(service)
        if lock is None:
            lock = threading.Lock()
            self._service_locks[service] = lock
        return lock

    def _respect_request_cooldown(self, service: str) -> None:
        # ChatGPT spacing is governed from the exact Send-button timestamp.
        # Do not add a second completion-relative delay after a long answer.
        if service == "chatgpt":
            return
        previous = float(self._last_request_end.get(service, 0.0) or 0.0)
        remaining = self._request_cooldown_sec - (time.monotonic() - previous)
        if remaining > 0:
            _debug_log(f"MANAGER_COOLDOWN service={service} wait={remaining:.3f}s")
            print(f"  [WebScraper] request cooldown {remaining:.1f}s", flush=True)
            time.sleep(remaining)

    def set_status_callback(self, service: str, callback) -> None:
        self._status_callbacks[service] = callback
        scraper = self._scrapers.get(service)
        if scraper is not None:
            scraper.set_status_callback(callback)

    def get_or_create(self, service: str, headless: bool = False, status_callback=None) -> WebLLMScraper:
        if service not in self._scrapers:
            scraper = WebLLMScraper(service=service, headless=headless)
            if service == "chatgpt":
                scraper._rate_governor = self._rate_governor
            if status_callback is not None:
                scraper.set_status_callback(status_callback)
            scraper.start()
            self._scrapers[service] = scraper
        scraper = self._scrapers[service]
        callback = self._status_callbacks.get(service)
        if callback is not None:
            scraper.set_status_callback(callback)
        return scraper

    def ask(
        self,
        service: str,
        prompt: str,
        headless: bool = False,
        new_conversation: bool = False,
        image_paths: list = None,
        attachment_paths: list = None,
        file_paths: list = None,
        protocol_expected: dict = None,
        status_callback=None,
    ) -> str:
        """Send a prompt and retry only failures proven to be pre-submit.

        Blindly retrying after submit can duplicate a successfully-delivered
        prompt when only DOM acknowledgement/response detection failed.
        """
        lock = self._service_lock(service)
        with lock:
            # Bind status ownership at the same boundary as the actual ask.
            # This cannot be skipped by an earlier best-effort setup failure,
            # and get_or_create() reapplies it after a safe reconnect.
            if status_callback is not None:
                self.set_status_callback(service, status_callback)
            self._respect_request_cooldown(service)
            _debug_log(f"MANAGER_ASK_BEGIN service={service} prompt_len={len(prompt)}")
            scraper = self.get_or_create(service, headless)
            kwargs = {
                "new_conversation": new_conversation,
                "image_paths": image_paths,
                "attachment_paths": attachment_paths,
                "file_paths": file_paths,
                "protocol_expected": protocol_expected,
            }
            rate_lease = None
            if service == "chatgpt":
                print(
                    "  [WebGPT Governor] 等待共用 WebGPT 執行時段；其他 Agent1 可繼續在各自視窗顯示狀態。",
                    flush=True,
                )
                emit_status = getattr(scraper, "_emit_status", None)
                if emit_status is not None:
                    emit_status(
                        "WAITING_BRAIN", message="等待共用 WebGPT 執行時段",
                        detail="global_submit_lock",
                    )
                rate_lease = self._rate_governor.acquire(wait=True)
                scraper._rate_submit_lease = rate_lease
                if emit_status is not None:
                    emit_status(
                        "PREPARING_PROMPT", message="已取得 WebGPT 執行時段",
                        detail="global_submit_lock_acquired",
                    )
            try:
                result = scraper.ask(prompt, **kwargs)
                if service == "chatgpt":
                    try:
                        self._rate_governor.record_success()
                    except Exception as exc:
                        _debug_log(
                            f"RATE_SUCCESS_RESET_FAILED type={type(exc).__name__} error={exc}"
                        )
                        print(
                            f"  [WebGPT Governor] 無法重置限流連續計數：{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    if hasattr(scraper, "_rate_limit_dialog_streak"):
                        scraper._rate_limit_dialog_streak = 0
                    if hasattr(scraper, "_rate_limited_until"):
                        scraper._rate_limited_until = 0.0
                _debug_log(f"MANAGER_ASK_SUCCESS service={service} result_len={len(result)}")
                return result
            except WebScraperStageError as exc:
                _debug_log(
                    f"MANAGER_STAGE_ERROR service={service} stage={exc.stage} "
                    f"safe_to_retry={exc.safe_to_retry} type={type(exc).__name__} "
                    f"message={str(exc).splitlines()[0][:600]}"
                )
                if not exc.safe_to_retry:
                    print(
                        f"  [!] {service} stage={exc.stage} 發生 post-submit/ambiguous failure；"
                        "為避免 duplicate prompt，不自動重送。",
                        flush=True,
                    )
                    raise
                print(
                    f"  [!] {service} stage={exc.stage} 發生 pre-submit failure，安全地重新連接一次: {exc}",
                    flush=True,
                )
                try:
                    scraper.close()
                except Exception:
                    pass
                self._scrapers.pop(service, None)
                scraper = self.get_or_create(service, headless)
                if rate_lease is not None:
                    scraper._rate_submit_lease = rate_lease
                return scraper.ask(prompt, **kwargs)
            except Exception as exc:
                _debug_log(f"MANAGER_UNKNOWN_ERROR service={service} exc={exc!r}")
                _debug_log(traceback.format_exc())
                raise
            finally:
                self._last_request_end[service] = time.monotonic()
                if rate_lease is not None:
                    if getattr(scraper, "_rate_submit_lease", None) is rate_lease:
                        scraper._rate_submit_lease = None
                    rate_lease.release()

    def cancel_current_generation(
        self,
        service: str,
        headless: bool = False,
        restart_on_failure: bool = True,
    ) -> dict:
        """Cancel the active WebGPT generation without closing it unless required."""
        scraper = self._scrapers.get(service)
        if scraper is None:
            return {
                "status": "not_running",
                "stopped": False,
                "restarted": False,
            }

        try:
            result = scraper.cancel_current_generation()
        except Exception as exc:
            result = {
                "status": "restart_required",
                "stopped": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        if result.get("status") != "restart_required" or not restart_on_failure:
            result.setdefault("restarted", False)
            return result

        # Last resort only: preserve SERVICE_CONFIG URL/profile, recreate the
        # Playwright persistent context, and do NOT resend the cancelled prompt.
        _debug_log(
            f"MANAGER_CANCEL_RESTART service={service} reason={result.get('error', '')[:400]}"
        )
        try:
            scraper.close()
        except Exception:
            pass
        self._scrapers.pop(service, None)

        try:
            self.get_or_create(service, headless)
            return {
                **result,
                "status": "restarted",
                "restarted": True,
            }
        except Exception as exc:
            return {
                **result,
                "status": "restart_failed",
                "restarted": False,
                "restart_error": f"{type(exc).__name__}: {exc}",
            }

    def edit_file(self, service: str, source_path: str, instruction: str, output_path: str = None, run_id: str = None) -> dict:
        lock = self._service_lock(service)
        with lock:
            self._respect_request_cooldown(service)
            rate_lease = None
            scraper = None
            try:
                scraper = self.get_or_create(service, False)
                if service == "chatgpt":
                    rate_lease = self._rate_governor.acquire(wait=True)
                    scraper._rate_submit_lease = rate_lease
                result = scraper.edit_file(source_path, instruction, output_path=output_path, run_id=run_id)
                if service == "chatgpt":
                    self._rate_governor.record_success()
                return result
            finally:
                self._last_request_end[service] = time.monotonic()
                if rate_lease is not None:
                    if scraper is not None and getattr(scraper, "_rate_submit_lease", None) is rate_lease:
                        scraper._rate_submit_lease = None
                    rate_lease.release()

    def download_latest_artifact(self, service: str, output_path: str, expected_filename: str = "", timeout_sec: float = 45.0) -> dict:
        """Generic manager entry point for WebGPT-produced files/images."""
        lock = self._service_lock(service)
        with lock:
            scraper = self.get_or_create(service, False)
            return scraper.download_latest_artifact(
                output_path, timeout_sec=timeout_sec, expected_filename=expected_filename
            )

    def close_all(self):
        for name, s in list(self._scrapers.items()):
            try:
                s.close()
            except:
                pass
        self._scrapers.clear()


# ─── Global manager instance (reused across agent calls) ─────────────────────
_manager: Optional[ScraperManager] = None

def get_manager() -> ScraperManager:
    global _manager
    if _manager is None:
        _manager = ScraperManager()
    return _manager


# ─── Standalone test mode ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except AttributeError:
            pass

    parser = argparse.ArgumentParser(description="Web LLM Scraper - 直接操作 ChatGPT/Gemini 網頁")
    parser.add_argument("--service", "-s", default="chatgpt",
                        choices=["chatgpt", "gemini"],
                        help="使用哪個服務 (default: chatgpt)")
    parser.add_argument("--prompt", "-p", type=str,
                        help="直接傳入提示（單次執行）")
    parser.add_argument("--headless", action="store_true",
                        help="無頭模式（不顯示瀏覽器）")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="互動模式（多輪對話）")
    args = parser.parse_args()

    scraper = WebLLMScraper(service=args.service, headless=args.headless)
    scraper.start(show_browser=True)

    try:
        if args.prompt:
            print(f"\n[回應]\n{scraper.ask(args.prompt)}")
        elif args.interactive:
            print(f"\n[{args.service} 互動模式] 輸入 /exit 退出\n")
            while True:
                try:
                    user_input = input("你> ").strip()
                    if user_input.lower() in ("/exit", "/quit", "exit"):
                        break
                    if not user_input:
                        continue
                    reply = scraper.ask(user_input)
                    print(f"\n[{args.service}]\n{reply}\n")
                except KeyboardInterrupt:
                    break
        else:
            # Quick test
            print("\n[測試] 發送測試訊息...")
            reply = scraper.ask("用一句話說你是什麼 AI")
            print(f"\n[回應] {reply}")
    finally:
        scraper.close()

