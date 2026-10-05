#!/usr/bin/env python3
"""Opt-in ChatGPT integration test for receiver-bound Project Sync."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from WebAgent.browser_bridge import adopt_page, execution_page_lease, open_or_attach_browser
from WebAgent.browser_client import WebAgentBrowserClient
from agent_core.project_sync_message import build_atomic_project_sync
from agent_core.project_sync_receiver import browser_receiver_identity, bind_receiver_transport
from agent_core.project_sync_runner import run_project_sync_transaction


def run(target_url: str, workspace: str) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace_not_found:{root}")

    attached_pw = None
    scraper = None
    with execution_page_lease(timeout_sec=180.0, label="Project Sync ChatGPT IT"):
        try:
            page, context, attached_pw, browser_mode = open_or_attach_browser(
                target_url,
                reuse_remote_agent_page=True,
            )
            scraper = adopt_page(page, context, attached_pw)
            scraper._expected_execution_url = target_url
            receiver_identity, transport = bind_receiver_transport(
                lambda prompt, attachments: WebAgentBrowserClient(
                    scraper,
                    display_name="ProjectSyncIT",
                ).ask(
                    prompt,
                    stage="Project Sync ChatGPT IT",
                    attachment_paths=attachments or None,
                    protocol_expected=None,
                ),
                lambda: browser_receiver_identity(scraper),
            )
            prepared = build_atomic_project_sync(
                root,
                "FULL_BUNDLE",
                max_bytes=128000,
                max_files=10,
            )
            if prepared.get("status") != "READY" or not prepared.get("transaction"):
                raise RuntimeError(
                    "project_sync_it_local_prepare_failed:"
                    + json.dumps(prepared, ensure_ascii=False, default=str)[:1000]
                )
            request_id = "PROJECT-SYNC-IT-" + uuid.uuid4().hex.upper()
            result = run_project_sync_transaction(
                root,
                prepared["transaction"],
                transport,
                interface_name="remote",
                conversation_id=receiver_identity,
                session_id="PROJECT-SYNC-IT-" + uuid.uuid4().hex,
                request_id=request_id,
            )
            return {
                "browser_mode": browser_mode,
                "actual_url": str(page.url),
                "receiver_identity": receiver_identity,
                "local_status": prepared.get("status"),
                "runtime_status": result.get("status"),
                "batch_count": result.get("batch_count"),
                "acknowledged_batches": result.get("acknowledged_batches"),
            }
        finally:
            if scraper is not None:
                try:
                    scraper.release_conversation_owner()
                except Exception:
                    pass
            if attached_pw is not None:
                try:
                    attached_pw.stop()
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.url, args.workspace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
