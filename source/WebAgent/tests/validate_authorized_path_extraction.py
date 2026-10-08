#!/usr/bin/env python3
"""Regression checks for request-scoped Windows path authorization parsing."""
from __future__ import annotations

from pathlib import Path

from agent_core.path_security import PathSecurityError, authorize_path, canonicalize_path
from agent_core.security_context import SecurityContext
from WebAgent.protocol_loop import extract_authorized_paths


def run() -> dict:
    root = Path(__file__).resolve().parents[3]
    plan = root / "doc" / "gemini_web_ui_end_to_end_plan.md"

    # Exact failure shape: two explicit paths separated by Chinese prose on
    # one logical request line must not be merged into one ADS-like value.
    request = (
        f"文件是{plan.name}完整的規劃\n"
        f"{plan} ，你先去修改這個路徑下的source code {root}\n"
        "完成後再執行單元測試"
    )
    extracted = extract_authorized_paths(request)
    assert extracted == [str(plan), str(root)], extracted
    assert authorize_path(extracted[0], [root], require_absolute=True) == plan.resolve()
    assert authorize_path(extracted[1], [root], require_absolute=True) == root.resolve()

    assert extract_authorized_paths(r"list C:\workspace\picture") == [r"C:\workspace\picture"]
    assert extract_authorized_paths(
        r'讀取 "C:\workspace with spaces\plan, v1.md"，再處理 C:\workspace\source'
    ) == [r"C:\workspace with spaces\plan, v1.md", r"C:\workspace\source"]
    assert extract_authorized_paths(
        r"比較 C:\first\one.txt ，再看 C:\second\two.txt。"
    ) == [r"C:\first\one.txt", r"C:\second\two.txt"]
    assert extract_authorized_paths(
        "讀取 C:\\first\\one.txt\n再看 C:\\second\\two.txt"
    ) == [r"C:\first\one.txt", r"C:\second\two.txt"]
    assert extract_authorized_paths(
        r"請讀取 E:\workspace\plan.md 然後接續上一個工作"
    ) == [r"E:\workspace\plan.md"]
    assert extract_authorized_paths(
        r"讀取 C:\workspace with spaces\plan.md 接著檢查結果"
    ) == [r"C:\workspace with spaces\plan.md"]
    assert extract_authorized_paths(
        r"列出D:\workspace\SmartAgentv2下的檔案有多少"
    ) == [r"D:\workspace\SmartAgentv2"]
    assert extract_authorized_paths(
        r"你先看C:\Users\ExampleUser\Desktop\picture裡面有多少檔案"
    ) == [r"C:\Users\ExampleUser\Desktop\picture"]

    # The parser must not hide a real ADS suffix from the security layer.
    ads_path = r"C:\workspace\file.txt:secret"
    ads_candidates = extract_authorized_paths(f"讀取 {ads_path}。")
    assert ads_candidates and ":" in ads_candidates[0][2:], ads_candidates
    try:
        canonicalize_path(ads_candidates[0], require_absolute=True)
    except PathSecurityError as exc:
        assert exc.code == "alternate_data_stream_forbidden"
    else:
        raise AssertionError("ADS path must remain rejected")

    # Sentence/quote parsing must never erase an ADS suffix and accidentally
    # grant request-scoped read authority to the base file.
    disguised_ads_requests = (
        '讀取 "C:\\workspace\\file.txt":secret',
        "讀取 C:\\workspace\\file，:secret。",
        '讀取 "C:\\workspace\\file.txt" ，:secret',
        "讀取 C:\\workspace\\file，suffix:secret。",
    )
    for malicious_request in disguised_ads_requests:
        candidates = extract_authorized_paths(malicious_request)
        assert candidates and ":" in candidates[0][2:], candidates
        try:
            SecurityContext.for_workspace(root, read_roots=tuple(candidates))
        except PathSecurityError as exc:
            assert exc.code == "alternate_data_stream_forbidden"
        else:
            raise AssertionError(f"disguised ADS path must fail closed: {candidates}")

    result = {
        "multi_path_request_split": True,
        "quoted_path_with_spaces": True,
        "sentence_boundaries": True,
        "chinese_continuation_boundary": True,
        "attached_chinese_prose_boundary": True,
        "ads_guard_preserved": True,
        "disguised_ads_guard_preserved": True,
    }
    print("AUTHORIZED_PATH_EXTRACTION_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
