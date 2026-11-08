"""Real Windows RCA collector: request-bound minimal parser diagnostics only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from WebAgent.minimal_core_loop import MinimalCoreJournal
from WebAgent.minimal_decision import MinimalDecisionError, parse_minimal_decision


@pytest.mark.skipif(os.name != "nt", reason="Windows BAT and PowerShell required")
def test_masked_minimal_parse_failure_is_present_in_rca(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    shutil.copyfile(repo / "collect.bat", tmp_path / "collect.bat")
    stub = tmp_path / "source" / "WebAgent" / "protocol_loop.py"
    stub.parent.mkdir(parents=True)
    stub.write_text("# synthetic collector fixture\n", encoding="utf-8")

    task, request = "TASK-MINIMAL-RCA", "RR-MINIMAL-RCA"
    ledger = tmp_path / "localdata" / "runtime" / "task_progress" / (task + ".json")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "task_id": task, "request_id": request,
        "runtime_state": "INTERRUPTED", "current_step": 0,
        "total_steps": 0, "steps": [],
    }), encoding="utf-8")

    # This text is deliberately invalid JSON. Neither the tool nor the path
    # should escape the sanitized RCA (only syntax, hash and location survive).
    secret = "PRIVATE_FOLDER_NAME_DO_NOT_EXPORT"
    raw = ('{"type":"tool_call","name":"create_directory",'
           '"arguments":{"path":"E:\\' + secret + '"}}')
    with pytest.raises(MinimalDecisionError) as exc:
        parse_minimal_decision(raw)
    diag = exc.value.diagnostic
    journal = MinimalCoreJournal(tmp_path, task)
    journal.append("task_started", task_id=task, request_id=request,
                   task_epoch="fixture", goal_sha256="a" * 64)
    journal.append("decision_rejected", round=1,
                   reason=exc.value.code,
                   response_sha256=diag["response_sha256"],
                   response_structure_redacted=diag["response_structure_redacted"])

    logs = tmp_path / "localdata" / "logs"
    logs.mkdir(parents=True)
    row = {
        "event": "STATUS", "stage": "minimal_decision_rejected",
        "task_id": task, "request_id": request,
        "timestamp": 1794127012.0, "round": 1,
        "diagnostic_reason": exc.value.code,
        "response_sha256": diag["response_sha256"],
        "response_chars": diag["response_chars"],
        "candidate_count": diag["candidate_count"],
        "valid_decision_count": diag["valid_decision_count"],
        "source_kind": "DIRECT_JSON", "normalization": "NONE",
        "json_error_line": diag["json_error_line"],
        "json_error_column": diag["json_error_column"],
        "json_error_message_code": diag["json_error_message_code"],
    }
    other = {**row, "request_id": "RR-OTHER", "response_sha256": "0" * 64}
    (logs / "remote_runtime.jsonl").write_text(
        "\n".join(json.dumps(v) for v in (row, other)), encoding="utf-8")

    outcome = subprocess.run(
        ["cmd.exe", "/d", "/c", str(tmp_path / "collect.bat"), task, request],
        cwd=tmp_path, capture_output=True, text=True, timeout=90,
    )
    assert outcome.returncode == 0, outcome.stdout[-2500:] + outcome.stderr[-2500:]
    files = list((tmp_path / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
    assert len(files) == 1
    with zipfile.ZipFile(files[0]) as bundle:
        trace = json.loads(bundle.read("minimal_core_decision_diagnostics.json"))
        shapes = json.loads(bundle.read("minimal_core_masked_structure.json"))
        assert len(trace) == 1
        assert trace[0]["diagnostic_reason"] == "json_invalid_escape"
        assert trace[0]["json_error_column"] > 0
        assert trace[0]["response_sha256"] == hashlib.sha256(
            raw.encode("utf-8")).hexdigest()
        assert len(shapes) == 1
        assert shapes[0]["shape_is_sanitized"] is True
        assert "\\" in shapes[0]["response_structure_redacted"]
        assert secret not in json.dumps(trace) + json.dumps(shapes)
        assert "RR-OTHER" not in json.dumps(trace) + json.dumps(shapes)
