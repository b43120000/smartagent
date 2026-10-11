"""Regression for request prose scopes, automatic chunk offsets and failure evidence."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from WebAgent.protocol import render_action_response_constraints
from WebAgent.protocol_loop import WebAgentProtocolLoop, extract_authorized_paths
from WebAgent.tool_context import WebAgentToolContext
from agent_core.chunked_write import ChunkedWriteError, ChunkedWriteManager


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        directory = root / "runtime_it"
        directory.mkdir()
        request = f"列出 {directory} 第一層內容\n在 {directory} 建立 batch_large_probe.txt"
        paths = extract_authorized_paths(request)
        assert paths == [str(directory)], paths
        context = WebAgentToolContext(root)
        context.begin_run("scope-chunks", request, paths)
        exact = context.preview_tool_scope({"tool": "list_directory", "path": str(directory)})
        assert exact["allowed"] and exact["resolved_paths"] == [str(directory)], exact
        broader = context.preview_tool_scope({"tool": "list_directory", "path": str(root)})
        assert not broader["allowed"], broader
        literal = directory / "Team 建立 專案"
        literal.mkdir()
        assert extract_authorized_paths(f"列出 {literal}") == [str(literal)]

        manager = ChunkedWriteManager(root / "transactions", [directory])
        context._chunked_write_manager = manager
        destination = directory / "probe.txt"
        chunks = ["B" * 990, "B" * 985, "B" * 1000, "B" * 1000, "B" * 1000, "B" * 25]
        expected = b"B" * 5000
        manager.begin({"action_id": "begin", "write_id": "probe", "path": str(destination),
                       "encoding": "utf-8", "overwrite": False, "expected_size": 5000,
                       "expected_sha256": hashlib.sha256(expected).hexdigest(), "chunk_count": len(chunks)})
        first = manager.write_chunk({"action_id": "first", "write_id": "probe", "content": chunks[0]})
        assert first["next_offset"] == 990 and first["accepted_bytes"] == 990
        rejected = context.execute({"tool": "write_file_chunk", "action_id": "bad-offset",
                                    "write_id": "probe", "offset": 980, "content": chunks[1]})
        failure = json.loads(rejected.removeprefix("[CHUNKED_WRITE_FAILED] "))
        assert failure["error_code"] == "chunk_overlap", failure
        assert failure["detail"] == {"expected_offset": 990, "received_offset": 980}
        assert failure["data"]["next_offset"] == 990
        assert failure["data"]["remaining_bytes"] == 4010
        evidence = WebAgentProtocolLoop._classify_action_evidence({"tool": "write_file_chunk"}, rejected)
        assert evidence["execution_status"] == "FAILED" and not evidence["terminal_eligible"], evidence
        gap_result = context.execute({"tool": "write_file_chunk", "action_id": "bad-gap",
                                      "write_id": "probe", "offset": 998, "content": chunks[1]})
        gap = json.loads(gap_result.removeprefix("[CHUNKED_WRITE_FAILED] "))
        assert gap["error_code"] == "chunk_gap" and gap["data"]["received_bytes"] == 990
        assert gap["detail"] == {"expected_offset": 990, "received_offset": 998}
        for number, chunk in enumerate(chunks[1:], 1):
            action = {"action_id": f"chunk-{number}", "write_id": "probe", "content": chunk}
            accepted = manager.write_chunk(action)
            replay = manager.write_chunk(action)
            assert replay["replay"] and replay["next_offset"] == accepted["next_offset"]
        result = manager.commit({"action_id": "commit", "write_id": "probe"})
        assert destination.read_bytes() == expected and result["remaining_bytes"] == 0

        # Exact byte boundaries, not characters, including multibyte Unicode.
        manager.begin({"action_id": "unicode-begin", "write_id": "unicode",
                       "path": str(directory / "unicode.txt"), "encoding": "utf-8", "overwrite": False})
        result = manager.write_chunk({"action_id": "unicode-1", "write_id": "unicode", "content": "圖"})
        assert result["next_offset"] == 3
        result = manager.write_chunk({"action_id": "unicode-2", "write_id": "unicode", "content": "B"})
        assert result["next_offset"] == 4
        result = manager.commit({"action_id": "unicode-commit", "write_id": "unicode"})
        assert result["expected_size"] == 4
        assert (directory / "unicode.txt").read_text(encoding="utf-8") == "圖B"

        for name, size, digest, content, code in (
            ("missing", 2, hashlib.sha256(b"BB").hexdigest(), "B", "missing_chunks"),
            ("size", 2, hashlib.sha256(b"BB").hexdigest(), "B", "assembled_size_mismatch"),
            ("hash", 1, "0" * 64, "B", "assembled_sha256_mismatch"),
        ):
            output = directory / (name + ".txt")
            manager.begin({"action_id": name + "-begin", "write_id": name, "path": str(output),
                           "encoding": "utf-8", "overwrite": False, "expected_size": size,
                           "expected_sha256": digest, "chunk_count": 2 if name == "missing" else 1})
            manager.write_chunk({"action_id": name + "-chunk", "write_id": name, "content": content})
            try:
                manager.commit({"action_id": name + "-commit", "write_id": name})
            except ChunkedWriteError as error:
                assert str(error) == code, error
            else:
                raise AssertionError("invalid transaction committed")
            assert not output.exists()

    for tool, text in (
        ("write_file_chunk", "[CHUNKED_WRITE_FAILED] chunk_gap"),
        ("read_file", "[Web Planner]\n[WEBAGENT_ATTACHMENT_ERROR] FileNotFoundError"),
        ("commit_file_write", '{"execution_status":"FAILED","error_code":"missing_chunks","data":{}}'),
    ):
        evidence = WebAgentProtocolLoop._classify_action_evidence({"tool": tool}, text)
        assert evidence["execution_status"] == "FAILED" and not evidence["terminal_eligible"], evidence
    good = WebAgentProtocolLoop._classify_action_evidence(
        {"tool": "list_directory"}, "probe.txt")
    assert good["execution_status"] == "SUCCEEDED"
    successful = "[CHUNKED_WRITE_SUCCESS] " + json.dumps({
        "execution_status": "SUCCEEDED", "status": "success",
        "destination_path": r"E:\safe\[CHUNKED_WRITE_FAILED]\probe.txt",
    })
    assert WebAgentProtocolLoop._classify_action_evidence(
        {"tool": "commit_file_write"}, successful)["execution_status"] == "SUCCEEDED"
    assert "省略 chunk_index、offset" in render_action_response_constraints()
    print("CHUNK_SCOPE_RESULT_CONTRACT_PASS")


if __name__ == "__main__":
    main()
