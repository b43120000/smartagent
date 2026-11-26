"""Git74: process-exit supervised waiting, independent of the fixed P7 timeout.

The OS-owned Popen handle establishes child identity. Waiting is automatic,
without user consent or a deadline. This NEVER grants command authorization.
Child exit and postconditions remain separate. No PID-only success or replay.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock
from .process_liveness import observe_owned_process

SCHEMA = "SMARTAGENT_GIT74_LONG_EXECUTION_V1"
CONSENT_SECONDS = 600.0
WAIT_POLICY = "UNTIL_EXIT"
_ID = re.compile(r"^WAIT-[0-9A-F]{20}$")
_STATES = frozenset({"RUNNING", "COMPLETED", "UNVERIFIED"})
_DECISIONS = frozenset({"PENDING", "CONTINUE", "AUTO_CONTINUE", "PAUSE_AFTER_EXIT", "COMPLETED_BEFORE_DECISION"})


def root_dir(install: str | Path | None = None) -> Path:
    base = Path(install).resolve() if install is not None else Path(__file__).resolve().parents[2]
    return base / "localdata" / "runtime" / "long_execution"


def _path(root: Path, wait_id: str) -> Path:
    if not _ID.fullmatch(str(wait_id)):
        raise ValueError("long_execution_wait_id_invalid")
    return root / (wait_id + ".json")


def _event(root: Path, row: dict[str, Any], event: str, **fields) -> None:
    payload = {
        "schema": SCHEMA, "event": event,
        "task_id": row["task_id"], "request_id": row["request_id"],
        "action_id": row["action_id"], "wait_id": row["wait_id"],
        "execution_id": row["execution_id"],
        "at": time.time(), **fields,
    }
    with (root / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()


def _modify(root: Path, wait_id: str, update):
    path = _path(root, wait_id)
    with exclusive_process_lock(root / (wait_id + ".lock"),
                                label="long-execution-consent"):
        record = read_json_retry(path)
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError("long_execution_record_invalid")
        updated = update(dict(record))
        if updated is None:
            return record
        if updated != record:
            write_json_atomic(path, updated)
        return updated


def begin_wait(*, install: str | Path | None, task_id: str, request_id: str,
               action_id: str, execution_id: str, pid: int) -> dict:
    root = root_dir(install)
    root.mkdir(parents=True, exist_ok=True)
    wait_id = "WAIT-" + uuid.uuid4().hex[:20].upper()
    now = time.time()
    record = {
        "schema": SCHEMA, "wait_id": wait_id, "task_id": str(task_id),
        "request_id": str(request_id), "action_id": str(action_id),
        "execution_id": str(execution_id), "pid": int(pid),
        "worker_pid": os.getpid(), "state": "RUNNING",
        "decision": "AUTO_CONTINUE", "notification": "NOT_REQUIRED",
        "chat_id": 0, "created_at": now, "deadline_at": None,
        "decided_at": now, "wait_confirmation_required": False,
        "last_os_observed_at": now, "last_os_state": "UNKNOWN",
        "last_os_source": "UNAVAILABLE", "liveness_sample_count": 0,
        "exit_code": None, "finished_at": None, "policy": WAIT_POLICY,
        "timeout_enabled": False, "original_command_replay": False,
    }
    write_json_atomic(_path(root, wait_id), record)
    _event(root, record, "LONG_EXEC_STARTED", pid=pid, policy=WAIT_POLICY)
    return record


def read_wait(install: str | Path | None, wait_id: str) -> dict:
    try:
        row = read_json_retry(_path(root_dir(install), wait_id))
        return row if row.get("schema") == SCHEMA else {}
    except (ValueError, OSError, TypeError, KeyError):
        return {}


def pending_notifications(install: str | Path | None) -> list[dict]:
    # Kept for old transport consumers. New and historical wait records
    # must never generate another confirmation request.
    return []


def bind_notice(install: str | Path | None, wait_id: str, *, chat_id: int) -> bool:
    return False


def finish_notice(install: str | Path | None, wait_id: str, *, success: bool) -> None:
    return None


def set_decision(install: str | Path | None, wait_id: str, *,
                 task_id: str, chat_id: int, choice: str) -> dict:
    # Stale Y/N buttons cannot pause a running task or authorize execution.
    row = read_wait(install, wait_id)
    if (not row or row.get("task_id") != task_id
            or row.get("chat_id") != int(chat_id) or int(chat_id) <= 0):
        raise ValueError("long_execution_binding_mismatch")
    return row


def record_observation(install: str | Path | None, wait_id: str, observation: dict) -> None:
    root = root_dir(install)
    def apply(row):
        if row["state"] != "RUNNING":
            return None
        if str(observation.get("execution_id")) != row["execution_id"] or observation.get("pid") != row["pid"]:
            raise ValueError("long_execution_os_identity_mismatch")
        row["last_os_observed_at"] = float(observation["observed_at"])
        row["last_os_state"] = observation["process_state"]
        row["last_os_source"] = observation["evidence_source"]
        row["liveness_sample_count"] += 1
        # Normalize legacy in-flight records as well.
        if row.get("decision") != "AUTO_CONTINUE" or row.get("notification") != "NOT_REQUIRED":
            row.update(decision="AUTO_CONTINUE", notification="NOT_REQUIRED",
                       deadline_at=None, decided_at=time.time(),
                       wait_confirmation_required=False)
        _event(root, row, "LONG_EXEC_OS_SAMPLE",
               process_state=observation["process_state"],
               evidence_source=observation["evidence_source"],
               liveness_verified=observation.get("liveness_verified") is True,
               sample_index=row["liveness_sample_count"])
        return row
    _modify(root, wait_id, apply)


def finish_wait(install: str | Path | None, wait_id: str,
                observation: dict, exit_code: int | None) -> dict:
    root = root_dir(install)
    def apply(row):
        if row["state"] != "RUNNING":
            return None
        same_identity = (
            observation.get("execution_id") == row["execution_id"]
            and observation.get("pid") == row["pid"]
            and observation.get("process_state") == "EXITED"
            and observation.get("process_identity_verified") is True
        )
        row["state"] = "COMPLETED" if same_identity else "UNVERIFIED"
        row["finished_at"] = time.time()
        row["exit_code"] = exit_code if same_identity else None
        row["last_os_state"] = observation.get("process_state", "UNKNOWN")
        row["last_os_source"] = observation.get("evidence_source", "UNAVAILABLE")
        row.update(decision="AUTO_CONTINUE", notification="NOT_REQUIRED",
                   deadline_at=None, wait_confirmation_required=False)
        _event(root, row, "LONG_EXEC_EXIT_OBSERVED", state=row["state"],
               decision=row["decision"], exit_code=row["exit_code"],
               evidence_source=row["last_os_source"],
               process_identity_verified=same_identity)
        return row
    return _modify(root, wait_id, apply)


def wait_until_exit(process: subprocess.Popen, *, install: str | Path | None,
                    telemetry: dict, execution_id: str,
                    on_running=None, interval: float = 0.5) -> tuple[dict, dict]:
    """No completion deadline; wait for this exact Popen handle, never a stale PID.

    Only on Windows/POSIX parent-owned handles. A detached daemon or API job
    requires its own completion adapter and is NOT proven by launcher exit.
    """
    record = begin_wait(
        install=install, task_id=str(telemetry.get("task_id") or ""),
        request_id=str(telemetry.get("request_id") or ""),
        action_id=str(telemetry.get("action_id") or ""),
        execution_id=execution_id, pid=process.pid)
    next_sample = 0.0
    try:
        while True:
            proof = observe_owned_process(process, execution_id=execution_id)
            now = time.monotonic()
            if proof["process_state"] == "EXITED":
                end = finish_wait(install, record["wait_id"], proof, process.returncode)
                return proof, end
            # A lost/failed OS probe must never spin indefinitely after an
            # independently observed Popen exit. Exit remains UNVERIFIED.
            if process.poll() is not None:
                end = finish_wait(install, record["wait_id"], proof, None)
                return proof, end
            if now >= next_sample:
                record_observation(install, record["wait_id"], proof)
                if proof.get("liveness_verified") and on_running is not None:
                    on_running(proof)
                next_sample = now + 2.0
            try:
                process.wait(timeout=max(0.1, min(float(interval), 2.0)))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # No fabricated terminal record on controller failure. The caller's
        # existing exception handler owns safe child termination.
        _event(root_dir(install), record, "LONG_EXEC_SUPERVISOR_INTERRUPTED",
               state="UNKNOWN")
        raise
