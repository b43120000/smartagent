#!/usr/bin/env python3
"""Request-first Workspace routing regression on Windows (offline, no side effects)."""
from __future__ import annotations

import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.task_workspace_resolver import (
    MAX_RECENT_TASKS, request_paths, resolve_task_workspace,
)
from agent_core.task_state import _allowed_task_binding
from agent_core.transport_message import NormalizedInboundMessage
from agent_core.agent_gateway import AgentIngressGateway


class FakeStore:
    def __init__(self, records=()):
        self.records = list(records)
    def process_lock(self): return nullcontext()
    def load(self): return self.records
    def all(self): return list(self.records)


def previous(n, workspace, *, sender="sender", conversation="telegram://chat/44", state="COMPLETED"):
    return SimpleNamespace(
        task_id="TASK-PAST-%02d" % n, request_id="RR-PAST-%02d" % n,
        conversation_url=conversation, metadata={"transport": "TELEGRAM", "sender_id": sender},
        workspace=str(workspace), state=state, created_at=float(n),
    )


def choose(text, store):
    return resolve_task_workspace(
        text, conversation_key="telegram://chat/44", transport="TELEGRAM",
        sender_id="sender", store=store,
    )


def rejected(function, code):
    try: function()
    except ValueError as exc:
        assert code in str(exc), (code, str(exc))
    else: raise AssertionError("expected fail-closed: " + code)


def run():
    assert MAX_RECENT_TASKS == 3
    with tempfile.TemporaryDirectory(prefix="workspace-route-") as name:
        root = Path(name).resolve()
        default = root / "default"
        project_a = root / "projectA"
        project_b = root / "projectB"
        readonly = root / "readonly"
        outside = root / "outside"
        for directory in (default, project_a, project_b, readonly, outside):
            directory.mkdir()
        bat = project_b / "collect.bat"
        bat.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
        policy = {"writable_workspaces": (str(default), str(project_a), str(project_b)),
                  "read_only_roots": (str(readonly),)}

        with patch("agent_core.task_workspace_resolver.access_snapshot", return_value=policy), \
             patch("agent_core.workspace_access.access_snapshot", return_value=policy):
            # Do not swallow positional arguments into the executable path.
            command = str(bat) + " TASK-C6A7B83EF0F633A988BC"
            assert request_paths(command) == [str(bat)], request_paths(command)
            quoted = "'" + str(bat) + "' 'TASK-C6A7B83EF0F633A988BC'"
            assert request_paths(quoted) == [str(bat)], request_paths(quoted)
            direct = choose("請執行 " + command, FakeStore())
            assert direct["workspace"] == str(default)
            assert direct["source"] == "WRITABLE_PRIMARY_DEFAULT"
            assert direct["explicit_paths"] == []
            assert str(bat) in direct["mentioned_paths"]

            # A narrative path is harmless; only an explicit Workspace directive
            # may select a Task working directory.
            assert choose("modify " + str(outside / "x.py"), FakeStore())["workspace"] == str(default)
            selected_directive = choose("Workspace: " + str(project_b), FakeStore())
            assert selected_directive["workspace"] == str(project_b)
            assert selected_directive["source"] == "EXPLICIT_WORKSPACE_DIRECTIVE"
            rejected(lambda: choose("Workspace: " + str(outside), FakeStore()),
                     "request_path_outside_authorized_roots")
            # Read-only paths are usable as read targets, not active write roots.
            read = choose("read " + str(readonly / "test.txt"), FakeStore())
            assert read["workspace"] == str(default)
            assert read["source"] == "WRITABLE_PRIMARY_DEFAULT"

            # Three recent completed Tasks, with no time cutoff.
            hist = FakeStore([
                previous(1, project_b), previous(2, project_a),
                previous(3, project_a), previous(4, project_a),
            ])
            recent = choose("繼續處理", hist)
            assert recent["workspace"] == str(project_a)
            assert recent["source"] == "RECENT_TASK_UNIQUE"
            assert len(recent["context_candidates"]) == 3
            assert all(item["task_id"] != "TASK-PAST-01" for item in recent["context_candidates"])

            # Different recent roots: exact directory mention disambiguates.
            mixed = FakeStore([previous(1, project_a), previous(2, project_b)])
            selected = choose("請繼續 projectB 的驗證", mixed)
            assert selected["workspace"] == str(project_b)
            assert selected["source"] == "RECENT_TASK_NAMED"
            ambiguous = choose("繼續之前的工作", mixed)
            assert ambiguous["workspace"] == str(default)
            assert ambiguous["source"] == "WRITABLE_PRIMARY_DEFAULT"

            # Explicit Task continuation bypasses three-task window (no TTL).
            history = FakeStore([previous(1, project_b)] + [
                previous(i, project_a) for i in range(2, 7)
            ])
            resumed = choose("接續工作 TASK-PAST-01", history)
            assert resumed["workspace"] == str(project_b)
            assert resumed["source"] == "EXPLICIT_CONTINUATION"
            assert resumed["source_task_id"] == "TASK-PAST-01"

            # Sender or conversation changes must not inherit someone else's root.
            other = FakeStore([previous(9, project_b, sender="other")])
            assert choose("繼續", other)["workspace"] == str(default)
            other_channel = FakeStore([previous(9, project_b, conversation="telegram://chat/55")])
            assert choose("繼續", other_channel)["workspace"] == str(default)
            rejected(lambda: choose("接續工作 TASK-PAST-09", other),
                     "continuation_workspace_reference_not_found_or_ambiguous")

            # Queue may accept a dynamic subworkspace only for an authorized
            # transport endpoint and only when the writable policy still allows it.
            allowed = {(str(default), "telegram://chat/44")}
            authorized_task = SimpleNamespace(
                workspace=str(project_b), conversation_url="telegram://chat/44",
                metadata={"workspace_resolution_version": 1},
            )
            assert _allowed_task_binding(authorized_task, allowed)
            authorized_task.conversation_url = "telegram://chat/55"
            assert not _allowed_task_binding(authorized_task, allowed)
            authorized_task.conversation_url = "telegram://chat/44"
            authorized_task.workspace = str(outside)
            assert not _allowed_task_binding(authorized_task, allowed)

            # Gateway must pin request resolution at durable queue admission.
            class FakeRouter:
                def resolve(self, _message, *, workspace, force_new=False):
                    return SimpleNamespace(session_id="SESSION-TEST")
            class FakeQueue:
                store = FakeStore()
                def enqueue_remote_request(self, message, **kwargs):
                    return SimpleNamespace(
                        task_id="TASK-GATEWAY", request_id=message["request_id"],
                        workspace=message["workspace"],
                        metadata=kwargs["metadata"],
                    ), True
            gateway = AgentIngressGateway(task_queue=FakeQueue(), session_router=FakeRouter())
            message = NormalizedInboundMessage(
                transport="TELEGRAM", endpoint="telegram", conversation_key="telegram://chat/44",
                sender_id="sender", source_message_id="M-1",
                text="Workspace: " + str(project_b), received_at=1.0, idempotency_key="KEY-1",
            )
            accepted = gateway.accept(message, workspace=str(default))
            assert accepted.task.workspace == str(project_b)
            assert accepted.task.metadata["workspace_resolution_version"] == 1
            assert accepted.task.metadata["workspace_resolution"]["source"] == "EXPLICIT_WORKSPACE_DIRECTIVE"

    print("REQUEST_FIRST_WORKSPACE_ROUTING_PASS")


if __name__ == "__main__":
    run()
