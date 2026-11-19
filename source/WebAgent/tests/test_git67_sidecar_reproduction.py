"""Git67 sidecar asserts the pre-fix bug; never changes production modules."""
from __future__ import annotations

import copy
import json

import pytest

from WebAgent.tests.sidecar_git67_reproduction import (
    GOOD, BAD, HISTORICAL_REQUEST_SHA256, identity, synthetic_source,
    restore_once, path_fidelity, make_report,
)


@pytest.mark.parametrize("count", [2, 12])
def test_action_digest_missing_reproduces_actual_git67_restore_exception(tmp_path, count):
    source = synthetic_source(count, action_digest=False)
    before = copy.deepcopy(source)
    assert all("action_digest" not in row for row in source["results"].values())
    assert all(
        source["actions"][key].get("signature") for key in source["results"]
    )
    result = restore_once(source, tmp_path)
    assert result["status"] == "KEYERROR_ACTION_DIGEST_REPRODUCED", result
    assert result["exception_type"] == "KeyError"
    assert "_restore_continuation" in result["traceback"]
    assert "record['action_digest']" in result["traceback"]
    assert result["executed_tools"] == 0
    assert source == before
    assert result["source_ledger_unchanged"] is True


@pytest.mark.parametrize("count", [1, 2, 12])
def test_digest_present_control_restores_without_keyerror(tmp_path, count):
    result = restore_once(synthetic_source(count, action_digest=True), tmp_path)
    assert result["status"] == "RESTORED", result
    assert result["imported_action_count"] == count
    assert result["executed_tools"] == 0


@pytest.mark.parametrize("path", [GOOD, BAD])
def test_fake_telegram_transport_parser_path_is_byte_identical(path):
    result = path_fidelity(path)
    assert result["status"] == "PRESERVED", result
    assert result["parser_normalization"] == "NONE"
    assert result["live_telegram_tested"] is False
    assert result["live_chatgpt_prompt_tested"] is False
    assert all(v["sha256"] == identity(path)["sha256"]
               for v in result["stages"].values())


def test_one_exact_missing_backslash_and_bytes():
    assert GOOD.replace("\\\\.", ".") != BAD  # literal control: not double-backslash
    assert GOOD.replace("\\.", ".") == BAD
    assert GOOD.encode("utf-8") == bytes.fromhex(
        "45 3a 5c 41 49 73 70 61 63 65 5c 2e "
        "63 61 73 65 5f 64 5f 62 61 63 6b 75 70 73"
    )
    assert len(GOOD) == 26 and len(BAD) == 25
    assert identity(GOOD)["backslash_positions"] == [2, 10]
    assert identity(BAD)["backslash_positions"] == [2]


def test_optional_historical_files_must_match_reported_sha(tmp_path):
    from WebAgent.tests.sidecar_git67_reproduction import check_historical_request
    f = tmp_path / "rca_historical_request.txt"
    f.write_text("this is synthetic, not historical", encoding="utf-8")
    r = check_historical_request(f)
    assert r["status"] == "NOT_HISTORICAL_SHA_MATCH"
    assert r["sha256"] != HISTORICAL_REQUEST_SHA256


def test_report_marks_history_as_gap_without_actual_input(tmp_path):
    report = make_report(root=tmp_path)
    assert report["continuation"]["missing_digest_12"]["status"] == (
        "KEYERROR_ACTION_DIGEST_REPRODUCED"
    )
    assert report["continuation"]["complete_digest_control"]["status"] == "RESTORED"
    assert report["history"]["A"]["status"] == "OBSERVATION_GAP"
    assert report["history"]["B"]["status"] == "OBSERVATION_GAP"
    assert report["first_corruption_point"] == "UNKNOWN_INSUFFICIENT_EVIDENCE"
    assert report["changes_to_production_code"] is False
