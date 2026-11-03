#!/usr/bin/env python3
"""SCR V1: finite semantic translation and request/epoch-bound tool validation."""
from __future__ import annotations

import tempfile
from pathlib import Path
from agent_core.semantic_capability_resolution import (
    SCHEMA, SemanticCapabilityError, normalize_resolution,
    verify_resolution_for_actions,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.tool_context import WebAgentToolContext

BOTH = {"schema": SCHEMA, "capabilities": ["directory.list", "directory.inspect_metadata"]}
LIST = {"schema": SCHEMA, "capabilities": ["directory.list"]}


def must_reject(callback, code):
    try:
        callback()
    except (SemanticCapabilityError, ValueError) as exc:
        assert code in str(exc), str(exc)
    else:
        raise AssertionError("unexpectedly admitted: " + code)


def run():
    assert normalize_resolution(BOTH) == BOTH
    assert validate_tool_envelope({
        "tool": "report_progress", "action_id": "SCR-1",
        "current_step": 1, "total_steps": 2, "current_focus": "read",
        "capability_resolution": BOTH,
    })[0]
    must_reject(lambda: normalize_resolution(
        {"schema": SCHEMA, "capabilities": ["dependency.install"]}),
        "semantic_capability_list_invalid")
    must_reject(lambda: normalize_resolution(
        {"schema": SCHEMA, "capabilities": ["directory.list", "directory.list"]}),
        "semantic_capability_list_invalid")
    must_reject(lambda: normalize_resolution(
        {"schema": SCHEMA, "capabilities": ["directory.list"], "authorized": True}),
        "semantic_capability_schema_invalid")
    must_reject(lambda: verify_resolution_for_actions(LIST, None, [
        {"tool": "inspect_directory"}]), "semantic_capability_not_selected")
    must_reject(lambda: verify_resolution_for_actions(LIST, BOTH, []),
                "semantic_capability_removal_forbidden")
    assert verify_resolution_for_actions(BOTH, LIST, [
        {"tool": "inspect_directory"}]) == BOTH

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        folder = root / "requested"
        folder.mkdir()
        (folder / "one.txt").write_text("one", encoding="utf-8")
        tools = WebAgentToolContext(root)
        paraphrases = (
            "請看看有哪些東西，並整理一下資料夾的狀況。",
            "Could you enumerate the entries and summarize the folder?",
            "麻煩盤點一下這個路徑的內容跟統計。",
        )
        for index, text in enumerate(paraphrases):
            rid = "SCR-" + str(index)
            tools.begin_run(rid, text, [str(folder)])
            tools.bind_runtime_capability_resolution(
                BOTH, request_id=rid, task_epoch=tools.current_task_epoch)
            for tool, fields in (
                ("list_directory", {"path": str(folder)}),
                ("inspect_directory", {"paths": [str(folder)], "recursive": False}),
            ):
                accepted = tools.preview_tool_scope({
                    "tool": tool, "action_id": "A", **fields})
                assert accepted["allowed"], accepted
            outside = tools.preview_tool_scope({
                "tool": "inspect_directory", "action_id": "B",
                "paths": [str(root)], "recursive": False})
            assert "resolved_scope_expands" in outside["error"], outside
        tools.begin_run("SCR-LIST-ONLY", "只要檔案清單", [str(folder)])
        tools.bind_runtime_capability_resolution(
            LIST, request_id="SCR-LIST-ONLY", task_epoch=tools.current_task_epoch)
        rejected = tools.preview_tool_scope({
            "tool": "inspect_directory", "action_id": "C",
            "paths": [str(folder)], "recursive": False})
        assert rejected["error"] == "semantic_capability_not_selected:inspect_directory", rejected
        must_reject(lambda: tools.bind_runtime_capability_resolution(
            BOTH, request_id="OTHER-REQUEST", task_epoch=tools.current_task_epoch),
            "semantic_capability_binding_request_identity_mismatch")
        tools.begin_run("SCR-FRESH", "new goal", [str(folder)])
        assert tools._runtime_semantic_capability_resolution is None
    print("SEMANTIC_CAPABILITY_RESOLUTION_IT_PASS")


if __name__ == "__main__":
    run()
