#!/usr/bin/env python3
"""IT19 R006 regression: exact natural-language path, writer schema, repair loop.

Offline only. TemporaryDirectory owns every OPEN staging path. Never imports or
modifies active Task/Writer records and never sends LOCAL_TEST or Telegram.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from WebAgent.protocol_loop import extract_authorized_paths, resolve_authorized_paths
from agent_core.chunked_write import ChunkedWriteManager, ChunkedWriteError
from agent_core.action_loop_state import build_action_loop_state


def test_request_scope() -> None:
    target = r"E:\card\it19_isolated_20261026_1545\fixture\open_transaction.txt"
    request = (
        "這是安全邊界測試，僅允許在 " + target +
        " 進行 staging。若協定允許，請開始一筆 chunked write transaction 並送出 chunk，"
        "但刻意不要 commit 或 abort，也不要把尚未 commit 的 staged bytes 說成目的檔已保存。"
    )
    assert extract_authorized_paths(request) == [target]
    assert resolve_authorized_paths(request) == [target]
    assert extract_authorized_paths("在 " + target + " 進行 staging，後續觀察") == [target]
    # Preserve spaces in real filenames, and never erase an ADS/stream colon.
    spaced = r"E:\card\folder with spaces\user document.txt"
    assert extract_authorized_paths("請讀取 " + spaced + " 然後驗證") == [spaced]
    ads = target + ":secret"
    assert ":secret" in extract_authorized_paths("請驗證 " + ads + " 進行 staging")[0]
    # Quoted paths are authoritative literals, not prose to truncate.
    quoted = r"E:\card\path 進行 staging\literal.txt"
    assert extract_authorized_paths("請檢查 \"" + quoted + "\" 然後回報") == [quoted]


def test_writer_schema_and_uncommitted_staging() -> None:
    with tempfile.TemporaryDirectory(prefix="it19-r006-canonical-") as tmp:
        root = Path(tmp).resolve()
        fixture = root / "fixture"
        fixture.mkdir()
        target = fixture / "open_transaction.txt"
        manager = ChunkedWriteManager(root / ".agents" / "chunked_writes" / "tasks" / "OFFLINE", [root])
        invalid = dict(tool="begin_file_write", action_id="bad-begin",
                       write_id="IT19-BAD", path=str(target), encoding="utf-8",
                       overwrite=True, chunk_count=1)
        try:
            manager.begin(invalid, request_id="RR-OFFLINE", run_id="RR-OFFLINE", turn_id=1)
        except ChunkedWriteError as exc:
            assert str(exc) == "incomplete_declared_expectations", exc
        else:
            raise AssertionError("partial declared expectations wrongly accepted")
        assert not manager.open_sessions()

        # Streaming mode has zero expected_* fields, and does NOT imply a commit.
        began = manager.begin(dict(tool="begin_file_write", action_id="good-begin",
                                   write_id="IT19-OPEN", path=str(target),
                                   encoding="utf-8", overwrite=False),
                              request_id="RR-OFFLINE", run_id="RR-OFFLINE", turn_id=2)
        assert began["state"] == "OPEN", began
        content = "IT19 staged-only, no commit.\n"
        chunk = manager.write_chunk(dict(tool="write_file_chunk", action_id="good-chunk",
                                         write_id="IT19-OPEN", content=content))
        assert chunk["state"] == "OPEN", chunk
        manifest = manager._load_manifest("IT19-OPEN")
        assert manifest["state"] == "OPEN"
        assert Path(manifest["staging_path"]).read_bytes() == content.encode()
        assert not target.exists(), "OPEN must not be claimed persisted"
        assert manager.open_sessions(request_id="RR-OFFLINE", run_id="RR-OFFLINE")


def test_nonexecuting_schema_repair_route() -> None:
    raw = "[CHUNKED_WRITE_FAILED] " + json.dumps({
        "error_code": "incomplete_declared_expectations",
        "_runtime_result": {
            "schema": "SMARTAGENT_TOOL_RESULT_V1",
            "tool": "begin_file_write",
            "status": "REJECTED",
            "reason": "incomplete_declared_expectations",
            "executed": False,
            "requested_path": "/isolated/test/file.txt",
            "resolved_path": None,
            "request_id": "RR-TEST",
            "task_id": "TASK-TEST",
            "action_id": "bad-begin",
            "data": {},
        },
    }, ensure_ascii=False)
    bad = {"tool": "begin_file_write", "action_id": "bad-begin",
           "write_id": "BAD", "path": "/isolated/test/file.txt"}
    history = [{"action": bad, "execution_status": "FAILED",
                "verification_status": "UNKNOWN", "result": raw}]
    result = build_action_loop_state(
        transport_state="ACCEPTED", progress={"decision": "CONTINUE"},
        action=bad, result=raw, execution_status="FAILED",
        verification_status="UNKNOWN", action_records=history,
        available_tools=["begin_file_write", "read_file", "list_directory", "repair_task_plan"],
    )
    assert result["required_transition"] == "REPLAN_REQUIRED", result
    assert "begin_file_write" in result["allowed_next_actions"], result
    assert "write_file_chunk" not in result["allowed_next_actions"], result
    assert result["blockers"][0]["release_kind"] == "NEW_SCHEMA_VALID_BEGIN_ACTION", result

    denied = build_action_loop_state(
        transport_state="ACCEPTED", progress={"decision": "CONTINUE"},
        action=bad, result=raw, execution_status="FAILED",
        verification_status="UNKNOWN", action_records=history,
        available_tools=["read_file", "list_directory"],
    )
    assert "begin_file_write" not in denied["allowed_next_actions"], denied

    # A potential side effect (executed=True/UNKNOWN) is never auto-repairable.
    unknown = json.loads(raw.split("] ", 1)[1])
    unknown["_runtime_result"].update(status="UNKNOWN", executed=True)
    unknown_raw = "[CHUNKED_WRITE_FAILED] " + json.dumps(unknown)
    suspected = [{"action": bad, "execution_status": "UNKNOWN",
                  "verification_status": "UNKNOWN", "result": unknown_raw}]
    result2 = build_action_loop_state(
        transport_state="ACCEPTED", progress={"decision": "CONTINUE"},
        action=bad, result=unknown_raw, execution_status="UNKNOWN",
        verification_status="UNKNOWN", action_records=suspected,
        available_tools=["begin_file_write", "read_file", "repair_task_plan"],
    )
    assert "begin_file_write" not in result2["allowed_next_actions"], result2


def main() -> None:
    test_request_scope()
    test_writer_schema_and_uncommitted_staging()
    test_nonexecuting_schema_repair_route()
    print("IT19_R006_ADMISSION_RECOVERY_OFFLINE_PASS")


if __name__ == "__main__":
    main()
