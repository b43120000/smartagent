#!/usr/bin/env python3
from __future__ import annotations

"""Filesystem result sink for the local Telegram simulation sender."""

import json
import os
import time
from pathlib import Path

from agent_core.process_file_lock import exclusive_process_lock
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter


class LocalTestDeliveryAdapter:
    TRANSPORT = "LOCAL_TEST"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.allowed_root = (self.root / ".agents" / "remote_test_runs").resolve()

    def _outbox(self, route: dict) -> Path:
        raw = str((route or {}).get("outbox_path", "") or "").strip()
        if not raw:
            raise ValueError("local_test_outbox_missing")
        target = Path(raw).resolve()
        target.relative_to(self.allowed_root)
        return target

    def deliver_event(self, route: dict, event: dict) -> dict:
        if str((route or {}).get("transport", "")).upper() != self.TRANSPORT:
            return {"delivered": False, "reason": "local_test_wrong_transport"}
        try:
            outbox = self._outbox(route)
            outbox.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "written_at": time.time(),
                "pid": os.getpid(),
                "text": TelegramDeliveryAdapter.render(event),
                "event": dict(event),
            }
            lock = outbox.with_name(outbox.name + ".lock")
            with exclusive_process_lock(
                lock,
                timeout_sec=10.0,
                label="remote local-test outbox",
                legacy_kind="remote-local-test-outbox-v1",
            ):
                with outbox.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
            return {"delivered": True, "reply": str(outbox)}
        except Exception as exc:
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}

    def reconcile_event(self, route: dict, event: dict) -> dict:
        try:
            outbox = self._outbox(route)
            event_id = str(event.get("event_id", "") or "")
            if outbox.is_file() and event_id:
                for line in outbox.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if str((row.get("event") or {}).get("event_id", "")) == event_id:
                        return {"delivered": True, "reply": str(outbox)}
            return {"delivered": False, "definitive_not_delivered": True}
        except Exception as exc:
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}


__all__ = ["LocalTestDeliveryAdapter"]
