#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebAgent-owned ChatGPT prompt sender with shared rate-limit safety."""
from __future__ import annotations

import time
import threading
from pathlib import Path

from agent_core.webgpt_rate_governor import WebGPTRateGovernor, WebGPTRateLimited


class WebAgentBrowserClient:
    """Send WebAgent prompts without routing through the Agent manager loop."""

    def __init__(self, scraper, *, governor=None, sleep=time.sleep, clock=time.monotonic, event_sink=None):
        self.scraper = scraper
        self.governor = governor or getattr(scraper, "_rate_governor", None)
        if self.governor is None:
            self.governor = WebGPTRateGovernor(Path(__file__).resolve().parents[1])
            self.scraper._rate_governor = self.governor
        self._sleep = sleep
        self._clock = clock
        self.event_sink = event_sink

    def _emit(self, event: str, **fields) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **fields)

    def _acquire_submit_lease(self, stage: str):
        self._emit("submit_lease_wait_started", stage=stage)
        last_busy_notice = 0.0
        last_delay_notice = 0.0
        while True:
            try:
                lease = self.governor.acquire(wait=False)
                self._emit("submit_lease_acquired", stage=stage)
                return lease
            except WebGPTRateLimited as exc:
                now = self._clock()
                if not last_delay_notice or now - last_delay_notice >= 10.0:
                    print(
                        f"[WebAgent] {stage}尚未送出：ChatGPT 安全間隔剩餘約 "
                        f"{max(1, int(exc.remaining_sec))} 秒。",
                        flush=True,
                    )
                    last_delay_notice = now
                self._sleep(min(1.0, max(0.1, exc.remaining_sec)))
            except TimeoutError:
                now = self._clock()
                if not last_busy_notice or now - last_busy_notice >= 15.0:
                    print(
                        f"[WebAgent] {stage}尚未送出：另一個 ChatGPT 自動化請求正在使用共用送出鎖；"
                        "這不會啟動 LocalAgent/Agent1，WebAgent 會繼續等待。",
                        flush=True,
                    )
                    last_busy_notice = now
                self._sleep(0.5)

    def ask(
        self,
        prompt: str,
        *,
        stage: str,
        attachment_paths: list[str] | None = None,
        protocol_expected: dict | None = None,
    ) -> str:
        print(f"[WebAgent] 準備發送{stage}。", flush=True)
        self._emit("browser_send_started", stage=stage, attachment_paths=attachment_paths or [])
        lease = self._acquire_submit_lease(stage)
        self.scraper._rate_submit_lease = lease
        print(f"[WebAgent] 正在把{stage}寫入 ChatGPT 並按下 Send...", flush=True)
        heartbeat_stop = threading.Event()
        started_at = self._clock()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(15.0):
                elapsed = self._clock() - started_at
                print(f"[WebAgent] {stage}處理中，已等待 {elapsed:.0f} 秒...", flush=True)
                self._emit("browser_wait_heartbeat", stage=stage, elapsed_sec=round(elapsed, 1))

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            response = self.scraper.ask(
                str(prompt),
                new_conversation=False,
                attachment_paths=attachment_paths or None,
                protocol_expected=protocol_expected,
            )
            try:
                self.governor.record_success()
            except Exception as exc:
                print(
                    f"[WebAgent][WARN] 回應已完成，但限流狀態更新失敗："
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            if hasattr(self.scraper, "_rate_limit_dialog_streak"):
                self.scraper._rate_limit_dialog_streak = 0
            if hasattr(self.scraper, "_rate_limited_until"):
                self.scraper._rate_limited_until = 0.0
            print(f"[WebAgent] 已收到{stage}回應。", flush=True)
            self._emit("browser_response_completed", stage=stage, response=response)
            return str(response)
        except Exception as exc:
            self._emit("browser_response_failed", stage=stage, error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            heartbeat_stop.set()
            if getattr(self.scraper, "_rate_submit_lease", None) is lease:
                self.scraper._rate_submit_lease = None
            lease.release()
