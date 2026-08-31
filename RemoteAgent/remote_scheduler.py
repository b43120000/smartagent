#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

import time
from dataclasses import dataclass
from typing import Callable

from agent_core.conversation_registry import ConversationRegistry

PROFILE_SECONDS = {"active": 15.0, "normal": 60.0, "inactive": 300.0}
BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0)
HEALTH_STATES = {"CONNECTED", "STALE", "RECONCILING", "UNAVAILABLE"}


@dataclass(frozen=True)
class DueConversation:
    workspace: str
    gpt_url: str
    poll_profile: str
    next_check_at: float


class RemoteWatchScheduler:
    def __init__(self, registry: ConversationRegistry | None = None, *, clock: Callable[[], float] = time.time):
        self.registry = registry or ConversationRegistry()
        self.clock = clock

    def due(self, now: float | None = None) -> list[DueConversation]:
        now = self.clock() if now is None else float(now)
        rows = []
        for rec in self.registry.list_remote_conversations(enabled_only=True):
            schedule = dict(rec.get("remote_schedule") or {})
            when = float(schedule.get("next_check_at", 0.0) or 0.0)
            if when <= now:
                rows.append(DueConversation(rec["workspace"], rec["gpt_url"], str(rec.get("poll_profile") or "normal"), when))
        return sorted(rows, key=lambda x: (x.next_check_at, x.gpt_url))

    def mark_started(self, item: DueConversation, now: float | None = None):
        now = self.clock() if now is None else float(now)
        return self.registry.update_remote_schedule(item.workspace, item.gpt_url, last_check_at=now, health="RECONCILING")

    def mark_success(self, item: DueConversation, now: float | None = None, *, health: str = "CONNECTED"):
        now = self.clock() if now is None else float(now)
        profile = item.poll_profile if item.poll_profile in PROFILE_SECONDS else "normal"
        return self.registry.update_remote_schedule(item.workspace, item.gpt_url, last_successful_sync=now, last_reconcile_at=now, failure_count=0, health=health, next_check_at=now + PROFILE_SECONDS[profile])

    def mark_failure(self, item: DueConversation, failure_count: int, now: float | None = None):
        now = self.clock() if now is None else float(now)
        count = max(1, int(failure_count))
        delay = BACKOFF_SECONDS[min(count - 1, len(BACKOFF_SECONDS) - 1)]
        return self.registry.update_remote_schedule(item.workspace, item.gpt_url, failure_count=count, health="UNAVAILABLE", next_check_at=now + delay)

