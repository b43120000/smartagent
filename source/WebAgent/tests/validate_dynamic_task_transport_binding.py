#!/usr/bin/env python3
"""Offline regression: RemoteAgent dynamic Workspace is not WebGPT URL binding."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.task_transport import (
    resolve_task_execution_chatgpt_url, validate_task_transport,
)

URL = "https://chatgpt.com/g/g-p-workspace-routing-test"
CHAT = "telegram://chat/44"


class Registry:
    def __init__(self, registered=True):
        self.registered = registered

    def find_by_url(self, url):
        return {"gpt_url": url, "workspace": "transport-bound"} if self.registered and url == URL else None

    def find(self, workspace, url):
        return None  # Dynamic workspaces are not transport bindings.


def task(directory, *, transport="TELEGRAM", dynamic=True, origin=CHAT):
    meta = {
        "transport": transport,
        "authorized": True,
        "workspace_resolution_version": 1 if dynamic else 0,
    }
    if dynamic:
        meta["workspace_resolution"] = {
            "workspace": str(directory), "source": "CURRENT_REQUEST",
            "source_task_id": "", "context_candidates": [],
            "explicit_paths": [str(directory)],
        }
    if transport in {"WEBGPT", "WEBGPT_COPILOT"}:
        return SimpleNamespace(
            workspace=str(directory), metadata=meta,
            conversation_url=URL,
            reply_route={"transport": transport, "conversation_key": URL},
        )
    return SimpleNamespace(
        workspace=str(directory), metadata=meta,
        conversation_url=origin,
        reply_route={"transport": transport, "conversation_key": origin, "chat_id": 44},
    )


def rejected(action, expected):
    try:
        action()
    except RuntimeError as exc:
        assert str(exc) == expected, (expected, str(exc))
    else:
        raise AssertionError("expected rejection: " + expected)


def run():
    with tempfile.TemporaryDirectory(prefix="remote-dynamic-binding-") as temp:
        root = Path(temp).resolve()
        bound = root / "binding-default"
        requested = root / "requested-project"
        outside = root / "outside"
        for folder in (bound, requested, outside):
            folder.mkdir()
        policy = {
            "writable_workspaces": [str(bound), str(requested)],
            "read_only_roots": [],
        }
        binding = {"workspace": str(bound), "gpt_url": URL, "skill_path": str(root)}
        with patch("agent_core.remote_binding.active", return_value=binding), \
             patch("agent_core.workspace_access.access_snapshot", return_value=policy):
            # Repro before fix: workspace != transport binding, yet both valid.
            valid = task(requested)
            context = validate_task_transport(valid, registry=Registry())
            assert context.transport == "TELEGRAM"
            assert resolve_task_execution_chatgpt_url(valid, registry=Registry()) == URL

            # Existing non-dynamic task must keep strict legacy binding.
            legacy = task(requested, dynamic=False)
            rejected(
                lambda: resolve_task_execution_chatgpt_url(legacy, registry=Registry()),
                "remote_task_workspace_binding_mismatch",
            )

            # A synthetic v1 marker without Gateway-owned canonical provenance
            # must not disable the old workspace safety boundary.
            forged = task(outside)
            rejected(lambda: resolve_task_execution_chatgpt_url(forged, registry=Registry()),
                     "remote_task_workspace_not_authorized")
            mismatch = task(requested)
            mismatch.metadata["workspace_resolution"]["workspace"] = str(bound)
            rejected(lambda: resolve_task_execution_chatgpt_url(mismatch, registry=Registry()),
                     "remote_task_workspace_resolution_mismatch")
            missing = task(requested)
            del missing.metadata["workspace_resolution"]
            rejected(lambda: resolve_task_execution_chatgpt_url(missing, registry=Registry()),
                     "remote_task_workspace_resolution_missing")

            # ChatGPT transport origin must remain registered, independently
            # of the Task's dynamic writable root.
            webgpt = task(requested, transport="WEBGPT")
            assert validate_task_transport(webgpt, registry=Registry()).chatgpt_url == URL
            rejected(lambda: resolve_task_execution_chatgpt_url(webgpt, registry=Registry(False)),
                     "remote_task_origin_not_registered")

            # Never accept the Telegram task if its reply endpoint was swapped.
            tampered = task(requested)
            tampered.reply_route["transport"] = "WEBGPT"
            rejected(lambda: resolve_task_execution_chatgpt_url(tampered, registry=Registry()),
                     "remote_reply_transport_mismatch")

    print("REMOTE_DYNAMIC_TASK_TRANSPORT_BINDING_PASS")


if __name__ == "__main__":
    run()
