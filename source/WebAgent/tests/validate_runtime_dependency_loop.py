#!/usr/bin/env python3
"""IT-R: real Runtime loop discovers, installs, verifies, and retries a dependency."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import uuid
import zipfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.json_state_io import write_json_atomic
from agent_core.runtime_environment import runtime_tool_paths
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_flexible_progress_admission import initial


TEMP_ROOT = APP_ROOT / "localdata" / "portable_git_tests"
STATE = APP_ROOT / "localdata" / "runtime_it" / "runtime_dependency_loop.json"


def _packet(progress: dict, action: dict) -> str:
    objects = [progress, action, {"tool": "turn_commit", "action_count": 2}]
    return "```smartagent_tool\n" + "\n".join(
        json.dumps(item, ensure_ascii=False) for item in objects
    ) + "\n```"


def _build_wheel(wheelhouse: Path, distribution: str, module: str) -> Path:
    wheelhouse.mkdir(parents=True)
    normalized = distribution.replace("-", "_")
    wheel = wheelhouse / f"{normalized}-1.0-py3-none-any.whl"
    dist_info = f"{normalized}-1.0.dist-info"
    files = {
        f"{module}.py": b"VALUE = 'SYNTHETIC_DEP_OK'\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {distribution}\nVersion: 1.0\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: SmartAgent IT-R\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode("utf-8"),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name in files:
        writer.writerow((name, "", ""))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


def _environment_from_prompt(prompt: str) -> dict:
    match = re.search(
        r"\[SMARTAGENT_ENVIRONMENT\]\n(.*?)\n\[/SMARTAGENT_ENVIRONMENT\]",
        prompt,
        re.DOTALL,
    )
    assert match, "initial planner environment block missing"
    return json.loads(match.group(1))


def _result(loop: WebAgentProtocolLoop, action_id: str) -> tuple[str, dict]:
    raw = str(loop.action_ledger[action_id]["result"])
    decoded = decode_result(raw)
    assert decoded, raw
    return raw, decoded


def run() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(TEMP_ROOT)
    os.environ["TMP"] = str(TEMP_ROOT)
    fixture = TEMP_ROOT / ("runtime dependency " + uuid.uuid4().hex[:12])
    fixture.mkdir()
    target = fixture / "deps"
    wheelhouse = fixture / "wheelhouse"
    suffix = uuid.uuid4().hex[:10]
    module = "smartagent_runtime_dep_" + suffix
    distribution = "smartagent-runtime-dep-" + suffix
    wheel = _build_wheel(wheelhouse, distribution, module)
    python_path = runtime_tool_paths(APP_ROOT)["python"].resolve()
    (fixture / "inspect_dependency.py").write_text(
        "import importlib, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'deps'))\n"
        "try:\n"
        f"    dependency = importlib.import_module('{module}')\n"
        "except ModuleNotFoundError:\n"
        "    print('DEPENDENCY_MISSING')\n"
        "else:\n"
        "    print('DEPENDENCY_PRESENT:' + dependency.VALUE)\n"
        "print('PYTHON_EXECUTABLE=' + sys.executable)\n",
        encoding="utf-8",
    )
    (fixture / "import_dependency.py").write_text(
        "import importlib, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'deps'))\n"
        f"dependency = importlib.import_module('{module}')\n"
        "print(dependency.VALUE)\n"
        "print('PYTHON_EXECUTABLE=' + sys.executable)\n",
        encoding="utf-8",
    )
    inspect_command = "python -I -B inspect_dependency.py"
    import_command = "python -I -B import_dependency.py"
    install_command = (
        "python -m pip install --no-index --no-deps --disable-pip-version-check "
        f"--target 'deps' '{wheel.relative_to(fixture)}'"
    )

    inspect = {
        "tool": "run_command", "action_id": "DEP-INSPECT-" + suffix,
        "operation": "INSPECT", "command": inspect_command,
        "working_directory": str(fixture),
    }
    mutate = {
        "tool": "run_command", "action_id": "DEP-INSTALL-" + suffix,
        "operation": "MUTATE", "command": install_command,
        "working_directory": str(fixture),
        "success_criteria": "The offline synthetic wheel is installed into the authorized fixture target.",
        "verify": [{"action": "file_exists", "path": f"deps/{module}.py", "expect": True}],
    }
    goal = "The previously missing synthetic dependency imports successfully and the original inspection is retried with a new action ID."
    verify = {
        "tool": "run_command", "action_id": "DEP-VERIFY-" + suffix,
        "operation": "VERIFY", "command": import_command,
        "working_directory": str(fixture), "verifies_action_id": mutate["action_id"],
        "condition_id": WebAgentProtocolLoop._canonical_condition_id(goal),
        "success_criteria": goal,
        "verify": [{
            "action": "run_command", "command": import_command,
            "expect_exit_code": 0, "expect_contains": "SYNTHETIC_DEP_OK",
        }],
    }
    retry = {
        **inspect,
        "action_id": "DEP-RETRY-" + suffix,
        "success_criteria": "The retried original inspection observes the installed dependency import.",
        "verify": [{
            "action": "run_command", "command": import_command,
            "expect_exit_code": 0, "expect_contains": "SYNTHETIC_DEP_OK",
        }],
    }
    progress = {
        **initial(), "action_id": "DEP-P1-" + suffix,
        "base_evaluation": "IT-R isolated missing dependency recovery",
        "next_action": "run_command",
        "completion_contract": {
            "success": [goal],
            "failure": ["Discovery, offline installation, import verification, or retry fails."],
            "in_progress": ["Dependency workflow still requires Runtime evidence."],
            "interrupted": ["The authorized Runtime cannot execute a required step."],
        },
        "matched_condition": "Dependency workflow still requires Runtime evidence.",
    }
    turns: list[str] = []
    observed_environment: dict = {}
    loop: WebAgentProtocolLoop | None = None

    def planner(prompt: str, _expected: dict, attachments: list[str]) -> str:
        assert not attachments
        turns.append(prompt)
        assert len(turns) <= 5, prompt[:12000]
        if len(turns) == 1:
            facts = _environment_from_prompt(prompt)
            observed_environment.update(facts)
            for tool in ("python", "git"):
                assert facts["tools"][tool]["status"] == "AVAILABLE", facts
                assert facts["tools"][tool]["probe_exit_code"] == 0, facts
            for policy in (
                "direct binary probes, not payload manifests",
                "INSPECT/GIT_INSPECT",
                "one prerequisite MUTATE action using bare python or git",
                "run an explicit VERIFY action, then retry the original operation with a new action_id",
                "Preserve denied, network, probe, and verification failures",
            ):
                assert policy in prompt, policy
            return _packet(progress, inspect)
        assert loop is not None
        if len(turns) == 2:
            assert inspect["action_id"] in loop.action_ledger, prompt[:12000]
            raw, row = _result(loop, inspect["action_id"])
            assert row["status"] == "SUCCEEDED"
            assert row["data"]["verification_status"] == "UNVERIFIED"
            assert "DEPENDENCY_MISSING" in raw and not target.exists(), raw
            executable = re.search(r"PYTHON_EXECUTABLE=([^\r\n]+)", raw)
            assert executable and Path(executable.group(1).strip()).resolve() == python_path
            return _packet({**progress, "action_id": "DEP-P2-" + suffix,
                "evidence_refs": [inspect["action_id"]],
                "decision_reason": "Real isolated import discovery proved the module is absent."}, mutate)
        if len(turns) == 3:
            raw, row = _result(loop, mutate["action_id"])
            assert row["status"] == "SUCCEEDED" and row["data"]["verification_status"] == "PASS", raw
            assert (target / f"{module}.py").is_file()
            assert verify["action_id"] not in loop.action_ledger
            return _packet({**progress, "action_id": "DEP-P3-" + suffix,
                "evidence_refs": [inspect["action_id"], mutate["action_id"]],
                "decision_reason": "Offline install completed with file evidence; explicit import verification remains."}, verify)
        if len(turns) == 4:
            raw, row = _result(loop, verify["action_id"])
            assert row["status"] == "SUCCEEDED" and row["data"]["verification_status"] == "PASS", raw
            assert "SYNTHETIC_DEP_OK" in raw
            assert retry["action_id"] not in loop.action_ledger
            return _packet({**progress, "action_id": "DEP-P4-" + suffix,
                "evidence_refs": [mutate["action_id"], verify["action_id"]],
                "decision_reason": "Linked VERIFY proved the import; retry the original operation under a new ID."}, retry)
        raw, row = _result(loop, retry["action_id"])
        assert retry["action_id"] != inspect["action_id"]
        assert row["status"] == "SUCCEEDED" and row["data"]["verification_status"] == "PASS", raw
        assert "DEPENDENCY_PRESENT:SYNTHETIC_DEP_OK" in raw, raw
        refs = [inspect["action_id"], mutate["action_id"], verify["action_id"], retry["action_id"]]
        assert loop._terminal_evidence_verdict(refs, matched_condition=goal) == "PASS"
        done = {
            **progress, "action_id": "DEP-P5-" + suffix,
            "decision": "COMPLETE", "outcome": "SUCCESS", "current_step": 1,
            "steps": [{"step": 1, "desc": "Runtime dependency recovery", "status": "COMPLETED"}],
            "matched_condition": goal, "evidence_refs": refs,
            "decision_reason": "Discovery, offline install, linked import verification, and fresh-ID retry all passed.",
            "next_action": "final_response",
        }
        return _packet(done, {
            "tool": "final_response", "action_id": "DEP-FINAL-" + suffix,
            "content": "RUNTIME_DEPENDENCY_LOOP_PASS",
        })

    state = {"schema": "SMARTAGENT_RUNTIME_DEPENDENCY_IT_R_V1", "fixture": str(fixture), "phases": {}}
    try:
        context = WebAgentToolContext(fixture, interface_name="remote")
        loop = WebAgentProtocolLoop(fixture, planner, tool_context=context, progress_root=APP_ROOT)
        output = loop.run(
            "IT-R only: discover the missing synthetic dependency locally, install only the supplied offline wheel "
            "into the authorized fixture, explicitly verify its import, then retry the original operation with a new ID.\n"
            + str(fixture)
        )
        assert output == "RUNTIME_DEPENDENCY_LOOP_PASS", output
        rows = {}
        for action in (inspect, mutate, verify, retry):
            raw, decoded = _result(loop, action["action_id"])
            rows[action["action_id"]] = {
                "status": decoded["status"],
                "verification_status": decoded["data"].get("verification_status"),
                "result_excerpt": raw[:2000],
            }
        state["phases"]["IT-R"] = {
            "status": "PASS", "rounds": len(turns), "actions": rows,
            "python": str(python_path), "wheel": str(wheel),
            "planner_environment": observed_environment,
            "notes": "Scripted planner; real WebAgentProtocolLoop, tools, ledger, security, pip process, files, and imports. No browser/model claim.",
        }
    except Exception as exc:
        state["phases"]["IT-R"] = {
            "status": "FAIL", "rounds": len(turns), "error": repr(exc)[:4000],
            "actions": dict(getattr(loop, "action_ledger", {}) or {}),
        }
        write_json_atomic(STATE, state)
        raise
    write_json_atomic(STATE, state)
    print("RUNTIME_DEPENDENCY_LOOP_IT_R_PASS", fixture, "rounds=" + str(len(turns)), flush=True)


if __name__ == "__main__":
    run()
