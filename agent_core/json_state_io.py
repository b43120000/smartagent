#!/usr/bin/env python3
"""Small Windows-safe JSON state I/O helpers for shared Agent processes."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def read_json_retry(path: str | Path, *, attempts: int = 8) -> Any:
    target = Path(path)
    last_error: OSError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (PermissionError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.02 * (attempt + 1))
    if last_error is not None:  # pragma: no cover - loop always returns/raises
        raise last_error
    raise RuntimeError("json_state_read_failed")


def write_json_atomic(path: str | Path, payload: Any, *, attempts: int = 8) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(
        f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for attempt in range(max(1, int(attempts))):
            try:
                os.replace(temp, target)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
