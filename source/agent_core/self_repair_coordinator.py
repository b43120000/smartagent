#!/usr/bin/env python3
"""Durable restart/reproducer/resume coordinator for guarded self repair."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from .workspace import AGENT_PROJECT_ROOT
from .task_checkpoint import TaskCheckpointStore
from .paths import self_repair_root

ROOT = self_repair_root()
ACTIVE = ROOT / "active_repair.json"
EVENTS = ROOT / "repair_events.jsonl"
RESTART = ROOT / "restart_request.json"
META = ROOT / "meta_recovery.json"

RESTART_REQUESTED = "RESTART_REQUESTED"
REPRODUCER_RETRY = "REPRODUCER_RETRY"
SELF_REPAIR_RESUMING = "SELF_REPAIR_RESUMING"
SELF_REPAIR_RESUMED = "SELF_REPAIR_RESUMED"
TASK_RESUMING = "TASK_RESUMING"
NEEDS_HUMAN = "NEEDS_HUMAN"
META_RESUMING = "META_RECOVERY_RESUMING"


class SelfRepairCoordinator:
    def __init__(self, root: str | Path = ROOT, checkpoint_store: TaskCheckpointStore | None = None, project_root: str | Path = AGENT_PROJECT_ROOT):
        self.root = Path(root)
        self.project_root = Path(project_root)
        self.active = self.root / "active_repair.json"
        self.events = self.root / "repair_events.jsonl"
        self.restart = self.root / "restart_request.json"
        self.meta = self.root / "meta_recovery.json"
        self.checkpoints = checkpoint_store or TaskCheckpointStore(self.root / "checkpoints")

    @staticmethod
    def _atomic(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _save(self, data: dict) -> dict:
        self._atomic(self.active, data)
        return dict(data)

    def load(self) -> dict:
        try:
            value = json.loads(self.active.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def load_meta(self) -> dict:
        try:
            value = json.loads(self.meta.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_meta(self, data: dict) -> dict:
        self._atomic(self.meta, data)
        return dict(data)

    def _append_event(self, old: str, new: str, data: dict, **fields) -> None:
        event = {
            "event_id": "REPAIR-EVT-" + uuid.uuid4().hex[:10].upper(),
            "repair_id": data.get("repair_id", ""),
            "issue_id": data.get("issue_id", ""),
            "run_id": data.get("run_id", ""),
            "from": old,
            "to": new,
            "at": time.time(),
            **fields,
        }
        self.events.parent.mkdir(parents=True, exist_ok=True)
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def transition(self, state: str, **fields) -> dict:
        data = self.load()
        old = str(data.get("state", "") or "")
        data.update(fields, state=state, updated_at=time.time())
        self._save(data)
        self._append_event(old, state, data)
        return data

    def snapshot_recovery_context(self, run_id: str) -> dict:
        current = self.load()
        task = self.checkpoints.load(run_id) if run_id else {}
        return {
            "self_repair_checkpoint": current,
            "user_task_checkpoint": task,
            "resume_order": ["self_repair", "user_task"],
        }

    def request_restart(
        self,
        *,
        issue_id: str,
        repair_id: str,
        run_id: str,
        candidate_revision: str,
        reproducer_argv: list[str],
        recovery_kind: str = "self_repair",
        supervisor_token: str = "",
    ) -> dict:
        prior = self.load()
        attempt = max(1, int(prior.get("repair_attempt", 0) or 0) + 1)
        context = self.snapshot_recovery_context(run_id)
        data = {
            "issue_id": issue_id,
            "repair_id": repair_id,
            "run_id": run_id,
            "candidate_revision": candidate_revision,
            "reproducer_argv": list(reproducer_argv or []),
            "repair_attempt": attempt,
            "recovery_kind": str(recovery_kind or "self_repair"),
            "state": RESTART_REQUESTED,
            "resume_order": ["self_repair", "user_task"],
            "original_self_repair_checkpoint": context["self_repair_checkpoint"],
            "original_user_task_checkpoint": context["user_task_checkpoint"],
            "updated_at": time.time(),
        }
        old = str(prior.get("state", "") or "")
        self._save(data)
        self._append_event(old, RESTART_REQUESTED, data)
        token = str(supervisor_token or os.environ.get("SMARTAGENT_SUPERVISOR_TOKEN", ""))
        request = {
            "state": "REQUESTED",
            "supervisor_token": token,
            "repair_id": repair_id,
            "candidate_revision": candidate_revision,
            "recovery_kind": data["recovery_kind"],
            "at": time.time(),
        }
        self._atomic(self.restart, request)
        return data
    def _run_reproducer(self, argv: list[str]) -> tuple[bool, object | None]:
        try:
            result = subprocess.run(
                argv,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            ) if argv else None
            return bool(result and result.returncode == 0), result
        except Exception:
            return False, None

    def mark_self_repair_resuming(self) -> dict:
        data = self.load()
        if data.get("state") not in {REPRODUCER_RETRY, RESTART_REQUESTED, SELF_REPAIR_RESUMING}:
            return data
        return self.transition(
            SELF_REPAIR_RESUMING,
            next_resume="self_repair",
            resume_order=["self_repair", "user_task"],
        )

    def mark_self_repair_resumed(self) -> dict:
        data = self.load()
        if data.get("state") not in {SELF_REPAIR_RESUMING, SELF_REPAIR_RESUMED}:
            return data
        return self.transition(
            SELF_REPAIR_RESUMED,
            self_repair_resumed=True,
            next_resume="user_task",
            resume_order=["self_repair", "user_task"],
        )

    def resume_user_task(self) -> dict:
        data = self.load()
        if data.get("state") != SELF_REPAIR_RESUMED or not data.get("self_repair_resumed"):
            return self.transition(NEEDS_HUMAN, reason="resume_order_violation")
        run_id = str(data.get("run_id", "") or "")
        checkpoint = self.checkpoints.load(run_id) if run_id else {}
        expected_task = bool(run_id or data.get("original_user_task_checkpoint"))
        if expected_task and not checkpoint:
            return self.transition(NEEDS_HUMAN, reason="user_task_checkpoint_missing")
        if checkpoint:
            checkpoint["task_state"] = "RUNNING"
            checkpoint["pause_reason"] = ""
            checkpoint["updated_at"] = time.time()
            self.checkpoints._save(checkpoint)
        return self.transition(
            TASK_RESUMING,
            user_task_resumed=True,
            next_resume="",
            reproducer_returncode=0,
        )

    def sync_meta_resume_state(self) -> dict:
        meta = self.load_meta()
        if meta.get("state") != META_RESUMING:
            return meta
        current = self.load()
        if current.get("state") in {SELF_REPAIR_RESUMING, SELF_REPAIR_RESUMED, TASK_RESUMING}:
            if current.get("state") == SELF_REPAIR_RESUMING:
                meta.update(next_resume="self_repair", self_repair_resumed=False)
            elif current.get("state") == SELF_REPAIR_RESUMED:
                meta.update(next_resume="user_task", self_repair_resumed=True)
            else:
                meta.update(
                    state="META_RECOVERY_COMPLETED",
                    next_resume="",
                    self_repair_resumed=True,
                    user_task_resumed=True,
                )
                # TASK_RESUMING is only a transient resume state. Once meta
                # recovery is durably complete, close active_repair as well so
                # HostSupervisor cannot mistake stale resume state for an active
                # repair on later heartbeat/stage watchdog checks.
                self.transition("COMPLETED")
            meta["updated_at"] = time.time()
            self._save_meta(meta)
        return meta

    def recover_after_restart(self, rollback: Callable[[], None] | None = None) -> dict:
        data = self.load()
        if data.get("state") != RESTART_REQUESTED:
            return {"state": "NO_RECOVERY"}

        if int(data.get("repair_attempt", 0) or 0) > 1:
            return self.transition(NEEDS_HUMAN, reason="repair_budget_exhausted")

        self.transition(REPRODUCER_RETRY)
        passed, result = self._run_reproducer(list(data.get("reproducer_argv", []) or []))
        if not passed:
            if rollback:
                try:
                    rollback()
                except Exception:
                    pass
            return self.transition(
                NEEDS_HUMAN,
                reason="reproducer_failed",
                reproducer_returncode=getattr(result, "returncode", None),
            )

        # The reproducer proving the repaired LocalAgent works is the boundary
        # where interrupted self-repair may resume. The user task remains paused
        # until that self-repair lane is explicitly marked resumed.
        self.mark_self_repair_resuming()
        self.mark_self_repair_resumed()
        resumed = self.resume_user_task()
        self.sync_meta_resume_state()
        return resumed

    def can_resume_user_task(self) -> bool:
        data = self.load()
        return bool(data.get("state") == SELF_REPAIR_RESUMED and data.get("self_repair_resumed"))

    def recovery_resume_order(self) -> list[str]:
        data = self.load()
        value = data.get("resume_order", ["self_repair", "user_task"])
        return list(value) if isinstance(value, list) else ["self_repair", "user_task"]
