#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7.2 integrated Agent Host lifecycle.

The normal user entry point owns the visible browser and LocalAgent.  A hidden
RemoteAgent-0 process attaches to that *same* Chromium instance over localhost
CDP, watches only already-armed conversations, and writes durable queue/handoff
state.  It never owns a second profile and never sends WebGPT prompts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT, normalize_chatgpt_url
from .remote_result import build_result_payload, build_result_prompt, validate_result_reply
from .routing import CARRIER_CHATGPT_CONVERSATION, select_carrier

HOST_STATE = AGENT_PROJECT_ROOT / ".agents" / "agent_host_state.json"
HANDOFF_STATE = AGENT_PROJECT_ROOT / ".agents" / "remote_handoff.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class IntegratedAgentHost:
    """Own Local/Remote lifecycle without exposing a second normal launcher."""

    def __init__(self, *, workspace: str, conversation_url: str):
        self.workspace = str(Path(workspace).resolve())
        self.conversation_url = normalize_chatgpt_url(conversation_url)
        self._proc: subprocess.Popen | None = None
        self._log_handle = None
        self._last_handoff_task_id = ""
        self._scraper = None
        self._stopped = False
        self._external_supervisor_token = os.environ.get("SMARTAGENT_SUPERVISOR_TOKEN", "")
        self._cdp_endpoint = ""

    def _write_state(self, status: str, **extra: Any) -> None:
        payload = {
            "version": 1,
            "host_pid": os.getpid(),
            "status": str(status),
            "workspace": self.workspace,
            "conversation_url": self.conversation_url,
            "updated_at": time.time(),
            "supervisor_token": self._external_supervisor_token,
            "cdp_endpoint": self._cdp_endpoint,
            **extra,
        }
        _atomic_json(HOST_STATE, payload)

    def start_hidden_supervisor(self, scraper: Any) -> dict[str, Any]:
        """Attach RemoteAgent-0 to the already-running browser over localhost CDP."""
        self._scraper = scraper
        endpoint = ""
        try:
            endpoint = str(scraper.get_cdp_endpoint(timeout_sec=8.0) or "")
        except Exception as exc:
            self._write_state("local_only", supervisor_error=f"{type(exc).__name__}: {exc}")
            return {"started": False, "reason": f"cdp_unavailable: {exc}"}
        if not endpoint:
            self._write_state("local_only", supervisor_error="empty_cdp_endpoint")
            return {"started": False, "reason": "empty_cdp_endpoint"}
        self._cdp_endpoint = endpoint

        if os.environ.get("SMARTAGENT_EXTERNAL_SUPERVISOR") == "1":
            token = os.environ.get("SMARTAGENT_SUPERVISOR_TOKEN", "")
            self._write_state("ready", cdp_endpoint=endpoint, supervisor_token=token)
            return {"started": True, "external": True, "cdp": endpoint}

        script = Path(__file__).resolve().parent.parent / "RemoteAgent" / "hidden_supervisor.py"
        if not script.exists():
            self._write_state("local_only", supervisor_error="hidden_supervisor_missing")
            return {"started": False, "reason": "hidden_supervisor_missing"}

        cmd = [
            sys.executable,
            str(script),
            "--cdp", endpoint,
            "--parent-pid", str(os.getpid()),
            "--poll", "10.0",
        ]
        creationflags = 0
        if os.name == "nt":
            # Stage 10 diagnostic mode: give Agent-0 its own visible status
            # console so mobile ingress and queue activity are observable.
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(script.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                creationflags=creationflags,
            )
        except Exception as exc:
            self._write_state("local_only", supervisor_error=f"{type(exc).__name__}: {exc}")
            return {"started": False, "reason": f"spawn_failed: {exc}"}

        self._write_state("idle", supervisor_pid=self._proc.pid, cdp_endpoint=endpoint)
        return {"started": True, "pid": self._proc.pid, "cdp": endpoint}

    def set_local_busy(self, busy: bool, *, workspace: str | None = None, conversation_url: str | None = None) -> None:
        if workspace:
            self.workspace = str(Path(workspace).resolve())
        if conversation_url:
            self.conversation_url = normalize_chatgpt_url(conversation_url)
        self._write_state("busy" if busy else "idle", supervisor_pid=getattr(self._proc, "pid", None))

    def activate_context(self, *, workspace: str, conversation_url: str) -> dict[str, Any]:
        """Switch the LocalAgent-owned browser to one exact registered task origin."""
        if self._scraper is None:
            return {"activated": False, "reason": "scraper_unavailable"}
        target_workspace = str(Path(workspace).resolve())
        target_url = normalize_chatgpt_url(conversation_url)
        if not target_url:
            return {"activated": False, "reason": "invalid_conversation_url"}
        try:
            self._scraper.navigate_to_conversation(target_url)
        except Exception as exc:
            return {"activated": False, "reason": f"{type(exc).__name__}: {exc}"}
        self.workspace = target_workspace
        self.conversation_url = target_url
        self._write_state("idle", supervisor_pid=getattr(self._proc, "pid", None))
        return {"activated": True, "workspace": target_workspace, "conversation_url": target_url}

    def return_remote_result(self, task: Any, *, status: str, summary: str) -> dict[str, Any]:
        """Post one correlated result to the already-bound ChatGPT conversation."""
        route = select_carrier(
            source="remote",
            conversation_url=str(getattr(task, "conversation_url", "") or ""),
            requested_carrier=(
                getattr(task, "metadata", {}).get("route_context", {})
                .get("carrier", {}).get("requested_carrier", "AUTO")
            ),
        )
        if route.selected_carrier != CARRIER_CHATGPT_CONVERSATION:
            return {
                "delivered": False,
                "reason": "carrier_adapter_not_implemented",
                "carrier_route": route.as_dict(),
            }
        if self._scraper is None:
            return {"delivered": False, "reason": "scraper_unavailable"}
        if normalize_chatgpt_url(task.conversation_url) != self.conversation_url:
            return {"delivered": False, "reason": "conversation_binding_mismatch"}
        payload = build_result_payload(task=task, status=status, summary=summary)
        prompt = build_result_prompt(payload)
        try:
            reply = self._scraper.ask(prompt, new_conversation=False)
        except Exception as exc:
            return {"delivered": False, "reason": f"{type(exc).__name__}: {exc}"}
        valid, reason = validate_result_reply(reply, request_id=task.request_id)
        if not valid:
            return {"delivered": False, "reason": reason, "reply": str(reply or "")[:2000]}
        return {
            "delivered": True,
            "reply": reply,
            "payload": payload,
            "carrier_route": route.as_dict(),
        }

    def consume_pending_handoff(self, scraper: Any | None = None) -> dict[str, Any] | None:
        """Apply a remote handoff only at a LocalAgent safe boundary."""
        path = HANDOFF_STATE
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("state") != "PENDING":
            return None
        task_id = str(payload.get("task_id", "") or "")
        if not task_id or task_id == self._last_handoff_task_id:
            return None
        url = normalize_chatgpt_url(payload.get("conversation_url", ""))
        workspace = str(Path(payload.get("workspace", "")).resolve())
        target_scraper = scraper or self._scraper
        if target_scraper is None:
            return None

        page = None
        context = getattr(target_scraper, "_browser", None)
        try:
            for candidate in list(getattr(context, "pages", []) or []):
                try:
                    if normalize_chatgpt_url(candidate.url) == url:
                        page = candidate
                        break
                except Exception:
                    continue
        except Exception:
            page = None
        if page is None:
            # The hidden supervisor normally creates/owns the background page.
            # At a Local safe boundary it is safe to create it here if missing.
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                return None

        try:
            page.bring_to_front()
        except Exception:
            pass
        target_scraper._page = page
        try:
            target_scraper.cfg["url"] = url
        except Exception:
            pass
        self.workspace = workspace
        self.conversation_url = url
        self._last_handoff_task_id = task_id
        payload["state"] = "APPLIED"
        payload["applied_at"] = time.time()
        payload["applied_by_pid"] = os.getpid()
        _atomic_json(path, payload)
        self._write_state("idle", last_handoff_task_id=task_id, supervisor_pid=getattr(self._proc, "pid", None))
        return payload

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._write_state("stopping", supervisor_pid=getattr(self._proc, "pid", None))
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._write_state("stopped")
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None


def run_agent_host_self_tests() -> dict[str, Any]:
    """Pure deterministic contract tests; no real browser needed."""
    import tempfile
    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        class Page:
            def __init__(self, url): self.url=url; self.front=False
            def bring_to_front(self): self.front=True
        class Context:
            def __init__(self, pages): self.pages=pages
        class Scraper:
            def __init__(self, pages): self._browser=Context(pages); self._page=pages[0]; self.cfg={"url":pages[0].url}
        p1=Page("https://chatgpt.com/c/a"); p2=Page("https://chatgpt.com/c/b")
        s=Scraper([p1,p2])
        host=IntegratedAgentHost(workspace=td, conversation_url=p1.url)
        # Monkeypatch module state paths only for this self-test.
        global HANDOFF_STATE, HOST_STATE
        old_h, old_s = HANDOFF_STATE, HOST_STATE
        HANDOFF_STATE=Path(td)/"handoff.json"; HOST_STATE=Path(td)/"host.json"
        try:
            _atomic_json(HANDOFF_STATE,{"state":"PENDING","task_id":"T1","conversation_url":p2.url,"workspace":td})
            got=host.consume_pending_handoff(s)
            results["safe_boundary_rebinds_existing_page"] = bool(got and s._page is p2 and p2.front)
            results["same_handoff_exactly_once"] = host.consume_pending_handoff(s) is None
            host.set_local_busy(True)
            data=json.loads(HOST_STATE.read_text(encoding="utf-8"))
            results["busy_state_persisted"] = data.get("status")=="busy"
        finally:
            HANDOFF_STATE, HOST_STATE = old_h, old_s
    results["all_passed"] = all(results.values())
    return results
