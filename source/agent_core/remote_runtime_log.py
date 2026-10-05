#!/usr/bin/env python3
"""Durable JSONL diagnostics shared by RemoteAgent runtime and its supervisor."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


class RemoteRuntimeLog:
    def __init__(self, path: Path, *, mirror_console: bool = False,
                 mirror_paths: list[Path] | None = None,
                 snapshot_path: Path | None = None):
        self.path = Path(path)
        self.mirror_console = bool(mirror_console)
        self.mirror_paths = [Path(item) for item in (mirror_paths or [])]
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

    def write(self, event: str, *, component: str, **detail: Any) -> None:
        detail = self._redact(detail)
        row = {
            "version": 1,
            "timestamp": time.time(),
            "event": str(event).upper(),
            "component": str(component),
            "pid": os.getpid(),
            **detail,
        }
        encoded = json.dumps(row, ensure_ascii=False, default=str)
        for target in [self.path, *self.mirror_paths]:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
            except Exception:
                # Diagnostics must never take down the supervised runtime.
                pass
        if self.snapshot_path is not None:
            try:
                self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.snapshot_path.with_name(
                    self.snapshot_path.name + f".{os.getpid()}.tmp"
                )
                temp.write_text(
                    json.dumps(row, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                temp.replace(self.snapshot_path)
            except Exception:
                pass
        if self.mirror_console:
            try:
                fields = " ".join(
                    f"{key}={value}" for key, value in detail.items()
                    if value not in (None, "", [], {})
                )
                stamp = time.strftime("%H:%M:%S", time.localtime(row["timestamp"]))
                print(f"[{stamp}] {row['event']} {row['component']} {fields}".rstrip(), flush=True)
            except Exception:
                pass

    @classmethod
    def _redact(cls, value: Any, *, key: str = "") -> Any:
        """Keep diagnostics useful without leaking transport credentials."""
        lowered = str(key).lower()
        if any(word in lowered for word in ("token", "secret", "password", "authorization")):
            return "***REDACTED***"
        if isinstance(value, dict):
            return {str(name): cls._redact(item, key=str(name)) for name, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            # A Telegram Bot API URL has no word boundary before its token
            # (`/bot123...`), so handle that form before generic bare tokens.
            value = re.sub(r"(/bot)\d{6,12}:[A-Za-z0-9_-]{20,}(?=/|$)", r"\1***REDACTED***", value)
            value = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "***REDACTED***", value)
            return re.sub(r"pair_[A-Za-z0-9_-]{16,}", "pair_***REDACTED***", value)
        return value
