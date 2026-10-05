#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable, controller-owned JSONL diagnostics for WebAgent Direct."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class WebAgentRuntimeLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _bounded(value):
        if isinstance(value, str):
            return value if len(value) <= 12000 else value[:12000] + "...[truncated]"
        if isinstance(value, dict):
            return {str(key): WebAgentRuntimeLog._bounded(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [WebAgentRuntimeLog._bounded(item) for item in value]
        return value

    def emit(self, event: str, **fields) -> None:
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": str(event),
            "pid": os.getpid(),
            **self._bounded(fields),
        }
        line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
