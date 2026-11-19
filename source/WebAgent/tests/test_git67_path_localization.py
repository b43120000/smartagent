"""Harness regressions. Never executes a production task or live transport."""
import hashlib
import json
from pathlib import Path

from WebAgent.tests.sidecar_git67_path_localization import (
    BAD, GOOD, HEX_GOOD, Matrix, forms, historical, parser_case,
    path_in_text, telegram_storage, trace_probe,
)


def test_golden_input_is_programmatic_and_json_roundtrip_is_exact():
    assert GOOD.encode().hex(" ") == HEX_GOOD
    assert len(GOOD) == 26 and len(BAD) == 25
    assert [i for i, c in enumerate(GOOD) if ord(c) == 92] == [2, 10]
    assert json.loads(json.dumps(GOOD)) == GOOD
    assert GOOD[:10] + GOOD[11:] == BAD


def test_real_ingress_storage_preserves_all_six_forms(tmp_path):
    matrix = Matrix("TEST-INGRESS")
    for index, (name, text) in enumerate(forms(GOOD).items(), 1):
        root = tmp_path / name
        root.mkdir()
        expected = text if name == "E_JOIN_PATH" else GOOD
        prompt, binding = telegram_storage(
            matrix, name, text, name, expected, index, root)
        task = json.loads((root / "tasks.json").read_bytes())["tasks"][binding["task"]]
        assert task["request"] == task["metadata"]["source_user_command"] == text
        assert task["metadata"]["source_text"] == text
        assert text in prompt
        assert binding["message"] == 20000 + index
    assert all(row["result"] == "PASS" for row in matrix.rows if row["stage_name"] != "T7")
    assert all(row["result"] == "INCONCLUSIVE" for row in matrix.rows if row["stage_name"] == "T7")
    assert len({row["test_case_id"] for row in matrix.rows}) == 6


def test_real_parser_and_canonical_admission_capture_preserve_good_and_bad():
    matrix = Matrix("TEST-PARSER")
    parser_case(matrix, "GOOD", GOOD)
    parser_case(matrix, "BAD", BAD)
    assert len(matrix.rows) == 8
    assert all(row["input_path"] == row["output_path"] for row in matrix.rows)
    assert all(row["result"] == "PASS" for row in matrix.rows)


def test_missing_history_cannot_be_reported_as_reproduced(tmp_path):
    result = historical(Matrix("TEST-GAP"), tmp_path, tmp_path)
    assert result["A"]["status"] == "HISTORICAL_REPLAY_NOT_AVAILABLE"
    assert result["B"]["status"] == "HISTORICAL_REPLAY_NOT_AVAILABLE"


def test_it10_existing_recorder_observes_only_controlled_fixture(tmp_path):
    result = trace_probe(tmp_path)
    assert result["controlled_offline_probe"] == "PASS"
    assert result["live_T1_hook"] == "OBSERVATION_GAP"
    assert not result["production_settings_changed"]
    path = result["controlled_trace_sample"]["path_values"][0]["value"]
    assert path == GOOD
    assert hashlib.sha256(path.encode()).hexdigest() == (
        result["controlled_trace_sample"]["path_values"][0]["path_value_sha256"])


def test_unknown_live_evidence_remains_inconclusive():
    matrix = Matrix("TEST-LIVE-GAP")
    matrix.add("NO-LIVE", "T1_LIVE", GOOD, None, source={}, kind="LIVE")
    row = matrix.rows[0]
    assert row["result"] == "INCONCLUSIVE"
    assert row["output_sha256"] is None and row["path_hex"] is None
    assert row["first_mismatch_indicator"] is False


def test_bad_input_remains_bad_and_mismatch_is_not_hidden():
    matrix = Matrix("TEST-NEGATIVE")
    matrix.add("NEGATIVE", "T2", GOOD, BAD, source={})
    row = matrix.rows[0]
    assert row["result"] == "FAIL"
    assert row["input_sha256"] != row["output_sha256"]
    assert row["path_hex"] == BAD.encode().hex(" ")
    # A controlled counterexample is not claimed as a historical transition.
    assert not row["first_mismatch_indicator"]


def test_published_evidence_indexes_and_observation_boundaries():
    root = Path(__file__).resolve().parents[3] / "docs/evidence"
    indexes = list(root.glob("git67_path_localization_*/index.json"))
    assert indexes
    for path in indexes:
        index = json.loads(path.read_bytes())
        for entry in index["files"]:
            content = (path.parent / entry["file"]).read_bytes()
            assert len(content) == entry["bytes"]
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        rows = json.loads((path.parent / "matrix.json").read_bytes())["rows"]
        for row in rows:
            assert row["test_run_id"] == index["run_id"]
            if row["evidence_source"] == "LIVE":
                assert row["result"] == "INCONCLUSIVE"
            if row["output_path"] is not None:
                assert row["path_hex"] == row["output_path"].encode("utf-8").hex(" ")
        summary = json.loads((path.parent / "summary.json").read_bytes())
        assert summary["live_executed_count"] == summary["dispatch_count"] == 0
        assert summary["CONTINUATION_KEYERROR_REPRODUCTION"] == "REAL_HISTORICAL_LEDGER_REPRODUCED"
