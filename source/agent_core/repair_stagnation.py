"""Request-scoped protocol repair stall clock and sanitized live status.

A valid assistant turn is not necessarily *effective* progress. Only a
strictly advanced accepted step or new Runtime-verifiable Action evidence
can reset the soft stall clock. Cumulative rejection / GAR hard budgets
are independent and never reset here.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Mapping

from .json_state_io import read_json_retry, write_json_atomic
from .paths import runtime_root

SCHEMA = "SMARTAGENT_REPAIR_STAGNATION_V1"
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")


def _code(value: object) -> str:
    text = str(value or "").strip()
    return text if CODE.fullmatch(text) else "REDACTED_REASON"


def observation_path(root: str | Path, task_id: str) -> Path:
    if not TASK_ID.fullmatch(str(task_id)):
        raise ValueError("repair_stall_task_id_invalid")
    return runtime_root(root) / "repair_stagnation" / (task_id + ".json")


def read_observation(root: str | Path, task_id: str,
                     request_id: str | None = None) -> dict | None:
    path = observation_path(root, task_id)
    if not path.is_file():
        return None
    try:
        doc = read_json_retry(path)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    if doc.get("task_id") != task_id:
        return None
    if request_id is not None and doc.get("request_id") != request_id:
        return None
    return doc


def format_status(doc: Mapping[str, object] | None,
                  *, now: float | None = None) -> str:
    """Safe, concise status; never print prompt, paths or raw diagnostics."""
    if not doc:
        return "\n\nRepair：尚無可用診斷。"
    stamp = time.time() if now is None else float(now)
    stall_since = doc.get("repair_stall_since")
    elapsed = max(0, int(stamp - float(stall_since))) if isinstance(
        stall_since, (float, int)
    ) else 0
    def clock(value: object) -> str:
        if not isinstance(value, (float, int)) or value <= 0:
            return "NONE"
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
    return (
        "\n\nRepair 診斷（Runtime）"
        f"\nRejected Turns: {int(doc.get('rejected_turns_total', 0))}"
        f"\n停滯區間拒絕次數: {int(doc.get('rejected_turns_since_progress', 0))}"
        f"\nRepair 停滯時間: {elapsed} / 600 秒"
        f"\nGAR Level: {_code(doc.get('gar_level', 'L0'))}"
        f"\n最後拒絕原因: {_code(doc.get('last_rejection_reason', 'NONE'))}"
        f"\n最後接受 Progress: {clock(doc.get('last_accepted_progress_at'))}"
        f"\n最後有效進度: {clock(doc.get('last_effective_progress_at'))}"
        f"\n最後有效 Evidence: {clock(doc.get('last_evidence_at'))}"
        f"\n最後 Rejection: {clock(doc.get('last_rejection_at'))}"
    )


class RepairStagnation:
    """One process, one task/request; timestamps persisted for remote status."""

    def __init__(self, *, root: str | Path, task_id: str, request_id: str,
                 now: float | None = None):
        self.path = observation_path(root, task_id)
        self.task_id = task_id
        self.request_id = str(request_id)
        self.started_at = time.time() if now is None else float(now)
        self.last_accepted_progress_at: float | None = None
        self.last_effective_progress_at: float | None = None
        self.last_evidence_at: float | None = None
        self.last_rejection_at: float | None = None
        self.last_rejection_reason = "NONE"
        self.repair_stall_since: float | None = None
        self._stall_monotonic: float | None = None
        self.rejected_turns_since_progress = 0
        self.rejected_turns_total = 0
        self.latest_step: float | None = None
        self.gar_level = "L0"
        self._seen_action_ids: set[str] = set()
        self.effective_generation = 0
        self._rejection_seen = False
        self._persist()

    def _persist(self) -> None:
        write_json_atomic(self.path, self.snapshot())

    def snapshot(self) -> dict:
        return {
            "schema":SCHEMA, "task_id":self.task_id,
            "request_id":self.request_id, "request_started_at":self.started_at,
            "last_accepted_progress_at":self.last_accepted_progress_at,
            "last_effective_progress_at":self.last_effective_progress_at,
            "last_evidence_at":self.last_evidence_at,
            "last_rejection_at":self.last_rejection_at,
            "last_rejection_reason":self.last_rejection_reason,
            "repair_stall_since":self.repair_stall_since,
            "rejected_turns_since_progress":self.rejected_turns_since_progress,
            "rejected_turns_total":self.rejected_turns_total,
            "latest_step":self.latest_step,"gar_level":self.gar_level,
            "effective_generation":self.effective_generation,
        }

    def _effective(self, now: float) -> None:
        self.effective_generation += 1
        self.last_effective_progress_at = now
        self.repair_stall_since = None
        self._stall_monotonic = None
        self.rejected_turns_since_progress = 0
        self._rejection_seen = False

    def accepted_progress(self, step: object, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        self.last_accepted_progress_at = stamp
        effective = False
        if isinstance(step, (int, float)) and not isinstance(step, bool):
            value = float(step)
            if self.latest_step is None:
                # The baseline (often step 0 or 1) is not by itself advancement.
                self.latest_step = value
            elif value > self.latest_step:
                self.latest_step = value
                self._effective(stamp)
                effective = True
        self._persist()
        return effective

    def accepted_evidence(self, *, action_id: object, executed: object,
                          verification_status: object,
                          now: float | None = None) -> bool:
        # A new action ID is not evidence; a committed, executed,
        # independently classifiable Runtime result is.
        aid = str(action_id or "")
        status = str(verification_status or "").upper()
        if not aid or aid in self._seen_action_ids:
            return False
        if executed is not True or status not in {"PASS","FAIL"}:
            return False
        self._seen_action_ids.add(aid)
        stamp = time.time() if now is None else float(now)
        self.last_evidence_at = stamp
        self._effective(stamp)
        self._persist()
        return True

    def rejected(self, reasons: object, *, total: int,
                 now: float | None = None) -> None:
        stamp = time.time() if now is None else float(now)
        if not self._rejection_seen:
            self.repair_stall_since = stamp
            self._stall_monotonic = time.monotonic() if now is None else stamp
            self._rejection_seen = True
        self.rejected_turns_since_progress += 1
        # This is informational only. The canonical BoundedRepairBudget
        # counters and GAR hard caps never reset with progress.
        self.rejected_turns_total = int(total)
        codes = [_code(row.get("reason")) for row in reasons
                 if isinstance(row, Mapping)] if isinstance(reasons, list) else []
        self.last_rejection_reason = codes[0] if codes else "PROTOCOL_REJECTED"
        self.last_rejection_at = stamp
        self._persist()

    def level(self, value: object) -> None:
        new = str(value or "").upper()
        if new in {"L0","L1","L2","L3","L4","L5"}:
            self.gar_level = new
            self._persist()

    def stalled(self, *, now: float | None = None, min_rejections: int = 3,
                seconds: float = 600) -> bool:
        stamp = time.monotonic() if now is None else float(now)
        return (
            self._stall_monotonic is not None
            and self.rejected_turns_since_progress >= min_rejections
            and stamp - self._stall_monotonic >= seconds
        )

    def idle_seconds(self, *, now: float | None = None) -> float:
        stamp = time.monotonic() if now is None else float(now)
        return max(0.0, stamp - self._stall_monotonic) if self._stall_monotonic is not None else 0.0
