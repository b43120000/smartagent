#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .conversation_identity import conversation_id
from .process_file_lock import exclusive_process_lock
from .paths import remote_control_lock_path, remote_control_state_path

CONTROL_COMMANDS = {"snapshot webgpt", "refresh", "重新整理"}


class RemoteControlPlane:
    """Durable single-slot priority channel for RemoteAgent browser controls."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.state_path = remote_control_state_path(self.root)
        self.lock_path = remote_control_lock_path(self.root)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write(self, value: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(
            f"{self.state_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def submit(self, command: str, *, source_message_id: str = "") -> dict[str, Any]:
        command = str(command or "").strip().casefold()
        if command not in CONTROL_COMMANDS:
            raise ValueError(f"unsupported_remote_control:{command}")
        now = time.time()
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="remote control plane"
        ):
            current = self._load()
            status = str(current.get("status", "")).upper()
            updated = float(current.get("updated_at", now) or now)
            if status in {"PENDING", "CLAIMED"} and now - updated < 120.0:
                raise RuntimeError("remote_control_busy")
            row = {
                "version": 1,
                "request_id": f"CTRL-{uuid.uuid4().hex.upper()}",
                "command": command,
                "status": "PENDING",
                "source_message_id": str(source_message_id or ""),
                "created_at": now,
                "updated_at": now,
            }
            self._write(row)
            return dict(row)

    def claim(self, owner: str) -> dict[str, Any] | None:
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="remote control plane"
        ):
            row = self._load()
            if str(row.get("status", "")).upper() != "PENDING":
                return None
            now = time.time()
            row.update(
                status="CLAIMED",
                owner=str(owner or "unknown"),
                claimed_at=now,
                updated_at=now,
            )
            self._write(row)
            return dict(row)

    def complete(
        self,
        request_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="remote control plane"
        ):
            row = self._load()
            if str(row.get("request_id", "")) != str(request_id):
                raise RuntimeError("remote_control_request_mismatch")
            now = time.time()
            row.update(
                status="FAILED" if error else "DONE",
                result=dict(result or {}),
                error=str(error or ""),
                completed_at=now,
                updated_at=now,
            )
            self._write(row)
            return dict(row)

    def wait(self, request_id: str, *, timeout_sec: float = 75.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, float(timeout_sec))
        while time.monotonic() < deadline:
            with exclusive_process_lock(
                self.lock_path, timeout_sec=10.0, label="remote control plane"
            ):
                row = self._load()
            if (
                str(row.get("request_id", "")) == str(request_id)
                and str(row.get("status", "")).upper() in {"DONE", "FAILED"}
            ):
                return row
            time.sleep(0.1)
        # A timed-out durable request must not remain PENDING/CLAIMED forever;
        # otherwise it keeps the runtime in a false demand state indefinitely.
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="remote control plane"
        ):
            row = self._load()
            if (
                str(row.get("request_id", "")) == str(request_id)
                and str(row.get("status", "")).upper() in {"PENDING", "CLAIMED"}
            ):
                now = time.time()
                row.update(
                    status="FAILED",
                    error=f"remote_control_timeout:{request_id}",
                    completed_at=now,
                    updated_at=now,
                )
                self._write(row)
        raise TimeoutError(f"remote_control_timeout:{request_id}")

    def cancel_for_clean_start(self, *, reason: str = "REMOTE_CLEAN_START") -> bool:
        """Close a pending control command without deleting its audit row."""
        with exclusive_process_lock(
            self.lock_path, timeout_sec=10.0, label="remote control plane"
        ):
            row = self._load()
            if str(row.get("status", "")).upper() not in {"PENDING", "CLAIMED"}:
                return False
            now = time.time()
            row.update(
                status="CANCELLED",
                error=str(reason or "REMOTE_CLEAN_START"),
                completed_at=now,
                updated_at=now,
            )
            self._write(row)
            return True


def _capture_visible_chrome_window(page, target: Path) -> None:
    """Capture the visible Chrome frame, including tabs/address bar, without DWM shadow."""
    import ctypes
    from ctypes import wintypes
    from PIL import ImageGrab

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    GA_ROOT = 2
    DWMWA_EXTENDED_FRAME_BOUNDS = 9

    page.bring_to_front()
    time.sleep(0.15)

    def window_class(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, len(buf))
        return str(buf.value or "")

    hwnd = user32.GetForegroundWindow()
    if not hwnd or not window_class(hwnd).startswith("Chrome_WidgetWin"):
        # Fallback: use CDP only to locate the target Chrome window, then let
        # Win32/DWM provide the actual visible pixel bounds.
        session = page.context.new_cdp_session(page)
        try:
            target_info = session.send("Target.getTargetInfo")
            target_id = str(target_info.get("targetInfo", {}).get("targetId", "") or "")
            params = {"targetId": target_id} if target_id else {}
            window_info = session.send("Browser.getWindowForTarget", params)
        finally:
            session.detach()
        bounds = dict(window_info.get("bounds", {}) or {})
        try:
            x = int(bounds["left"]) + max(1, int(bounds["width"]) // 2)
            y = int(bounds["top"]) + max(1, int(bounds["height"]) // 2)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("remote_control_snapshot_window_bounds_missing") from exc

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        hwnd = user32.WindowFromPoint(POINT(x, y))
        hwnd = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else 0

    if not hwnd or not user32.IsWindowVisible(hwnd):
        raise RuntimeError("remote_control_snapshot_chrome_window_missing")
    if not window_class(hwnd).startswith("Chrome_WidgetWin"):
        raise RuntimeError("remote_control_snapshot_target_not_chrome")

    rect = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        raise RuntimeError(f"remote_control_snapshot_dwm_bounds_failed:{hr}")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise RuntimeError("remote_control_snapshot_visible_bounds_invalid")

    image = ImageGrab.grab(
        bbox=(rect.left, rect.top, rect.right, rect.bottom),
        all_screens=True,
    )
    image.save(target, format="PNG")

def execute_page_control(
    page,
    target_url: str,
    request: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    command = str(request.get("command", "") or "").strip().casefold()
    request_id = str(request.get("request_id", "") or "")
    target_id = conversation_id(target_url)
    current_id = conversation_id(str(getattr(page, "url", "") or ""))
    if not target_id or current_id != target_id:
        raise RuntimeError(
            f"remote_control_wrong_conversation:target={target_id},current={current_id}"
        )

    if command == "snapshot webgpt":
        # Capture the visible Chrome frame: Web content + address bar + tab strip.
        # DWM extended frame bounds exclude the invisible resize border/shadow that
        # caused the earlier blank-edge and cut-edge artifacts.
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        target = output / f"{request_id}.png"
        _capture_visible_chrome_window(page, target)
        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError("remote_control_snapshot_missing")
        return {
            "photo_path": str(target),
            "message": "WebGPT Chrome-window snapshot",
            "reloaded": False,
        }

    if command in {"refresh", "重新整理"}:
        original = str(getattr(page, "url", "") or target_url)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        current_id = conversation_id(str(getattr(page, "url", "") or ""))
        if current_id != target_id:
            page.goto(original or target_url, wait_until="domcontentloaded", timeout=60000)
        current_id = conversation_id(str(getattr(page, "url", "") or ""))
        if current_id != target_id:
            raise RuntimeError("remote_control_refresh_conversation_changed")
        return {
            "message": "WebGPT refreshed without resubmitting the original prompt.",
            "reloaded": True,
        }

    raise ValueError(f"unsupported_remote_control:{command}")


__all__ = ["CONTROL_COMMANDS", "RemoteControlPlane", "execute_page_control"]
