#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared persistent registry for Workspace <-> ChatGPT conversation state.

Stage 3 replaces the old workspace-only profile list with one conversation-level
source of truth.  The legacy ``workspace_links.json`` file is read once for
migration so existing LocalAgent installations keep their saved profiles.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext as _nullcontext
from pathlib import Path
from typing import Iterable
from .process_file_lock import exclusive_process_lock

from .workspace import (
    AGENT_PROJECT_ROOT,
    DEFAULT_PROFILE_STORE,
    normalize_chatgpt_url,
    normalize_workspace_path,
)

DEFAULT_CONVERSATION_STORE = AGENT_PROJECT_ROOT / ".agents" / "conversations.json"
REGISTRY_VERSION = 4
_CR=re.compile(r"/c/([^/?#]+)",re.I)

def _cid(u: str) -> str:
 u=normalize_chatgpt_url(u);m=_CR.search(u);return "c:"+m.group(1).lower() if m else "u:"+u

def _same_url(a: str,b: str) -> bool:
 try:return _cid(a)==_cid(b)
 except ValueError:return False

def _binding_key(workspace: str, gpt_url: str) -> tuple[str, str]:
    return os.path.normcase(str(workspace)), _cid(gpt_url)


def _empty_record(workspace: str, gpt_url: str) -> dict:
    return {
        "workspace": workspace,
        "gpt_url": gpt_url,
        "purpose": "general",
        "display_name": "",
        "transport": "WEBGPT",
        "remote_enabled": False,
        "poll_profile": "normal",
        "remote_schedule": {
            "next_check_at": 0.0,
            "last_check_at": 0.0,
            "last_successful_sync": 0.0,
            "health": "UNAVAILABLE",
            "failure_count": 0,
        },
        "active_workspace": workspace,
        "default_workspace": workspace,
        "known_workspaces": [workspace],
        "watch_cursors": {},
        "protocols": {},
        "last_seen_turn": "",
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _normalize_protocol_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = {
        "protocol_name": str(raw.get("protocol_name", "") or ""),
        "protocol_version": int(raw.get("protocol_version", 0) or 0),
        "protocol_hash": str(raw.get("protocol_hash", "") or ""),
        "armed": bool(raw.get("armed", False)),
        "session_id": str(raw.get("session_id", "") or ""),
        "session_state": str(raw.get("session_state", "") or ""),
        "last_protocol_check": float(raw.get("last_protocol_check", 0.0) or 0.0),
        "last_session_attach": float(raw.get("last_session_attach", 0.0) or 0.0),
    }
    return result


def _normalize_record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        workspace = normalize_workspace_path(raw.get("workspace", ""))
        gpt_url = normalize_chatgpt_url(raw.get("gpt_url", ""))
    except ValueError:
        return None

    record = _empty_record(workspace, gpt_url)
    record["purpose"] = str(raw.get("purpose", "general") or "general")
    record["display_name"] = str(
        raw.get("display_name", raw.get("conversation_title", "")) or ""
    ).strip()
    record["transport"] = str(raw.get("transport", "WEBGPT") or "WEBGPT").upper()
    record["remote_enabled"] = bool(raw.get("remote_enabled", False))
    record["poll_profile"] = str(raw.get("poll_profile", "normal") or "normal").lower()
    if record["poll_profile"] not in {"active", "normal", "inactive"}:
        record["poll_profile"] = "normal"
    schedule = raw.get("remote_schedule", {})
    if isinstance(schedule, dict):
        target = record["remote_schedule"]
        for name in ("next_check_at", "last_check_at", "last_successful_sync"):
            target[name] = float(schedule.get(name, target[name]) or 0.0)
        target["health"] = str(schedule.get("health", target["health"]) or "UNAVAILABLE").upper()
        target["failure_count"] = int(schedule.get("failure_count", target["failure_count"]) or 0)
    record["active_workspace"] = str(raw.get("active_workspace") or workspace)
    record["default_workspace"] = str(raw.get("default_workspace") or workspace)
    known = raw.get("known_workspaces", [])
    if isinstance(known, list):
        record["known_workspaces"] = list(dict.fromkeys(
            str(item) for item in known if str(item).strip()
        ))
    if workspace not in record["known_workspaces"]:
        record["known_workspaces"].append(workspace)
    cursors = raw.get("watch_cursors", {})
    if isinstance(cursors, dict):
        record["watch_cursors"] = {
            str(name): str(value or "")
            for name, value in cursors.items()
            if str(name).strip()
        }
    record["last_seen_turn"] = str(raw.get("last_seen_turn", "") or "")
    record["created_at"] = float(raw.get("created_at", record["created_at"]) or record["created_at"])
    record["updated_at"] = float(raw.get("updated_at", record["updated_at"]) or record["updated_at"])
    protocols = raw.get("protocols", {})
    if isinstance(protocols, dict):
        record["protocols"] = {
            str(name): _normalize_protocol_state(state)
            for name, state in protocols.items()
            if str(name).strip() and isinstance(state, dict)
        }
    return record


def _merge_alias(a: dict,b: dict) -> dict:
    old,new=(a,b) if float(a.get("updated_at",0) or 0)<=float(b.get("updated_at",0) or 0) else (b,a)
    out=copy.deepcopy(new);known=[]
    for src in (old,new):
        for x in src.get("known_workspaces",[]) or []:
            if x and x not in known: known.append(x)
    if out.get("workspace") and out["workspace"] not in known: known.append(out["workspace"])
    out["known_workspaces"]=known
    for n in ("watch_cursors","protocols"):
        d=copy.deepcopy(old.get(n,{}));d.update(copy.deepcopy(new.get(n,{})));out[n]=d
    out["created_at"]=min(float(a.get("created_at",0) or 0),float(b.get("created_at",0) or 0));out["updated_at"]=max(float(a.get("updated_at",0) or 0),float(b.get("updated_at",0) or 0))
    return out


class ConversationRegistry:
    """Atomic conversation/session registry shared by LocalAgent and RemoteAgent."""

    def __init__(
        self,
        path: str | Path = DEFAULT_CONVERSATION_STORE,
        *,
        legacy_profile_path: str | Path | None = DEFAULT_PROFILE_STORE,
    ):
        self.path = Path(path)
        self.legacy_profile_path = Path(legacy_profile_path) if legacy_profile_path else None
        self.records: list[dict] = []
        self._lock = threading.RLock()

    @contextmanager
    def process_lock(self, timeout_sec: float = 10.0):
        """Serialize registry transactions with a crash-safe kernel lock."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        with exclusive_process_lock(lock_path,timeout_sec=timeout_sec,label="conversation registry",legacy_kind="conversation-registry-sentinel-v2"):
            yield

    def _load_legacy_profiles(self) -> list[dict]:
        path = self.legacy_profile_path
        if path is None or not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw_profiles = payload.get("profiles", []) if isinstance(payload, dict) else []
        records = []
        seen = set()
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            try:
                workspace = normalize_workspace_path(item.get("workspace", ""))
                gpt_url = normalize_chatgpt_url(item.get("gpt_url", ""))
            except ValueError:
                continue
            key = _binding_key(workspace, gpt_url)
            if key in seen:
                continue
            seen.add(key)
            records.append(_empty_record(workspace, gpt_url))
        return records

    def load(self) -> list[dict]:
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            raw_records = payload.get("conversations", []) if isinstance(payload, dict) else []
            by_key={};order=[]
            for raw in raw_records:
                record=_normalize_record(raw)
                if not record: continue
                key=_binding_key(record["workspace"],record["gpt_url"])
                if key in by_key: by_key[key]=_merge_alias(by_key[key],record)
                else: by_key[key]=record;order.append(key)
            self.records=[by_key[k] for k in order]
        else:
            self.records = self._load_legacy_profiles()

        # Do not persist legacy migration from a read-only load. Startup loads
        # can run in LocalAgent and Agent0 concurrently; the next locked
        # mutation will reload/migrate and save without overwriting newer data.
        return copy.deepcopy(self.records)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "conversations": self.records,
        }
        tmp = self.path.with_name(
            self.path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _ensure_loaded(self) -> None:
        if not self.records:
            self.load()

    @contextmanager
    def _mutation(self, persist: bool = True):
        """Reload and mutate under one in-process/cross-process boundary."""
        with self._lock:
            lock_context = self.process_lock() if persist else _nullcontext()
            with lock_context:
                if persist:
                    self.load()
                else:
                    self._ensure_loaded()
                yield

    def list_bindings(self) -> list[dict]:
        self._ensure_loaded()
        return [
            {
                "workspace": record["workspace"],
                "gpt_url": record["gpt_url"],
                "display_name": str(record.get("display_name", "") or ""),
            }
            for record in self.records
            if str(record.get("purpose", "general")) == "general"
        ]

    def list_conversations(self) -> list[dict]:
        self._ensure_loaded()
        return copy.deepcopy(self.records)

    def find_by_url(self, gpt_url: str) -> dict | None:
        gpt_url = normalize_chatgpt_url(gpt_url)
        self._ensure_loaded()
        for record in self.records:
            if _same_url(record.get("gpt_url", ""), gpt_url):
                return copy.deepcopy(record)
        return None

    def find(self, workspace: str, gpt_url: str) -> dict | None:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        self._ensure_loaded()
        key = _binding_key(workspace, gpt_url)
        for record in self.records:
            if _binding_key(record["workspace"], record["gpt_url"]) == key:
                return copy.deepcopy(record)
        return None

    def upsert_binding(self, workspace: str, gpt_url: str, *, purpose: str = "general", persist: bool = True) -> tuple[dict, bool]:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        with self._mutation(persist):
            key = _binding_key(workspace, gpt_url)
            for record in self.records:
                if _binding_key(record["workspace"], record["gpt_url"]) == key:
                    return copy.deepcopy(record), False
            record = _empty_record(workspace, gpt_url)
            record["purpose"] = str(purpose or "general")
            self.records.append(record)
            if persist:
                self.save()
            return copy.deepcopy(record), True

    def replace_bindings(self, profiles: Iterable[dict]) -> None:
        """Replace binding list while preserving protocol state of existing records."""
        with self._mutation(True):
            existing = {
                _binding_key(record["workspace"], record["gpt_url"]): record
                for record in self.records
            }
            rebuilt = []
            seen = set()
            for profile in profiles or []:
                if not isinstance(profile, dict):
                    continue
                try:
                    workspace = normalize_workspace_path(profile.get("workspace", ""))
                    gpt_url = normalize_chatgpt_url(profile.get("gpt_url", ""))
                except ValueError:
                    continue
                key = _binding_key(workspace, gpt_url)
                if key in seen:
                    continue
                seen.add(key)
                record = copy.deepcopy(existing.get(key) or _empty_record(workspace, gpt_url))
                display_name = str(profile.get("display_name", "") or "").strip()
                if display_name:
                    record["display_name"] = display_name
                rebuilt.append(record)
            rebuilt.extend(
                copy.deepcopy(record) for record in self.records
                if str(record.get("purpose", "general")) != "general"
                and _binding_key(record["workspace"], record["gpt_url"])
                not in {_binding_key(item["workspace"], item["gpt_url"]) for item in rebuilt}
            )
            self.records = rebuilt
            self.save()

    def set_display_name(self, gpt_url: str, display_name: str, *, persist: bool = True) -> str:
        """Persist the best-effort visible title for a ChatGPT conversation."""
        gpt_url = normalize_chatgpt_url(gpt_url)
        name = str(display_name or "").strip()
        with self._mutation(persist):
            for record in self.records:
                if not _same_url(record.get("gpt_url", ""), gpt_url):
                    continue
                if name:
                    record["display_name"] = name
                    record["updated_at"] = time.time()
                    if persist:
                        self.save()
                return str(record.get("display_name", "") or "")
            return ""

    def set_active_workspace(
        self,
        gpt_url: str,
        workspace: str,
        *,
        source: str = "",
        persist: bool = True,
        add_known: bool = True,
    ) -> dict:
        gpt_url = normalize_chatgpt_url(gpt_url)
        workspace = normalize_workspace_path(workspace)
        with self._mutation(persist):
            for record in self.records:
                if not _same_url(record.get("gpt_url", ""), gpt_url):
                    continue
                record["active_workspace"] = workspace
                if add_known:
                    known = record.setdefault("known_workspaces", [])
                    if workspace not in known:
                        known.append(workspace)
                record["workspace_source"] = str(source or "")
                record["updated_at"] = time.time()
                if persist:
                    self.save()
                return copy.deepcopy(record)
            raise ValueError(f"conversation not registered: {gpt_url}")

    def get_protocol_state(self, workspace: str, gpt_url: str, protocol_name: str) -> dict:
        record = self.find(workspace, gpt_url)
        if not record:
            return {}
        state = record.get("protocols", {}).get(str(protocol_name), {})
        return copy.deepcopy(state) if isinstance(state, dict) else {}

    def set_protocol_state(
        self,
        workspace: str,
        gpt_url: str,
        protocol_name: str,
        state: dict,
        *,
        persist: bool = True,
    ) -> dict:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        protocol_name = str(protocol_name or "").strip()
        if not protocol_name:
            raise ValueError("protocol_name 不可為空")
        with self._mutation(persist):
            key = _binding_key(workspace, gpt_url)
            target = None
            for record in self.records:
                if _binding_key(record["workspace"], record["gpt_url"]) == key:
                    target = record
                    break
            if target is None:
                target = _empty_record(workspace, gpt_url)
                self.records.append(target)
            normalized = _normalize_protocol_state({**state, "protocol_name": protocol_name})
            target.setdefault("protocols", {})[protocol_name] = normalized
            target["updated_at"] = time.time()
            if persist:
                self.save()
            return copy.deepcopy(normalized)

    def set_last_seen_turn(self, workspace: str, gpt_url: str, fingerprint: str, *, persist: bool = True) -> None:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        with self._mutation(persist):
            key = _binding_key(workspace, gpt_url)
            for record in self.records:
                if _binding_key(record["workspace"], record["gpt_url"]) == key:
                    record["last_seen_turn"] = str(fingerprint or "")
                    record["updated_at"] = time.time()
                    if persist:
                        self.save()
                    return
            record = _empty_record(workspace, gpt_url)
            record["last_seen_turn"] = str(fingerprint or "")
            self.records.append(record)
            if persist:
                self.save()

    def get_watch_cursor(self, gpt_url: str, cursor_name: str) -> str:
        record = self.find_by_url(gpt_url)
        if not record:
            return ""
        cursors = record.get("watch_cursors", {})
        return str(cursors.get(str(cursor_name), "") or "") if isinstance(cursors, dict) else ""

    def set_watch_cursor(
        self,
        gpt_url: str,
        cursor_name: str,
        fingerprint: str,
        *,
        persist: bool = True,
    ) -> None:
        gpt_url = normalize_chatgpt_url(gpt_url)
        cursor_name = str(cursor_name or "").strip()
        if not cursor_name:
            raise ValueError("cursor_name 不可為空")
        with self._mutation(persist):
            for record in self.records:
                if _same_url(record.get("gpt_url", ""), gpt_url):
                    record.setdefault("watch_cursors", {})[cursor_name] = str(fingerprint or "")
                    record["updated_at"] = time.time()
                    if persist:
                        self.save()
                    return
            raise ValueError(f"conversation not registered: {gpt_url}")


    def configure_remote_conversation(self, workspace: str, gpt_url: str, *, enabled: bool, poll_profile: str = "normal", transport: str = "WEBGPT", persist: bool = True) -> dict:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        profile = str(poll_profile or "normal").lower()
        if profile not in {"active", "normal", "inactive"}:
            raise ValueError(f"unsupported poll_profile: {poll_profile}")
        transport = str(transport or "WEBGPT").upper()
        with self._mutation(persist):
            key = _binding_key(workspace, gpt_url)
            for record in self.records:
                if _binding_key(record["workspace"], record["gpt_url"]) == key:
                    record["transport"] = transport
                    record["remote_enabled"] = bool(enabled)
                    record["poll_profile"] = profile
                    record["updated_at"] = time.time()
                    if persist:
                        self.save()
                    return copy.deepcopy(record)
            raise ValueError(f"conversation not registered: {gpt_url}")

    def update_remote_schedule(self, workspace: str, gpt_url: str, **values) -> dict:
        workspace = normalize_workspace_path(workspace)
        gpt_url = normalize_chatgpt_url(gpt_url)
        with self._mutation(True):
            key = _binding_key(workspace, gpt_url)
            allowed = {"next_check_at", "last_check_at", "last_successful_sync", "health", "failure_count", "last_reconcile_at", "last_seen_turn"}
            for record in self.records:
                if _binding_key(record["workspace"], record["gpt_url"]) != key:
                    continue
                schedule = record.setdefault("remote_schedule", {})
                for name, value in values.items():
                    if name not in allowed:
                        continue
                    if name in {"next_check_at", "last_check_at", "last_successful_sync", "last_reconcile_at"}:
                        schedule[name] = float(value or 0.0)
                    elif name == "failure_count":
                        schedule[name] = int(value or 0)
                    elif name == "last_seen_turn":
                        record["last_seen_turn"] = str(value or "")
                    else:
                        schedule[name] = str(value or "UNAVAILABLE").upper()
                record["updated_at"] = time.time()
                self.save()
                return copy.deepcopy(record)
            raise ValueError(f"conversation not registered: {gpt_url}")


    def list_remote_conversations(self, *, enabled_only: bool = True) -> list[dict]:
        self._ensure_loaded()
        rows = []
        for record in self.records:
            if str(record.get("transport", "WEBGPT")).upper() != "WEBGPT":
                continue
            if enabled_only and not bool(record.get("remote_enabled", False)):
                continue
            rows.append(copy.deepcopy(record))
        return rows


def run_conversation_registry_self_tests() -> dict:
    import tempfile

    results = {}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        workspace.mkdir()
        legacy = root / "workspace_links.json"
        store = root / "conversations.json"
        url = "https://chatgpt.com/c/test"
        legacy.write_text(
            json.dumps({"version": 1, "profiles": [{"workspace": str(workspace), "gpt_url": url}]}),
            encoding="utf-8",
        )

        registry = ConversationRegistry(store, legacy_profile_path=legacy)
        loaded = registry.load()
        results["legacy_migration"] = len(loaded) == 1 and store.exists()
        results["binding_preserved"] = loaded[0]["workspace"] == str(workspace.resolve()) and loaded[0]["gpt_url"] == url

        registry.set_protocol_state(
            str(workspace), url, "smart_agent",
            {
                "protocol_version": 3,
                "protocol_hash": "abc",
                "armed": True,
                "session_id": "S-1",
                "session_state": "ACTIVE",
                "last_protocol_check": 10.0,
                "last_session_attach": 11.0,
            },
        )
        reloaded = ConversationRegistry(store, legacy_profile_path=legacy)
        reloaded.load()
        state = reloaded.get_protocol_state(str(workspace), url, "smart_agent")
        results["restart_persistence"] = (
            state.get("armed") is True
            and state.get("protocol_version") == 3
            and state.get("protocol_hash") == "abc"
            and state.get("session_id") == "S-1"
        )

        reloaded.replace_bindings([{"workspace": str(workspace), "gpt_url": url}])
        state2 = reloaded.get_protocol_state(str(workspace), url, "smart_agent")
        results["replace_preserves_protocol_state"] = state2.get("armed") is True and state2.get("protocol_hash") == "abc"

    results["all_passed"] = all(results.values())
    return results
