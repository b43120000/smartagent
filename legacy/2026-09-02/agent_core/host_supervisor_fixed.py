#!/usr/bin/env python3
"""External lifecycle owner for Agent 1, persistent Agent 0, and Meta Recovery."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .windows_power_guard import WindowsPowerGuard

SELF_REPAIR_STALLED = "SELF_REPAIR_STALLED"
NEEDS_HUMAN = "NEEDS_HUMAN"
META_ACTIVE = "META_RECOVERY_ACTIVE"
META_RESTARTING = "META_RECOVERY_RESTARTING"
META_RESUMING = "META_RECOVERY_RESUMING"
META_COMPLETED = "META_RECOVERY_COMPLETED"


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class HostSupervisor:
    def __init__(self, *, root: Path = AGENT_PROJECT_ROOT, python: str = sys.executable, popen=subprocess.Popen):
        self.root = Path(root)
        self.python = python
        self._popen = popen
        self.token = uuid.uuid4().hex
        self.agent1 = None
        self.agent0 = None
        self.generation = 0
        base = self.root / ".agents"
        repair = base / "self_repair"
        self.host_state = base / "agent_host_state.json"
        self.status_state = base / "status_metadata.json"
        self.active_repair = repair / "active_repair.json"
        self.restart_request = repair / "restart_request.json"
        self.restart_result = repair / "restart_result.json"
        self.meta_state = repair / "meta_recovery.json"
        self.issue_store = repair / "issues.jsonl"
        self.checkpoint_dir = repair / "checkpoints"
        self.heartbeat_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_HEARTBEAT_TIMEOUT_SEC", "30"))
        self.stage_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_STAGE_STALL_SEC", "90"))
        self.meta_budget = max(1, int(os.environ.get("SMARTAGENT_META_REPAIR_BUDGET", "2")))
        self.restart_budget = max(1, int(os.environ.get("SMARTAGENT_META_RESTART_BUDGET", "3")))
        self.power_guard = WindowsPowerGuard()

    def _spawn(self, cmd: list[str], **kwargs):
        return self._popen(cmd, cwd=str(self.root), **kwargs)

    def start_agent1(self, extra_args: list[str] | None = None):
        env = os.environ.copy()
        env["SMARTAGENT_EXTERNAL_SUPERVISOR"] = "1"
        env["SMARTAGENT_SUPERVISOR_TOKEN"] = self.token
        env["SMARTAGENT_SELF_REPAIR_ROOT"] = str(self.root / ".agents" / "self_repair")
        env["SMARTAGENT_PROJECT_ROOT"] = str(self.root)
        try:
            self.host_state.unlink(missing_ok=True)
        except OSError:
            pass
        self.generation += 1
        self.agent1 = self._spawn([self.python, str(self.root / "smart_agent.py"), *(extra_args or [])], env=env)
        return self.agent1

    def start_agent0(self, state: dict):
        script = self.root / "RemoteAgent" / "hidden_supervisor.py"
        cmd = [self.python, str(script), "--cdp", str(state["cdp_endpoint"]), "--parent-pid", str(os.getpid()), "--poll", "10.0"]
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        self.agent0 = self._spawn(cmd, stdin=subprocess.DEVNULL, creationflags=flags)
        return self.agent0

    def stop_agent1(self, timeout: float = 8) -> None:
        proc = self.agent1
        if not proc or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def restart_agent1(self):
        self.stop_agent1()
        return self.start_agent1()

    @staticmethod
    def failure_fingerprint(reason: str, detail: str = "", stage: str = "") -> str:
        payload = json.dumps({"reason": reason, "detail": detail, "stage": stage}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    def load_meta_recovery(self) -> dict:
        return _load(self.meta_state)

    def _repair_context_active(self, active: dict | None = None) -> bool:
        active = dict(active or _load(self.active_repair))
        state = str(active.get("state", "") or "").upper()
        if not active:
            return False
        if state in {"", "COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN"}:
            return False
        return bool(active.get("issue_id") or active.get("repair_id") or active.get("run_id") or state)

    def _checkpoint_metadata(self) -> dict:
        active = _load(self.active_repair)
        run_id = str(active.get("run_id", "") or "")
        user = _load(self.checkpoint_dir / f"{run_id}.json") if run_id else {}
        return {
            "self_repair_checkpoint": active,
            "user_task_checkpoint": user,
            "resume_order": ["self_repair", "user_task"],
        }

    def _watchdog_snapshot(self) -> dict:
        host = _load(self.host_state)
        status = _load(self.status_state)
        repair = _load(self.active_repair)
        return {"host": host, "status": status, "repair": repair}

    def _latest_issue_event(self, *, run_id: str = "") -> dict:
        try:
            lines=self.issue_store.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        fallback={}
        for line in reversed(lines[-100:]):
            try:
                event=json.loads(line)
            except Exception:
                continue
            if not isinstance(event,dict):
                continue
            if not fallback:
                fallback=event
            if run_id and str(event.get("run_id") or "")==run_id:
                return event
        return {} if run_id else fallback

    def collect_diagnostic_evidence(self, *, reason: str = "", stage: str = "", detail: str = "") -> dict:
        snap=self._watchdog_snapshot()
        status=dict(snap.get("status") or {})
        repair=dict(snap.get("repair") or {})
        host=dict(snap.get("host") or {})
        run_id=str(repair.get("run_id") or status.get("run_id") or "")
        issue=self._latest_issue_event(run_id=run_id)
        return {
            "reason": str(reason or ""),
            "stage": str(stage or ""),
            "detail": str(detail or "")[:4000],
            "generation": self.generation,
            "agent1_exit_code": self.agent1.poll() if self.agent1 else None,
            "host": {k:host.get(k) for k in ("status","cdp_endpoint","startup_recovery","startup_meta") if k in host},
            "status": {k:status.get(k) for k in ("stage","state","actor","error_code","message","detail","task_phase","observed_state","heartbeat_at") if k in status},
            "repair": {k:repair.get(k) for k in ("state","issue_id","repair_id","run_id","failure_type","failure_message","failure_stage","updated_at") if k in repair},
            "issue": {k:issue.get(k) for k in ("issue_id","fingerprint","classification","exception_type","message","traceback","stage","tool","action_id","error_code","source_file","source_line","source_symbol","detail","status") if k in issue},
        }


    def classify_watchdog(
        self,
        *,
        now: float,
        agent1_alive: bool,
        ready: bool,
        heartbeat_at: float | None = None,
        stage: str = "",
        stage_changed_at: float | None = None,
        coordinator_alive: bool = True,
        repair_active: bool = True,
    ) -> dict:
        if not agent1_alive:
            reason = "agent1_exited_before_ready" if not ready else "repair_coordinator_interrupted"
            return {"stalled": True, "reason": reason, "stage": stage}
        if not repair_active:
            return {"stalled": False, "reason": "", "stage": stage}
        if not coordinator_alive:
            return {"stalled": True, "reason": "repair_coordinator_interrupted", "stage": stage}
        if heartbeat_at is not None and now - float(heartbeat_at) > self.heartbeat_timeout:
            return {"stalled": True, "reason": "heartbeat_timeout", "stage": stage}
        if stage_changed_at is not None and now - float(stage_changed_at) > self.stage_timeout:
            return {"stalled": True, "reason": "stage_stagnation", "stage": stage}
        return {"stalled": False, "reason": "", "stage": stage}

    def record_self_repair_stall(self, *, reason: str, detail: str = "", stage: str = "", now: float | None = None, evidence: dict | None = None) -> dict:
        now = float(time.time() if now is None else now)
        evidence = dict(evidence or self.collect_diagnostic_evidence(reason=reason, stage=stage, detail=detail))
        fp = self.failure_fingerprint(reason, detail, stage)
        cur = self.load_meta_recovery()
        active_states = {SELF_REPAIR_STALLED, META_ACTIVE, META_RESTARTING, META_RESUMING}
        if cur.get("state") in active_states:
            if cur.get("fingerprint") == fp:
                cur["duplicate_events"] = int(cur.get("duplicate_events", 0)) + 1
                cur["updated_at"] = now
                _atomic(self.meta_state, cur)
                return cur
            cur["blocked_secondary_fingerprint"] = fp
            cur["blocked_secondary_reason"] = reason
            cur["updated_at"] = now
            _atomic(self.meta_state, cur)
            return cur

        attempts = int(cur.get("meta_repair_attempts", 0))
        restarts = int(cur.get("restart_attempts", 0))
        if attempts >= self.meta_budget:
            out = {
                "version": 1,
                "state": NEEDS_HUMAN,
                "reason": "meta_repair_budget_exhausted",
                "failed_reason": reason,
                "fingerprint": fp,
                "meta_repair_attempts": attempts,
                "restart_attempts": restarts,
                "generation": self.generation,
                "diagnostic_evidence": evidence,
                "updated_at": now,
                **self._checkpoint_metadata(),
            }
            _atomic(self.meta_state, out)
            return out

        out = {
            "version": 1,
            "state": SELF_REPAIR_STALLED,
            "meta_issue_required": True,
            "repair_dispatch_required": True,
            "fingerprint": fp,
            "failed_reason": reason,
            "detail": detail,
            "stage": stage,
            "diagnostic_evidence": evidence,
            "generation": self.generation,
            "meta_repair_attempts": attempts,
            "restart_attempts": restarts,
            "duplicate_events": 0,
            "created_at": now,
            "updated_at": now,
            **self._checkpoint_metadata(),
        }
        _atomic(self.meta_state, out)
        return out
    def mark_meta_repair_dispatched(self, issue_id: str) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") == NEEDS_HUMAN:
            return data
        if data.get("state") not in {SELF_REPAIR_STALLED, META_ACTIVE}:
            data.update(state=NEEDS_HUMAN, reason="invalid_meta_dispatch_state", updated_at=time.time())
        elif data.get("state") == SELF_REPAIR_STALLED:
            data.update(
                state=META_ACTIVE,
                issue_id=str(issue_id),
                meta_issue_required=False,
                repair_dispatch_required=False,
                meta_repair_attempts=int(data.get("meta_repair_attempts", 0)) + 1,
                updated_at=time.time(),
            )
        _atomic(self.meta_state, data)
        return data

    def mark_meta_repair_succeeded(self) -> dict:
        data = self.load_meta_recovery()
        restarts = int(data.get("restart_attempts", 0))
        if restarts >= self.restart_budget:
            data.update(state=NEEDS_HUMAN, reason="restart_budget_exhausted", updated_at=time.time())
        elif data.get("state") != META_ACTIVE:
            data.update(state=NEEDS_HUMAN, reason="invalid_meta_success_state", updated_at=time.time())
        else:
            data.update(
                state=META_RESTARTING,
                restart_attempts=restarts + 1,
                clean_restart_required=True,
                resume_order=["self_repair", "user_task"],
                updated_at=time.time(),
            )
        _atomic(self.meta_state, data)
        return data

    def mark_restart_complete(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") not in {META_RESTARTING, META_RESUMING}:
            return data
        data.update(
            state=META_RESUMING,
            clean_restart_required=False,
            next_resume="self_repair",
            self_repair_resumed=False,
            user_task_resumed=False,
            resume_order=["self_repair", "user_task"],
            updated_at=time.time(),
        )
        _atomic(self.meta_state, data)
        return data

    def mark_self_repair_resumed(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") != META_RESUMING or data.get("next_resume") != "self_repair":
            return data
        data.update(self_repair_resumed=True, next_resume="user_task", updated_at=time.time())
        _atomic(self.meta_state, data)
        return data

    def mark_user_task_resumed(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") != META_RESUMING:
            return data
        if data.get("next_resume") != "user_task" or not data.get("self_repair_resumed"):
            data.update(state=NEEDS_HUMAN, reason="resume_order_violation", updated_at=time.time())
        else:
            data.update(state=META_COMPLETED, user_task_resumed=True, next_resume="", updated_at=time.time())
        _atomic(self.meta_state, data)
        return data

    def wait_agent1_ready(self, timeout: float | None = None) -> dict:
        timeout = float(timeout or os.environ.get("SMARTAGENT_READINESS_TIMEOUT_SEC", "180"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.agent1 and self.agent1.poll() is not None:
                meta = self.record_self_repair_stall(
                    reason="agent1_exited_before_ready",
                    detail=f"generation={self.generation}",
                    stage="AGENT1_BOOTSTRAP",
                )
                return {"status": SELF_REPAIR_STALLED, "meta_recovery": meta, "supervisor_token": self.token}
            state = _load(self.host_state)
            if state.get("supervisor_token") == self.token and state.get("cdp_endpoint") and state.get("status") in {"idle", "busy", "ready"}:
                return state
            time.sleep(0.2)
        meta = self.record_self_repair_stall(
            reason="agent1_readiness_timeout",
            detail=f"generation={self.generation}",
            stage="AGENT1_BOOTSTRAP",
        )
        return {"status": SELF_REPAIR_STALLED, "meta_recovery": meta, "supervisor_token": self.token}

    def evaluate_runtime_watchdog(self, *, now: float | None = None) -> dict:
        now = float(time.time() if now is None else now)
        snap = self._watchdog_snapshot()
        status = snap["status"]
        repair = snap["repair"]
        alive = bool(self.agent1 and self.agent1.poll() is None)
        ready = bool(snap["host"].get("status") in {"idle", "busy", "ready"})
        repair_active = self._repair_context_active(repair)

        # A completed task is a hard terminal condition for the runtime watchdog.
        # This prevents stale TASK_RESUMING repair state from causing false
        # heartbeat/stage timeout meta-recovery after Agent 1 already completed.
        status_stage = str(status.get("stage", "") or "").upper()
        status_state = str(status.get("state", "") or "").upper()
        if status_stage == "COMPLETED" or status_state == "COMPLETED":
            repair_active = False

        stage = str(status.get("stage", "") or repair.get("state", "") or "RUNTIME")
        heartbeat_at = status.get("heartbeat_at") if repair_active else None
        stage_changed_at = repair.get("updated_at") if repair_active else None
        return self.classify_watchdog(
            now=now,
            agent1_alive=alive,
            ready=ready,
            heartbeat_at=heartbeat_at,
            stage=stage,
            stage_changed_at=stage_changed_at,
            coordinator_alive=alive,
            repair_active=repair_active,
        )
    def _handle_restart_request(self) -> bool:
        if not self.restart_request.exists():
            return False
        req = _load(self.restart_request)
        if req.get("supervisor_token") != self.token:
            _atomic(self.restart_result, {"state": "STALE_REQUEST_REJECTED", "generation": self.generation, "at": time.time()})
            self.restart_request.unlink(missing_ok=True)
            return True
        if req.get("state") != "REQUESTED":
            return False

        self.restart_agent1()
        state = self.wait_agent1_ready()
        if state.get("status") == SELF_REPAIR_STALLED:
            _atomic(self.restart_result, {
                "state": SELF_REPAIR_STALLED,
                "generation": self.generation,
                "meta_recovery": state.get("meta_recovery", {}),
                "at": time.time(),
            })
            return True

        _atomic(self.restart_result, {"state": "RESTARTED", "generation": self.generation, "at": time.time()})
        self.restart_request.unlink(missing_ok=True)
        meta = self.load_meta_recovery()
        if meta.get("state") == META_RESTARTING:
            self.mark_restart_complete()
            from .self_repair_coordinator import SelfRepairCoordinator
            SelfRepairCoordinator(self.root / ".agents" / "self_repair", project_root=self.root).sync_meta_resume_state()
        return True

    def _dispatch_meta_if_required(self) -> dict:
        meta = self.load_meta_recovery()
        if not meta.get("repair_dispatch_required"):
            return {"status": "NOT_REQUIRED"}
        try:
            from .meta_recovery_dispatcher import MetaRecoveryDispatcher
            result = MetaRecoveryDispatcher(root=self.root).dispatch_if_required(meta)
        except Exception as exc:
            result = {"status": "DISPATCH_FAILED", "error": f"{type(exc).__name__}: {exc}"}
        if result.get("status") == "PLAN_READY":
            issue_id=str(result.get("issue_id", "") or "")
            self.mark_meta_repair_dispatched(issue_id)
            try:
                from .meta_recovery_executor import execute_meta_plan
                execution=execute_meta_plan(result.get("plan_path", ""), self.root)
            except Exception as exc:
                execution={"status":"EXECUTOR_ERROR","error":f"{type(exc).__name__}: {exc}"}
            result["execution"]=execution
            if execution.get("status") in {"APPLIED","NO_CHANGES"}:
                progressed=self.mark_meta_repair_succeeded()
                if progressed.get("state")==META_RESTARTING:
                    from .self_repair_coordinator import SelfRepairCoordinator
                    coordinator=SelfRepairCoordinator(self.root / ".agents" / "self_repair", project_root=self.root)
                    user_checkpoint=dict(progressed.get("user_task_checkpoint") or {})
                    self_checkpoint=dict(progressed.get("self_repair_checkpoint") or {})
                    run_id=str(user_checkpoint.get("run_id") or self_checkpoint.get("run_id") or "")
                    modified=list(result.get("modified_paths") or [])
                    reproducer=[self.python,"-m","py_compile",*modified] if modified else [self.python,"-c","import agent_core.host_supervisor, agent_core.self_repair_coordinator"]
                    coordinator.request_restart(issue_id=issue_id,repair_id="META-"+issue_id,run_id=run_id,candidate_revision=str(execution.get("candidate_revision","") or ""),reproducer_argv=reproducer,recovery_kind="meta_recovery",supervisor_token=self.token)
            else:
                cur=self.load_meta_recovery()
                cur.update(state=NEEDS_HUMAN,reason="meta_executor_failed",last_execution_result=execution,updated_at=time.time())
                _atomic(self.meta_state,cur)
        else:
            cur = self.load_meta_recovery()
            cur["last_dispatch_result"] = result
            cur["updated_at"] = time.time()
            _atomic(self.meta_state, cur)
        return result

    def run(self) -> int:
        self.power_guard.acquire()
        self.start_agent1()
        state = self.wait_agent1_ready()

        # Bootstrap failure is already persisted as SELF_REPAIR_STALLED. Keep
        # Host alive so an external/meta repair controller can act instead of
        # collapsing the supervisor process immediately.
        if state.get("status") != SELF_REPAIR_STALLED:
            self.start_agent0(state)

        runtime_exit_seen_at = 0.0
        try:
            while True:
                if self._handle_restart_request():
                    runtime_exit_seen_at = 0.0
                    time.sleep(0.25)
                    continue

                meta = self.load_meta_recovery()
                if meta.get("state") == NEEDS_HUMAN:
                    return 2
                if meta.get("repair_dispatch_required"):
                    self._dispatch_meta_if_required()
                    meta = self.load_meta_recovery()
                    if meta.get("state") == NEEDS_HUMAN:
                        return 2

                watchdog = self.evaluate_runtime_watchdog()
                if watchdog.get("stalled"):
                    reason = str(watchdog.get("reason", "repair_coordinator_interrupted"))
                    stage = str(watchdog.get("stage", "RUNTIME"))
                    code = self.agent1.poll() if self.agent1 else None
                    detail = f"generation={self.generation};agent1_exit_code={code}"
                    stalled = self.record_self_repair_stall(reason=reason, detail=detail, stage=stage)
                    if stalled.get("state") == NEEDS_HUMAN:
                        return 2

                # Agent1 may die while Meta Recovery is pending. Do not return;
                # Host/Agent0 owns the outer lifecycle and must stay available
                # for repair dispatch and clean restart.
                if self.agent1 and self.agent1.poll() is not None:
                    if not runtime_exit_seen_at:
                        runtime_exit_seen_at = time.time()
                else:
                    runtime_exit_seen_at = 0.0

                time.sleep(0.25)
        finally:
            self.power_guard.release()
            self.stop_agent1()
            if self.agent0 and self.agent0.poll() is None:
                self.agent0.terminate()


def main():
    return HostSupervisor().run()


if __name__ == "__main__":
    raise SystemExit(main())
