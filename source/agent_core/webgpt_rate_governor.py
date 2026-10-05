#!/usr/bin/env python3
from __future__ import annotations

"""Cross-process submit serialization and adaptive ChatGPT web cooldown."""

import json
import os
import time
import ctypes
import datetime
from dataclasses import dataclass
from pathlib import Path

from .runtime_cleanup import runtime_cancel_requested
from .paths import webgpt_rate_state_lock_path, webgpt_rate_state_path, webgpt_submit_lock_path


class WebGPTRateLimited(RuntimeError):
    def __init__(self, remaining_sec: float):
        self.remaining_sec = max(0.0, float(remaining_sec))
        super().__init__(f"CHATGPT_RATE_LIMITED: cooldown_remaining_sec={int(max(1, self.remaining_sec))}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows: signal
        # zero may be interpreted as a console control event. OpenProcess is a
        # read-only liveness check and never signals the target.
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        error = int(ctypes.windll.kernel32.GetLastError())
        return error == 5  # ACCESS_DENIED still means the process exists.
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class _ExclusiveFileLock:
    def __init__(self, path: Path, *, poll_sec: float = 0.2):
        self.path = Path(path)
        self.poll_sec = max(0.02, float(poll_sec))
        self.fd = None

    def _remove_stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0) or 0)
        except Exception:
            pid = 0
        try:
            age = max(0.0, time.time() - self.path.stat().st_mtime)
        except OSError:
            return True
        # Never steal a lock from a live request. Unreadable orphan locks get a
        # bounded grace period so a partial create cannot deadlock all agents.
        stale = (pid > 0 and not _pid_alive(pid)) or (pid <= 0 and age > 30.0)
        if not stale:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def acquire(
        self,
        *,
        wait: bool = True,
        timeout_sec: float | None = None,
        cancel_check=None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        while self.fd is None:
            if cancel_check is not None:
                cancel_check()
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = json.dumps({"pid": os.getpid(), "created_at": time.time()}).encode("utf-8")
                os.write(self.fd, payload)
                return
            except (FileExistsError, PermissionError):
                if self._remove_stale():
                    continue
                if not wait:
                    raise TimeoutError(f"webgpt lock busy: {self.path}")
                if timeout_sec is not None and time.monotonic() - started >= float(timeout_sec):
                    raise TimeoutError(f"webgpt lock timeout: {self.path}")
                time.sleep(self.poll_sec)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.close(self.fd)
        finally:
            self.fd = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


@dataclass
class WebGPTSubmitLease:
    governor: "WebGPTRateGovernor"
    lock: _ExclusiveFileLock
    acquired_at: float
    conversation_key: str = ""
    submit_count: int = 0
    released: bool = False

    def before_submit(self) -> float:
        """Wait at the real Send boundary while the global lease is held."""
        if self.released:
            raise RuntimeError("webgpt submit lease already released")
        self.governor._check_runtime_cancelled()
        self.governor._wait_until_allowed(wait=True, conversation_key=self.conversation_key)
        self.governor._check_runtime_cancelled()
        return self.governor._clock()

    def record_submit(self) -> float:
        """Persist the timestamp immediately after the Send click attempt."""
        if self.released:
            raise RuntimeError("webgpt submit lease already released")
        submitted_at = self.governor._clock()
        self.governor._record_request_start(submitted_at, conversation_key=self.conversation_key)
        self.acquired_at = submitted_at
        self.submit_count += 1
        return submitted_at

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.governor._record_request_end()
        except Exception as exc:
            print(f"  [WebGPT Governor] 無法更新 rate state: {type(exc).__name__}: {exc}", flush=True)
        finally:
            self.lock.release()


class WebGPTRateGovernor:
    def __init__(
        self,
        root: str | Path,
        *,
        min_interval_sec: float | None = None,
        first_cooldown_sec: float | None = None,
        repeated_cooldown_sec: float | None = None,
        repeat_window_sec: float | None = None,
        dismissals_before_cooldown: int | None = None,
        sleep=time.sleep,
        clock=time.time,
    ):
        self.root = Path(root).expanduser().resolve()
        self.state_path = webgpt_rate_state_path(self.root)
        self.submit_lock_path = webgpt_submit_lock_path(self.root)
        self.state_lock_path = webgpt_rate_state_lock_path(self.root)
        configured_interval = float(
            os.environ.get("SMARTAGENT_WEBGPT_MIN_INTERVAL_SEC", "11")
            if min_interval_sec is None else min_interval_sec
        )
        # Production configuration cannot undercut the strict >10 second rule.
        # Explicit constructor values remain available for fast deterministic tests.
        self.min_interval_sec = max(
            10.5 if min_interval_sec is None else 0.0,
            configured_interval,
        )
        self.first_cooldown_sec = max(0.01, float(
            os.environ.get("SMARTAGENT_WEBGPT_FIRST_COOLDOWN_SEC", "600")
            if first_cooldown_sec is None else first_cooldown_sec
        ))
        self.repeated_cooldown_sec = max(self.first_cooldown_sec, float(
            os.environ.get("SMARTAGENT_WEBGPT_REPEATED_COOLDOWN_SEC", "1800")
            if repeated_cooldown_sec is None else repeated_cooldown_sec
        ))
        self.repeat_window_sec = max(0.01, float(
            os.environ.get("SMARTAGENT_WEBGPT_REPEAT_WINDOW_SEC", "1800")
            if repeat_window_sec is None else repeat_window_sec
        ))
        self.dismissals_before_cooldown = max(1, int(
            os.environ.get("SMARTAGENT_WEBGPT_DISMISSALS_BEFORE_COOLDOWN", "5")
            if dismissals_before_cooldown is None else dismissals_before_cooldown
        ))
        self._sleep = sleep
        self._clock = clock

    def _check_runtime_cancelled(self) -> None:
        if runtime_cancel_requested(self.root):
            raise RuntimeError("runtime_shutdown_cancelled_before_submit")

    def _load(self) -> dict:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _iso(timestamp: float) -> str:
        return datetime.datetime.fromtimestamp(
            float(timestamp), tz=datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds")

    def _mutate(self, callback):
        lock = _ExclusiveFileLock(self.state_lock_path, poll_sec=0.05)
        lock.acquire(wait=True, timeout_sec=15.0)
        try:
            state = self._load()
            result = callback(state)
            self._save(state)
            return result
        finally:
            lock.release()

    def remaining_delay(self, *, now: float | None = None, conversation_key: str = "") -> float:
        now = self._clock() if now is None else float(now)
        state = self._load()
        # Legacy versions entered cooldown after one dismissed dialog. Only a
        # state explicitly marked as reaching the current threshold may block.
        threshold_reached = bool(state.get("cooldown_threshold_reached", False))
        recorded_threshold = int(state.get("rate_limit_threshold", 0) or 0)
        cooldown = (
            float(state.get("cooldown_until", 0.0) or 0.0) - now
            if threshold_reached and recorded_threshold == self.dismissals_before_cooldown
            else 0.0
        )
        # Spacing is submit-to-submit, not completion-to-next-submit. A response
        # that already took longer than the interval must not incur extra idle.
        spacing = float(state.get("last_submit_at", 0.0) or 0.0) + self.min_interval_sec - now
        key = str(conversation_key or "").strip().lower()
        scoped = 0.0
        if key:
            rows = dict(state.get("last_submit_by_conversation") or {})
            scoped = float(rows.get(key, 0.0) or 0.0) + self.min_interval_sec - now
        return max(0.0, cooldown, spacing, scoped)

    def _wait_until_allowed(self, *, wait: bool, conversation_key: str = "") -> None:
        announced_at = 0.0
        while True:
            self._check_runtime_cancelled()
            remaining = self.remaining_delay(conversation_key=conversation_key)
            if remaining <= 0:
                return
            if not wait:
                raise WebGPTRateLimited(remaining)
            now = self._clock()
            if not announced_at or now - announced_at >= 60.0:
                print(
                    f"  [WebGPT Governor] 等待全域送出間隔 {remaining:.1f} 秒；"
                    "不會按下新的 ChatGPT Send。",
                    flush=True,
                )
                announced_at = now
            self._sleep(min(1.0, remaining))

    def acquire(self, *, wait: bool = True, conversation_key: str = "") -> WebGPTSubmitLease:
        """Acquire cross-process submit ownership only.

        The decisive spacing/cooldown gate is before_submit(), immediately
        before the verified Send click, after composer/attachment preparation.
        """
        lock = _ExclusiveFileLock(self.submit_lock_path)
        lock.acquire(wait=wait, cancel_check=self._check_runtime_cancelled)
        self._check_runtime_cancelled()
        return WebGPTSubmitLease(self, lock, self._clock(), str(conversation_key or "").strip().lower())

    def _record_request_start(self, submitted_at: float, *, conversation_key: str = "") -> None:
        def update(state):
            key = str(conversation_key or "").strip().lower()
            if key:
                rows = dict(state.get("last_submit_by_conversation") or {})
                rows[key] = float(submitted_at)
                state["last_submit_by_conversation"] = rows
            state.update(
                version=2,
                last_submit_at=float(submitted_at),
                last_submit_at_iso=self._iso(submitted_at),
                last_submit_pid=os.getpid(),
                updated_at=float(submitted_at),
            )
        self._mutate(update)

    def _record_request_end(self) -> None:
        now = self._clock()
        def update(state):
            state.update(
                version=max(2, int(state.get("version", 0) or 0)),
                last_request_end=now,
                last_request_end_iso=self._iso(now),
                last_request_pid=os.getpid(),
                updated_at=now,
            )
        self._mutate(update)

    def record_rate_limit(self, detail: str = "") -> dict:
        now = self._clock()
        def update(state):
            previous = float(state.get("last_rate_limit_at", 0.0) or 0.0)
            consecutive = bool(previous and 0.0 <= now - previous <= self.repeat_window_sec)
            streak = (int(state.get("rate_limit_streak", 0) or 0) + 1) if consecutive else 1
            triggered = streak >= self.dismissals_before_cooldown
            previous_cooldown = float(state.get("last_cooldown_at", 0.0) or 0.0)
            repeated = bool(
                triggered and previous_cooldown
                and 0.0 <= now - previous_cooldown <= self.repeat_window_sec
            )
            cooldown = (
                self.repeated_cooldown_sec if repeated else self.first_cooldown_sec
            ) if triggered else 0.0
            cooldown_until = (
                max(float(state.get("cooldown_until", 0.0) or 0.0), now + cooldown)
                if triggered else 0.0
            )
            state.update(
                version=1,
                cooldown_until=cooldown_until,
                last_rate_limit_at=now,
                last_rate_limit_at_iso=self._iso(now),
                cooldown_until_iso=self._iso(cooldown_until) if cooldown_until else "",
                rate_limit_count=int(state.get("rate_limit_count", 0) or 0) + 1,
                rate_limit_streak=streak,
                rate_limit_threshold=self.dismissals_before_cooldown,
                cooldown_threshold_reached=triggered,
                last_rate_limit_detail=str(detail or "")[:500],
                updated_at=now,
            )
            if triggered:
                state.update(last_cooldown_at=now, last_cooldown_at_iso=self._iso(now))
            return {
                "triggered": triggered, "repeated": repeated,
                "streak": streak, "threshold": self.dismissals_before_cooldown,
                "cooldown_sec": cooldown, "cooldown_until": state["cooldown_until"],
            }
        return dict(self._mutate(update) or {})

    def record_success(self) -> None:
        """A completed WebGPT response breaks the consecutive-dialog streak."""
        now = self._clock()
        def update(state):
            state.update(
                version=1,
                rate_limit_streak=0,
                rate_limit_threshold=self.dismissals_before_cooldown,
                cooldown_threshold_reached=False,
                cooldown_until=0.0,
                cooldown_until_iso="",
                last_success_at=now,
                last_success_at_iso=self._iso(now),
                updated_at=now,
            )
        self._mutate(update)

    def reset_for_force_stop(self, *, reason: str = "FORCE_STOP_ALL_AGENTS") -> None:
        """Clear transient submit gates after an explicit force-stop.

        A force-stop is an operator reset boundary.  Do not leave a cooldown,
        submit spacing timestamp, or conversation-scoped spacing behind for a
        newly launched generation.
        """
        now = self._clock()

        def update(state):
            state.update(
                version=2,
                last_submit_at=0.0,
                last_submit_at_iso="",
                last_submit_by_conversation={},
                cooldown_until=0.0,
                cooldown_until_iso="",
                last_rate_limit_at=0.0,
                last_rate_limit_at_iso="",
                last_cooldown_at=0.0,
                last_cooldown_at_iso="",
                rate_limit_count=0,
                rate_limit_streak=0,
                rate_limit_threshold=self.dismissals_before_cooldown,
                cooldown_threshold_reached=False,
                last_rate_limit_detail="",
                force_stop_reset_at=now,
                force_stop_reset_at_iso=self._iso(now),
                force_stop_reset_reason=str(reason or "FORCE_STOP_ALL_AGENTS"),
                updated_at=now,
            )

        self._mutate(update)


__all__ = ["WebGPTRateGovernor", "WebGPTRateLimited", "WebGPTSubmitLease"]
