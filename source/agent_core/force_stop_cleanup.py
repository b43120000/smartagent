"""Archive and abandon owned work only after explicit force-stop, never startup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .chunked_write import ChunkedWriteManager
from .json_state_io import write_json_atomic
from .paths import remote_binding_transactions_path, remote_events_path, remote_tasks_path, runtime_root
from .process_file_lock import exclusive_process_lock
from .remote_events import RemoteEventStore
from .task_state import TaskStateStore

INCOMPLETE = {"QUEUED", "RUNNING", "PAUSING", "PAUSED", "RESUMING"}
TRANSPORTS = {"TELEGRAM", "WEBGPT", "WEBGPT_COPILOT", "LOCAL_TEST"}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain(path: Path) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        junction = getattr(part, "is_junction", None)
        if part.is_symlink() or (callable(junction) and junction()):
            raise RuntimeError(f"force_stop_reparse_rejected:{part}")
    return path


def _read(path: Path) -> dict:
    _plain(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"force_stop_invalid_state:{path}")
    return value


def _archive(root: Path, files: set[Path]) -> tuple[Path, dict]:
    """Publish a verified archive before the first cleanup mutation."""
    base = _plain(runtime_root(root) / "force_stop_archives")
    base.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex}"
    pending = base / (name + ".pending")
    pending.mkdir()
    entries = []
    for index, source in enumerate(sorted(files, key=str)):
        _plain(source)
        expected = _hash(source)
        target = pending / f"{index:06d}.bin"
        with source.open("rb") as inp, target.open("xb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if _hash(target) != expected or _hash(source) != expected:
            raise RuntimeError("force_stop_archive_source_changed")
        entries.append({"source": str(source), "file": target.name,
                        "bytes": target.stat().st_size, "sha256": expected})
    payload = {"schema": "FORCE_STOP_ARCHIVE_V1", "complete": True,
               "created_at": time.time(), "files": entries}
    # Unlike write_json_atomic, the archive completion marker is fsync'ed.
    marker = pending / "index.json"
    with marker.open("xb") as out:
        out.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        out.flush()
        os.fsync(out.fileno())
    for entry in entries:
        if _hash(pending / entry["file"]) != entry["sha256"]:
            raise RuntimeError("force_stop_archive_verification_failed")
    final = base / name
    os.replace(pending, final)
    return final, payload


def _plan(root: Path, inventory: list[dict]) -> dict:
    task_path = _plain(remote_tasks_path(root))
    store = TaskStateStore(task_path)
    tasks = store.load()  # Read only; no empty-state corruption fallback.
    rows = _read(task_path).get("tasks", {}) if task_path.exists() else {}
    alive = {int(p.get("ProcessId", 0)) for p in inventory}
    owned = {}
    cancel = []
    reconcile = []
    files = {task_path} if task_path.exists() else set()
    requests = {}
    for task_id, row in rows.items():
        metadata = row.get("metadata") or {}
        transport = str(metadata.get("transport") or (row.get("reply_route") or {}).get("transport") or "").upper()
        if transport not in TRANSPORTS:
            if row.get("state") in INCOMPLETE:
                raise RuntimeError(f"force_stop_unowned_incomplete_task:{task_id}")
            continue
        declared_root = metadata.get("install_root")
        if declared_root and Path(declared_root).resolve() != root:
            if row.get("state") in INCOMPLETE:
                raise RuntimeError(f"force_stop_foreign_task:{task_id}")
            continue
        request = str(row.get("request_id") or "")
        if not request:
            if row.get("state") in INCOMPLETE:
                raise RuntimeError("force_stop_missing_request_identity")
            continue
        if request in requests:
            raise RuntimeError("force_stop_ambiguous_request_identity")
        requests[request] = task_id
        owned[task_id] = row
        if (row.get("state") == "CANCELLED" and
                isinstance(metadata.get("force_stop"), dict) and
                not metadata["force_stop"].get("cleanup_complete")):
            reconcile.append(task_id)
        if row.get("state") in INCOMPLETE:
            cancel.append(task_id)
            for key in ("worker_pid", "launcher_pid", "dispatcher_pid"):
                pid = int(metadata.get(key, 0) or 0)
                if pid and pid in alive:
                    raise RuntimeError(f"force_stop_live_or_reused_task_pid:{task_id}:{pid}")
    writers = []
    checked = set()
    known_scopes = set()
    workspace_roots = set()
    for task_id, task in owned.items():
        raw = str(task.get("workspace") or "")
        if raw and Path(raw).is_absolute() and task.get("task_epoch"):
            identity = json.dumps([task_id, task["task_epoch"], task["request_id"]],
                                  ensure_ascii=False, separators=(",", ":"))
            known_scopes.add(Path(raw).absolute() / ".agents" / "chunked_writes" / "tasks" /
                             hashlib.sha256(identity.encode()).hexdigest())
    for task_id, task in owned.items():
        workspace_raw = str(task.get("workspace") or "")
        if not workspace_raw:
            if task_id in cancel:
                raise RuntimeError("force_stop_missing_workspace")
            continue
        if not Path(workspace_raw).is_absolute():
            raise RuntimeError("force_stop_workspace_not_absolute")
        workspace = _plain(Path(workspace_raw))
        if not workspace.is_dir():
            if task_id not in cancel:
                continue  # Historical terminal task; no surviving writer tree.
            raise RuntimeError("force_stop_workspace_unavailable")
        legacy = _plain(workspace / ".agents" / "chunked_writes")
        workspace_roots.add(legacy)
        roots = [(legacy, False)]
        epoch = str(task.get("task_epoch") or "")
        if epoch:
            identity = json.dumps([task_id, epoch, task["request_id"]], ensure_ascii=False, separators=(",", ":"))
            scope = hashlib.sha256(identity.encode()).hexdigest()
            roots.append((_plain(legacy / "tasks" / scope), True))
        for state_root, scoped in roots:
            if state_root in checked or not state_root.exists():
                continue
            checked.add(state_root)
            if not state_root.is_dir():
                raise RuntimeError("force_stop_writer_root_not_directory")
            manifests = _plain(state_root / "manifests")
            for path in manifests.glob("*.json"):
                manifest = _read(path)
                state = manifest.get("state")
                retry_abort = (state == "ABORTED" and manifest.get("abort_reason") == "FORCE_STOP_ALL_AGENTS"
                               and Path(str(manifest.get("staging_path") or "")).is_file())
                if state in {"COMMITTED", "ABORTED", "FAILED", "EXPIRED"} and not retry_abort:
                    continue
                owner = requests.get(str(manifest.get("request_id") or ""))
                if not owner:
                    raise RuntimeError("force_stop_writer_ownership_unknown_or_foreign")
                if scoped and owner != task_id:
                    raise RuntimeError("force_stop_writer_scope_identity_mismatch")
                if state != "OPEN" and not retry_abort:
                    raise RuntimeError(f"force_stop_writer_state_requires_review:{state}")
                write_id = str(manifest.get("write_id") or "")
                key = hashlib.sha256(write_id.encode()).hexdigest()
                raw_stage = Path(str(manifest.get("staging_path") or ""))
                if not raw_stage.is_absolute():
                    raise RuntimeError("force_stop_staging_not_absolute")
                stage = _plain(raw_stage)
                expected_stage = _plain(state_root / "staging" / (key + ".part"))
                if not write_id or path.name != key + ".json" or stage != expected_stage or not stage.is_file():
                    raise RuntimeError("force_stop_noncanonical_writer_paths")
                lease = str(manifest.get("lease_owner") or "")
                if not lease.startswith("pid:"):
                    raise RuntimeError("force_stop_writer_lease_unknown")
                if int(lease[4:]) in alive:
                    raise RuntimeError("force_stop_writer_owner_pid_alive")
                # Archive only this proven writer's ledger, not the shared store.
                files.update((path, stage))
                for ledger in _plain(state_root / "ledger").glob("*.json"):
                    value = _read(ledger)
                    if value.get("write_id") == write_id:
                        files.add(ledger)
                writers.append({"manifest": path, "staging": stage,
                                "manifest_sha256": _hash(path), "staging_sha256": _hash(stage),
                                "request_id": manifest["request_id"]})
    # A missing Task record cannot make an orphan active transaction invisible.
    for legacy in workspace_roots:
        scopes = _plain(legacy / "tasks")
        for child in scopes.iterdir() if scopes.is_dir() else ():
            _plain(child)
            if child in known_scopes:
                continue
            if not child.is_dir() or not re.fullmatch(r"[a-f0-9]{64}", child.name):
                raise RuntimeError("force_stop_orphan_writer_scope_invalid")
            for path in _plain(child / "manifests").glob("*.json"):
                if _read(path).get("state") not in {"COMMITTED", "ABORTED", "FAILED", "EXPIRED"}:
                    raise RuntimeError("force_stop_orphan_active_writer_requires_review")
            known_parts = {p.stem for p in (child / "manifests").glob("*.json")}
            for stage in _plain(child / "staging").glob("*"):
                _plain(stage)
                if stage.suffix != ".part" or stage.stem not in known_parts:
                    raise RuntimeError("force_stop_orphan_staging_requires_review")
    for task_id in cancel + reconcile:
        plan_id = str((owned[task_id].get("metadata") or {}).get("source_plan_id") or "")
        if plan_id:
            if not re.fullmatch(r"PLAN-[A-F0-9]{20}", plan_id):
                raise RuntimeError("force_stop_invalid_source_plan_id")
            plan_path = _plain(task_path.parent / "plan_ledger" / (plan_id + ".json"))
            if not plan_path.is_file():
                raise RuntimeError("force_stop_source_plan_missing")
            record = _read(plan_path)
            if (record.get("schema") != "SMARTAGENT_EXECUTION_PLAN_V1" or
                    record.get("plan_id") != plan_id or
                    record.get("execution_task_id") != task_id or
                    record.get("execution_request_id") != owned[task_id]["request_id"] or
                    record.get("state") not in {"EXECUTING", "CANCELLED"}):
                raise RuntimeError("force_stop_source_plan_owner_or_state_mismatch")
            files.add(plan_path)
    transaction_path = _plain(remote_binding_transactions_path(root))
    if transaction_path.exists():
        tx = _read(transaction_path)
        if not isinstance(tx.get("transactions"), dict):
            raise ValueError("force_stop_invalid_binding_transactions")
        files.add(transaction_path)
    for path in (remote_events_path(root), runtime_root(root) / "task_continuations.json"):
        if path.exists():
            files.add(_plain(path))
    return {"tasks": cancel, "reconcile": reconcile, "writers": writers, "files": files,
            "task_path": task_path, "transaction_path": transaction_path}


def cleanup_stopped_work(root: Path, inventory: list[dict], *, inventory_provider=None, _barrier_held=False) -> dict:
    """Caller must provide a fresh inventory proving no owned Agent survives."""
    from .force_stop_all_agents import discover_agent_pids, _powershell_process_inventory
    inventory_provider = inventory_provider or _powershell_process_inventory
    root = _plain(root).resolve()
    if discover_agent_pids(inventory, root, exclude_pids={os.getpid()})["selected_pids"]:
        return {"success": False, "error": "force_stop_owned_process_still_alive"}
    archive = None
    aborted = []
    try:
        transaction_path = _plain(remote_binding_transactions_path(root))
        barrier = _plain(runtime_root(root) / "force_stop_cleanup.lock")
        transaction_lock = _plain(transaction_path.with_name(transaction_path.name + ".lock"))
        with (nullcontext() if _barrier_held else exclusive_process_lock(
                barrier, timeout_sec=5, label="force-stop/startup barrier")), \
             exclusive_process_lock(transaction_lock,
                                    timeout_sec=5, label="force stop binding transactions"):
            plan = _plan(root, inventory)
            fresh = inventory_provider()
            if discover_agent_pids(fresh, root, exclude_pids={os.getpid()})["selected_pids"]:
                raise RuntimeError("force_stop_new_agent_started_during_preflight")
            _plan(root, fresh)
            if not plan["files"]:
                return {"success": True, "tasks_cancelled": [], "writers_aborted": [], "transactions_invalidated": 0}
            archive, data = _archive(root, plan["files"])
            hashes = {entry["source"]: entry["sha256"] for entry in data["files"]}
            if any(_hash(path) != hashes[str(path)] for path in plan["files"]):
                raise RuntimeError("force_stop_state_changed_after_archive")
            fresh = inventory_provider()
            if discover_agent_pids(fresh, root, exclude_pids={os.getpid()})["selected_pids"]:
                raise RuntimeError("force_stop_new_agent_started_during_archive")
            _plan(root, fresh)  # Recheck leases/metadata PIDs under launch barrier.
            for writer in plan["writers"]:
                aborted.append(ChunkedWriteManager.abort_archived_open_session(
                    writer["manifest"], request_id=writer["request_id"],
                    manifest_sha256=writer["manifest_sha256"],
                    staging_sha256=writer["staging_sha256"], archive_path=str(archive),
                ))
            cancelled = TaskStateStore(plan["task_path"]).cancel_after_force_stop(
                plan["tasks"], expected_sha256=hashes[str(plan["task_path"])], archive_path=str(archive),
            ) if plan["tasks"] else []
            reconcile = cancelled + plan["reconcile"]
            store = TaskStateStore(plan["task_path"])
            store.load()
            from .plan_ledger import PlanLedger
            for task_id in reconcile:
                task = store.get(task_id)
                if task.metadata.get("source_plan_id"):
                    PlanLedger.from_task_store(plan["task_path"]).finish_execution_task(task, state="CANCELLED", detail="FORCE_STOP_ALL_AGENTS")
            paused_events = RemoteEventStore(remote_events_path(root)).pause_for_tasks(set(reconcile), reason="FORCE_STOP_ALL_AGENTS")
            count = 0
            if transaction_path.exists():
                transactions = _read(transaction_path)
                count = len(transactions["transactions"])
                transactions.update(transactions={}, invalidated_at=time.time(),
                                    invalidation_reason="FORCE_STOP_ALL_AGENTS", archive=str(archive))
                write_json_atomic(transaction_path, transactions)
            if reconcile:
                store.finish_force_stop(reconcile)
            return {"success": True, "archive": str(archive), "tasks_cancelled": cancelled,
                    "writers_aborted": aborted, "events_paused": paused_events,
                    "transactions_invalidated": count}
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}",
                "archive": str(archive) if archive else None, "writers_aborted": aborted}
