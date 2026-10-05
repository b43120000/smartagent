#!/usr/bin/env python3
"""Own the retained browser session used by RemoteAgent execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .paths import remote_binding_path


class RemoteBrowserSessionManager:
    """Create, retain, and release the RemoteAgent WebGPT browser session."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def binding(self) -> dict[str, Any]:
        from .remote_binding import active

        binding = active()
        if binding:
            return binding
        from .conversation_registry import ConversationRegistry

        target = str(
            os.environ.get("SMARTAGENT_TELEGRAM_WORKSPACE", "") or ""
        ).strip()
        if not target:
            return {}
        target_path = Path(target).resolve()
        explicit_url = str(
            os.environ.get("SMARTAGENT_REMOTE_EXECUTION_URL", "") or ""
        ).strip()
        if explicit_url:
            return {"workspace": str(target_path), "gpt_url": explicit_url}
        primary_path = remote_binding_path(self.host.root)
        try:
            primary = json.loads(primary_path.read_text(encoding="utf-8"))
            if Path(str(primary.get("workspace", ""))).resolve() == target_path:
                return dict(primary)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        rows = []
        for row in ConversationRegistry().list_remote_conversations(
            enabled_only=True
        ):
            try:
                if Path(str(row.get("workspace", "") or "")).resolve() != target_path:
                    continue
            except Exception:
                continue
            if str(row.get("purpose", "general") or "general") != "general":
                continue
            url = str(row.get("gpt_url", "") or "").strip()
            if "/c/" not in url:
                continue
            rows.append(row)
        rows.sort(
            key=lambda row: float(row.get("updated_at", 0.0) or 0.0),
            reverse=True,
        )
        return dict(rows[0]) if rows else {}

    def ensure(self) -> dict[str, Any]:
        host = self.host
        if host._remote_browser_scraper is not None and host._remote_browser_state:
            return dict(host._remote_browser_state)
        binding = host._remote_binding()
        if not binding:
            raise RuntimeError("remote_linked_conversation_missing")
        url = str(binding["gpt_url"])
        workspace = str(binding["workspace"])
        from .web_runtime import WebLLMScraper

        scraper = WebLLMScraper("chatgpt")
        scraper._conversation_owner_interface = "remote"
        scraper.cfg = dict(scraper.cfg)
        scraper.cfg["url"] = url
        previous_attach = os.environ.get("SMARTAGENT_ATTACH_CDP")
        previous_cdp = os.environ.get("SMARTAGENT_CHATGPT_CDP")
        shared = host.live_local_agent_state()
        try:
            if shared:
                os.environ["SMARTAGENT_ATTACH_CDP"] = "1"
                os.environ["SMARTAGENT_CHATGPT_CDP"] = str(
                    shared["cdp_endpoint"]
                )
            else:
                os.environ.pop("SMARTAGENT_ATTACH_CDP", None)
            try:
                scraper.start()
            except Exception:
                try:
                    scraper.close()
                except Exception:
                    pass
                raise
        finally:
            if previous_attach is None:
                os.environ.pop("SMARTAGENT_ATTACH_CDP", None)
            else:
                os.environ["SMARTAGENT_ATTACH_CDP"] = previous_attach
            if previous_cdp is None:
                os.environ.pop("SMARTAGENT_CHATGPT_CDP", None)
            else:
                os.environ["SMARTAGENT_CHATGPT_CDP"] = previous_cdp

        endpoint = scraper.get_cdp_endpoint()
        host._remote_browser_scraper = scraper
        host._remote_browser_state = {
            "status": "ready",
            "host_pid": os.getpid(),
            "cdp_endpoint": endpoint,
            "workspace": workspace,
            "conversation_url": url,
            "supervisor_token": host.token,
            "startup_mode": "TASK_DEMAND",
        }
        print(f"[RemoteAgent-0] SESSION_READY {url}", flush=True)
        return dict(host._remote_browser_state)

    def close(self) -> None:
        host = self.host
        scraper = host._remote_browser_scraper
        conversation_url = str(
            host._remote_browser_state.get("conversation_url", "") or ""
        )
        host._remote_browser_scraper = None
        host._remote_browser_state = {}
        if scraper is None:
            return
        # A software reconnect is a hard WebGPT page boundary.  Close the
        # retained page explicitly before tearing down its context/Playwright
        # driver so the next ensure() cannot accidentally reuse stale DOM,
        # navigation, or generation state.
        page = getattr(scraper, "_page", None)
        if page is not None:
            try:
                page.close(run_before_unload=False)
            except TypeError:
                page.close()
            except Exception:
                pass
            scraper._page = None
        try:
            if conversation_url:
                scraper.release_conversation(conversation_url, owner="remote")
        except Exception:
            pass
        try:
            scraper.close()
        except Exception:
            pass

    def restart(self) -> dict[str, Any]:
        """Force-close and immediately rebuild the retained WebGPT session."""
        host = self.host
        self.close()
        host.remote_runtime_log.write(
            "RECONNECT",
            component="remote_browser_session_manager",
            stage="REMOTE_BROWSER_CLOSED",
        )
        try:
            state = self.ensure()
        except Exception as exc:
            host.remote_runtime_log.write(
                "ERROR",
                component="remote_browser_session_manager",
                stage="REMOTE_BROWSER_REOPEN_FAILED",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        host.remote_runtime_log.write(
            "RECONNECT",
            component="remote_browser_session_manager",
            stage="REMOTE_BROWSER_REOPENED",
            conversation_url=str(state.get("conversation_url", "") or ""),
            cdp_endpoint=str(state.get("cdp_endpoint", "") or ""),
        )
        return state
