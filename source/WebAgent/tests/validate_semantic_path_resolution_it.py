#!/usr/bin/env python3
"""SPR V1: true Windows paths, prose-boundary variation, safety and epochs."""
from __future__ import annotations

import tempfile
from pathlib import Path

from WebAgent.protocol_loop import extract_authorized_paths, resolve_authorized_paths
from agent_core.semantic_path_resolution import (
    SCHEMA, SemanticPathError, analyze_request_paths,
    persist_request_path_resolution, require_resolved_paths,
)


def check(request, path, *, readonly=True):
    lexical = extract_authorized_paths(request)
    doc = analyze_request_paths(request, lexical, allow_readonly_narrowing=readonly)
    assert require_resolved_paths(doc) == [str(path)], (request, doc)
    assert resolve_authorized_paths(request) == [str(path)], (request, doc)
    return doc


def run():
    with tempfile.TemporaryDirectory(prefix="spr-v1-") as temp:
        root = Path(temp).resolve()
        project = root / "My Project"
        project.mkdir()
        leaf = project / "Camera Test Data"
        leaf.mkdir()
        deps = project / "deps"
        deps.mkdir()
        (deps / "one.txt").write_text("one", encoding="utf-8")
        # Prefix existence must not truncate an actual path containing spaces.
        (project / "Camera").mkdir()
        check("請分析 " + str(leaf), leaf)
        check("請分析 " + str(leaf) + " 的資料夾狀況。", leaf)
        check("請分析 " + str(leaf) + "的資料夾狀況。", leaf)
        check("請分析 " + str(leaf) + "\n的資料夾狀況。", leaf)
        check("請分析 " + str(deps) + " 的資料夾狀況。", deps)
        doc = check("請分析 " + str(deps) + "的資料夾狀況。", deps)
        assert doc["status"] == "VERIFIED_UNIQUE", doc
        # Real L06 structure: negative write restrictions after the path
        # are NOT positive install, delete, or command-execution intent.
        check(
            "請只針對 " + str(deps) +
            " 進行資料盤點。\\n需要內容與數量統計。"
            "不允許執行任何寫入、安裝、刪除或修改操作。",
            deps,
        )
        from agent_core.semantic_path_resolution import readonly_narrowing_allowed
        creation = "請建立 " + str(deps) + " 的資料夾。"
        assert not readonly_narrowing_allowed(creation, extract_authorized_paths(creation))

        assert sum(item["existence"] == "ABSENT"
                   for row in doc["records"] for item in row["candidates"]) > 0
        assert doc["grants_authorization"] is False
        check('請分析 "' + str(leaf) + '" 的資料夾狀況。', leaf)
        check("讀取 " + str(deps) + "。", deps)
        many = "請分析 " + str(leaf) + " 的內容，並看 " + str(deps) + " 的狀況"
        result = resolve_authorized_paths(many)
        assert result == [str(leaf), str(deps)], result

        # True ambiguity: both names exist; neither is silently authorized.
        ambiguous_name = project / "deps 的資料夾狀況"
        ambiguous_name.mkdir()
        ambiguous_request = "請分析 " + str(deps) + " 的資料夾狀況。"
        d = analyze_request_paths(ambiguous_request, extract_authorized_paths(ambiguous_request),
                                  allow_readonly_narrowing=True)
        assert d["status"] == "AMBIGUOUS", d
        try:
            resolve_authorized_paths(ambiguous_request)
        except SemanticPathError as exc:
            assert str(exc) == "semantic_path_resolution_ambiguous", str(exc)
        else:
            raise AssertionError("Ambiguous path was admitted")

        # Independent non-existent target remains the target. No parent fallback.
        absent = project / "no_such_folder"
        assert resolve_authorized_paths("請確認 " + str(absent) + "。") == [str(absent)]

        # Quotes preserve even a whitespace-bearing *nonexistent* path.
        quoted_absent = project / "new report folder"
        q = '請確認 "' + str(quoted_absent) + '"'
        assert resolve_authorized_paths(q) == [str(quoted_absent)]
        # ADS suffix must not be erased to reach the existing file.
        file = deps / "one.txt"
        ads = str(file) + ":secret"
        assert resolve_authorized_paths("請分析 " + ads + "的狀況。") != [str(file)]

        # Trusted provenance is immutable (saved plan and authenticated scope).
        user_prose = "請分析 " + str(deps) + "的資料夾狀況。"
        assert resolve_authorized_paths(
            user_prose, trusted_authorized_paths=[str(project)]) == [str(project)]

        # Preflight ledger is idempotent for one original request/epoch.
        path = persist_request_path_resolution(
            root, request_id="RR-SPR-1", task_epoch="EPOCH-1",
            original_request=user_prose, document=doc)
        assert path.exists()
        assert persist_request_path_resolution(
            root, request_id="RR-SPR-1", task_epoch="EPOCH-1",
            original_request=user_prose, document=doc) == path
        try:
            persist_request_path_resolution(
                root, request_id="RR-SPR-1", task_epoch="EPOCH-2",
                original_request=user_prose, document=doc)
        except SemanticPathError as exc:
            assert str(exc) == "semantic_path_ledger_identity_mismatch", str(exc)
        else:
            raise AssertionError("Cross-epoch path ledger reused")
        assert SCHEMA in path.read_text(encoding="utf-8")
    print("SEMANTIC_PATH_RESOLUTION_IT_PASS")


if __name__ == "__main__":
    run()
