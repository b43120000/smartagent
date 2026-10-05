"""Durable request ownership for reusing one ChatGPT conversation safely."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .conversation_identity import conversation_id
from .process_file_lock import _pid_alive, exclusive_process_lock
from .paths import request_ownership_lock_path, request_ownership_state_path, source_root

REQUEST_CONTINUATION_LOST = "REQUEST_CONTINUATION_LOST"
REQUEST_OWNERSHIP_VIOLATION = "REQUEST_OWNERSHIP_VIOLATION"
REQUEST_CONTEXT_MISMATCH = "REQUEST_CONTEXT_MISMATCH"
STALE_TASK_EPOCH = "STALE_TASK_EPOCH"
STAGE_PROGRESS_TIMEOUT = "STAGE_PROGRESS_TIMEOUT"
WATCHDOG = 1800.0
REQUEST_SCOPE_FIELDS = (
    "request_id", "task_id", "task_epoch", "intent_digest",
    "request_phase", "continuation_seq",
)


class RequestOwnershipError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def digest_intent(request: str) -> str:
    normalized = " ".join(str(request or "").split())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def stage_for(prompt: str) -> str:
    value = str(prompt or "").upper()
    if "PROJECT_SYNC_READY" in value:
        return "POST_SYNC_MUTATION_PENDING"
    if "ACTION_REPLAN" in value or "ACK_REJECTED" in value or "PROTOCOL_REJECTED" in value:
        return "ACK_RECOVERY_PENDING"
    if "VERIFICATION_GATE" in value:
        return "VERIFY_PENDING"
    if "TOOL_RESULTS" in value or "RESULT_ID=" in value:
        return "TOOL_RESULT_PENDING"
    return "PLANNER_ROUND_PENDING"


class RequestOwnershipRegistry:
    def __init__(self, root=None, clock=time.time, alive=_pid_alive):
        self.root = Path(root).resolve() if root else source_root()
        self.state = request_ownership_state_path(self.root)
        self.lock = request_ownership_lock_path(self.root)
        self.clock = clock
        self.alive = alive

    def load(self) -> dict:
        if not self.state.exists():
            return {"schema": "REQUEST_OWNERSHIP_V2", "active": {}, "stale": []}
        try:
            state = json.loads(self.state.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RequestOwnershipError(REQUEST_CONTINUATION_LOST, type(exc).__name__) from exc
        if state.get("schema") not in {"REQUEST_OWNERSHIP_V1", "REQUEST_OWNERSHIP_V2"}:
            raise RequestOwnershipError(REQUEST_CONTINUATION_LOST, "bad_state")
        if not isinstance(state.get("active"), dict):
            raise RequestOwnershipError(REQUEST_CONTINUATION_LOST, "bad_active_state")
        state["schema"] = "REQUEST_OWNERSHIP_V2"
        state.setdefault("stale", [])
        return state

    def save(self, state: dict) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state)

    def ctx(self):
        return exclusive_process_lock(self.lock, timeout_sec=10, label="request ownership")

    def _new_active(self, cid, rid, owner, interface, pid, watchdog, *,
                    task_id="", task_epoch="", intent_digest="") -> dict:
        now = float(self.clock())
        return {
            "cid": cid, "rid": rid, "request_id": rid,
            "task_id": str(task_id or ""),
            "task_epoch": str(task_epoch or uuid.uuid4().hex),
            "intent_digest": str(intent_digest or ""),
            "owner": owner, "interface": interface, "pid": pid,
            "generation": 0, "continuation_seq": 0,
            "token": uuid.uuid4().hex, "stage": "REQUEST_ACTIVE",
            "deadline": now + watchdog,
        }

    def acquire(self, cid, rid, owner, interface, pid=None, watchdog=WATCHDOG, *,
                task_id="", task_epoch="", intent_digest=""):
        now = float(self.clock())
        pid = int(pid or os.getpid())
        with self.ctx():
            state = self.load()
            active = dict(state["active"].get(cid) or {})
            if active:
                same_request = active.get("rid") == rid
                migrated = False
                defaults = {
                    "request_id": active.get("rid", rid),
                    "task_id": str(task_id or "") if same_request else "",
                    "task_epoch": (
                        str(task_epoch or uuid.uuid4().hex)
                        if same_request else uuid.uuid4().hex
                    ),
                    "intent_digest": str(intent_digest or "") if same_request else "",
                    "continuation_seq": int(active.get("generation", 0)),
                }
                for field, value in defaults.items():
                    if field not in active:
                        active[field] = value
                        migrated = True
                if migrated:
                    state["active"][cid] = active
                    self.save(state)
                if active.get("rid") != rid:
                    if self.alive(int(active.get("pid", 0))):
                        if now > float(active.get("deadline", now + 1)):
                            raise RequestOwnershipError(STAGE_PROGRESS_TIMEOUT, active.get("stage", ""))
                        return {"status": "QUEUED", "active": active}
                    state["stale"].append({**active, "closed_at": now, "reason": "owner_process_exited"})
                    state["stale"] = state["stale"][-50:]
                    active = self._new_active(
                        cid, rid, owner, interface, pid, watchdog,
                        task_id=task_id, task_epoch=task_epoch, intent_digest=intent_digest,
                    )
                    state["active"][cid] = active
                    self.save(state)
                    return {"status": "RECOVERED_AFTER_STALE_OWNER", "active": active}
                if now > float(active.get("deadline", now + 1)):
                    raise RequestOwnershipError(STAGE_PROGRESS_TIMEOUT, active.get("stage", ""))
                if task_epoch and active.get("task_epoch") and active["task_epoch"] != task_epoch:
                    raise RequestOwnershipError(STALE_TASK_EPOCH, rid)
                if intent_digest and active.get("intent_digest") and active["intent_digest"] != intent_digest:
                    raise RequestOwnershipError(REQUEST_CONTEXT_MISMATCH, "intent_digest")
                if active.get("owner") != owner:
                    if self.alive(int(active.get("pid", 0))):
                        raise RequestOwnershipError(REQUEST_OWNERSHIP_VIOLATION, rid)
                    active.update(
                        owner=owner, pid=pid, interface=interface,
                        generation=int(active.get("generation", 0)) + 1,
                        token=uuid.uuid4().hex, deadline=now + watchdog,
                    )
                    state["active"][cid] = active
                    self.save(state)
                    return {"status": "RESUMED", "active": active}
                return {"status": "ACQUIRED", "active": active}
            active = self._new_active(
                cid, rid, owner, interface, pid, watchdog,
                task_id=task_id, task_epoch=task_epoch, intent_digest=intent_digest,
            )
            state["active"][cid] = active
            self.save(state)
            return {"status": "ACQUIRED", "active": active}

    def wait(self, cid, rid, owner, interface, timeout=120, **scope):
        end = time.monotonic() + timeout
        while True:
            result = self.acquire(cid, rid, owner, interface, **scope)
            if result["status"] != "QUEUED":
                return result["active"]
            if time.monotonic() >= end:
                raise RequestOwnershipError(STAGE_PROGRESS_TIMEOUT, rid)
            time.sleep(0.25)

    def advance(self, cid, rid, owner, token, stage):
        with self.ctx():
            state = self.load()
            active = dict(state["active"].get(cid) or {})
            if not active or active.get("rid") != rid or active.get("token") != token:
                raise RequestOwnershipError(REQUEST_CONTINUATION_LOST, rid)
            if active.get("owner") != owner:
                raise RequestOwnershipError(REQUEST_OWNERSHIP_VIOLATION, rid)
            active.update(
                generation=int(active.get("generation", 0)) + 1,
                continuation_seq=int(active.get("continuation_seq", 0)) + 1,
                token=uuid.uuid4().hex, stage=stage,
                deadline=float(self.clock()) + WATCHDOG,
            )
            state["active"][cid] = active
            self.save(state)
            return active

    def release(self, rid):
        # Planners used by unit tests or non-browser adapters never acquire a
        # conversation lease.  Their normal completion is therefore a no-op
        # and must not create or lock the production state directory.
        if not self.state.exists():
            return False
        snapshot = self.load()
        if not any(active.get("rid") == rid for active in snapshot["active"].values()):
            return False
        with self.ctx():
            state = self.load()
            for cid, active in list(state["active"].items()):
                if active.get("rid") == rid:
                    if int(active.get("pid", 0)) != os.getpid():
                        raise RequestOwnershipError(REQUEST_OWNERSHIP_VIOLATION, rid)
                    state["active"].pop(cid)
                    self.save(state)
                    return True
            return False

    def discard_for_restart(self, *, interface: str = "remote", request_ids: set[str] | None = None) -> int:
        """Move active leases out of the execution path during a hard reset."""
        wanted = {str(value or "") for value in (request_ids or set()) if str(value or "")}
        now = float(self.clock())
        changed = 0
        with self.ctx():
            state = self.load()
            for cid, active in list(state["active"].items()):
                if str(active.get("interface", "") or "") != str(interface):
                    continue
                rid = str(active.get("rid", active.get("request_id", "")) or "")
                if wanted and rid not in wanted:
                    continue
                state["active"].pop(cid, None)
                state["stale"].append({
                    **active,
                    "closed_at": now,
                    "reason": "remote_restart_discarded",
                })
                changed += 1
            if changed:
                state["stale"] = state["stale"][-50:]
                self.save(state)
        return changed


def guard_web_prompt(scraper, prompt, expected):
    """Acquire a lease and expose runtime-owned scope as read-only context."""
    rid = str((expected or {}).get("run_id", "") or "")
    if not rid:
        return str(prompt)
    page_url = str(getattr(getattr(scraper, "_page", None), "url", "") or "")
    cid = conversation_id(page_url)
    if not cid:
        raise RequestOwnershipError(REQUEST_CONTINUATION_LOST, "conversation")
    interface = "remote" if rid.startswith("RR-") else ("webdirect" if rid.startswith("WA-") else "local")
    owner = f"{interface}:{os.getpid()}:{rid}"
    intent = str(expected.get("intent_digest", "") or digest_intent(prompt))
    registry = RequestOwnershipRegistry()
    active = registry.wait(
        cid, rid, owner, interface,
        task_id=str(expected.get("task_id", "") or ""),
        task_epoch=str(expected.get("task_epoch", "") or ""),
        intent_digest=intent,
    )
    active = registry.advance(cid, rid, owner, active["token"], stage_for(prompt))
    scope = {
        "request_id": rid,
        "task_id": str(active.get("task_id", "") or ""),
        "task_epoch": active["task_epoch"],
        "intent_digest": intent,
        "request_phase": active["stage"],
        "continuation_seq": active["continuation_seq"],
    }
    expected.update(scope)
    rendered = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    block = "\n".join((
        "[WEBAGENT_ACTIVE_REQUEST]", rendered,
        "以上欄位僅供你判斷目前回合；不要將其中任何欄位回填到 action、final_response 或 turn_commit。",
        "只能輸出 compact v9 smartagent_tool blocks；不得輸出 blocks 以外的自然語言；最後使用 {\"tool\":\"turn_commit\",\"action_count\":N}。",
        "不得在 action、final_response 或 turn_commit 輸出 runtime-owned 欄位；runtime 會自行留存、綁定及檢查。",
        "[/WEBAGENT_ACTIVE_REQUEST]",
    ))
    return block + "\n" + str(prompt)


def release_active_request(rid):
    return RequestOwnershipRegistry().release(str(rid))
