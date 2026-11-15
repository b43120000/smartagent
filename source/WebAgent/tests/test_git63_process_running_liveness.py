"""Git63 shared RUNNING semantics for ordinary commands and tool installers.

OS liveness observations are transient; a PID alone or the lack of stdout is
not permission to claim completion or keep a stale RUNNING status forever.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from agent_core.process_liveness import (
    SCHEMA, communicate_with_liveness, observe_owned_process, wait_with_liveness,
)
from agent_core.tool_result_contract import decode_result
from agent_core import tools as tool_runtime
from agent_core import execution_telemetry as telemetry


def test_silent_owned_child_remains_running_until_it_exits():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.32)"])
    observations = []
    try:
        final = wait_with_liveness(
            proc, timeout=3, on_running=observations.append,
            execution_id="EXEC-SILENT", interval=0.04,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert observations
    assert all(e["schema"] == SCHEMA for e in observations)
    assert all(e["liveness_verified"] and e["process_identity_verified"]
               for e in observations)
    assert all(e["process_state"] == "RUNNING" for e in observations)
    assert all(e["pid"] == proc.pid and e["execution_id"] == "EXEC-SILENT"
               for e in observations)
    assert final["process_state"] == "EXITED"
    assert not final["liveness_verified"]
    assert final["exit_code"] == 0


def test_wait_timeout_is_not_process_exit():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            wait_with_liveness(proc, timeout=0.12, interval=0.03)
        still_running = observe_owned_process(proc)
        assert still_running["process_state"] == "RUNNING"
        assert still_running["liveness_verified"] is True
        assert still_running["exit_code"] is None
    finally:
        proc.kill()
        proc.wait()
    final = observe_owned_process(proc)
    assert final["process_state"] == "EXITED"
    assert not final["liveness_verified"]


def test_communicate_drains_both_pipes_with_silent_interval():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; time.sleep(.15); print('complete'); print('warn',file=sys.stderr)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    observations = []
    stdout, stderr = communicate_with_liveness(
        proc, timeout=4, on_running=observations.append, interval=0.03,
    )
    assert b"complete" in stdout
    assert b"warn" in stderr
    assert proc.returncode == 0
    assert observations and all(o["liveness_verified"] for o in observations)


def test_running_snapshot_requires_fresh_kernel_observation(tmp_path):
    evidence = {
        "schema": SCHEMA, "execution_id": "EXEC-T1",
        "process_state": "RUNNING", "liveness_verified": True,
        "process_identity_verified": True, "pid": 4000,
        "evidence_source": "OWNED_POPEN_WAITPID",
        "observed_at": time.time() - 60,
    }
    telemetry.write_snapshot(tmp_path, "TASK-T1", state="RUNNING",
                             pid=4000, liveness_evidence=evidence)
    stale = telemetry.refresh_snapshot(tmp_path, "TASK-T1")
    assert stale["state"] == "UNKNOWN"
    assert stale["liveness_verified"] is False
    assert stale["liveness_uncertain_reason"] == "OS_OBSERVATION_EXPIRED"

    evidence["observed_at"] = time.time()
    telemetry.write_snapshot(tmp_path, "TASK-T1", state="RUNNING",
                             pid=4000, liveness_evidence=evidence)
    fresh = telemetry.refresh_snapshot(tmp_path, "TASK-T1")
    assert fresh["state"] == "RUNNING"
    assert fresh["liveness_freshness"] == "FRESH"


def _mock_command(monkeypatch, tmp_path, result):
    monkeypatch.setattr(
        tool_runtime, "resolve_command_working_directory",
        lambda *args, **kwargs: tmp_path,
    )
    monkeypatch.setattr(
        tool_runtime, "_run_powershell_capture",
        lambda *args, **kwargs: result,
    )
    return decode_result(tool_runtime.tool_run_command(
        "buildingapk.bat", "BUILD", working_directory=tmp_path,
    ))


def test_unknown_exit_never_fabricates_failed_build(monkeypatch, tmp_path):
    result = _mock_command(monkeypatch, tmp_path, {
        "exit_code": None, "process_started": True,
        "process_state": "UNKNOWN",
        "error": "controller reply missing",
    })
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "COMMAND_OUTCOME_UNKNOWN"
    assert result["data"]["verification_status"] == "UNVERIFIED"
    assert result["data"]["process_state"] == "UNKNOWN"
    assert result["data"]["terminal_eligible"] is False


def test_planner_running_requires_verified_handle_not_pid(monkeypatch, tmp_path):
    without_proof = _mock_command(monkeypatch, tmp_path, {
        "exit_code": None, "process_started": True,
        "process_state": "RUNNING", "pid": 1234,
        "liveness_evidence": {"liveness_verified": False},
    })
    assert without_proof["status"] == "UNKNOWN"
    proof = {
        "schema": SCHEMA, "process_state": "RUNNING",
        "liveness_verified": True, "process_identity_verified": True,
        "evidence_source": "WIN32_WAIT_FOR_SINGLE_OBJECT",
        "observed_at": time.time(), "pid": 1234,
    }
    running = _mock_command(monkeypatch, tmp_path, {
        "exit_code": None, "process_started": True,
        "process_state": "RUNNING", "pid": 1234,
        "liveness_evidence": proof,
    })
    assert running["status"] == "RUNNING"
    assert running["reason"] == "COMMAND_RUNNING_OS_VERIFIED"
    assert running["data"]["terminal_eligible"] is False


def test_running_telemetry_does_not_extend_command_timeout():
    # OS proof is a progress observation, not authority to detach or extend
    # a user-specified timeout or to keep a Native AppContainer Job alive.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            wait_with_liveness(proc, timeout=.11, interval=.02)
        assert time.monotonic() - started < 1.0
    finally:
        proc.kill()
        proc.wait()
