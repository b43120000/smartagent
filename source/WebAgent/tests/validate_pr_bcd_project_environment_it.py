"""Real offline Windows PR-B/C/D + Project Environment Persistence IT.

Runs in an isolated temp project and uses a locally generated wheel only.
No network and no edits to the shared SmartAgent .venv.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from agent_core.paths import install_root
from agent_core.project_environment import (
    environment_index_path, publish_verified_dependency, publish_verified_tool,
    planner_environment_hint, read_project_environment, resolve_install_target,
)
from agent_core.prerequisite_runtime_v3 import (
    RecoveryLedger, PrerequisiteRecoveryError, _probe_project_package,
    probe_requirements,
)
from agent_core.runtime_environment import runtime_tool_paths
from WebAgent.tests.prepare_pr_bcd_live_fixture import prepare


def _run_python(python: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), *args],
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )


def _action(action_id: str, operation: str, **extra) -> dict:
    return {"tool": "run_command", "operation": operation, "action_id": action_id, **extra}


def _result(ledger, ids, action, execution: subprocess.CompletedProcess, evidence: str):
    return ledger.action_result(
        **ids, action=action, evidence_ref=evidence,
        result={
            "executed": True,
            "result_status": "SUCCEEDED" if execution.returncode == 0 else "FAILED",
            "effective_verification_status": "PASS" if execution.returncode == 0 else "FAIL",
        },
    )


def run() -> None:
    app = install_root()
    bundled = runtime_tool_paths(app)["python"]
    assert bundled.is_file(), f"Bundled Python unavailable: {bundled}"
    with tempfile.TemporaryDirectory(prefix="sa-prbcd-project-env-") as td:
        root = Path(td)
        project = root / "SmartAgent_PR_BCD_LIVE"
        project.mkdir()
        prepare(str(project))
        target = project / "deps"
        package_request = "package smartagent-pr-bcd-live>=1.0"
        environment = {"tools": {
            "python": {"status": "AVAILABLE", "version": "Python 3.11"},
            "git": {"status": "AVAILABLE", "version": "git version 2.43"},
        }}
        index = environment_index_path(project)
        # Read-only PR-B must not write the project environment index.
        a = probe_requirements(
            request="Only inspect " + package_request + ", do not install",
            authorized_paths=[str(project)],
            environment_snapshot=environment, install_root=app,
            project_root=project,
        )
        facts = {f["id"]: f for f in a["facts"]}
        package = facts["package:smartagent-pr-bcd-live"]
        assert package["state"] == "MISSING", package
        assert not a["remediation_requested"], a
        assert not index.exists(), "Read-only inspection wrote the project index"

        # Reuse the same task scope, but do not grant permissions based on the
        # probe itself. Explicit installer request is required.
        b = probe_requirements(
            request="Please install " + package_request + " from local offline wheel",
            authorized_paths=[str(project)],
            environment_snapshot=environment, install_root=app,
            project_root=project,
        )
        assert b["remediation_requested"] and b["remediation_eligible"], b
        ids = dict(task_id="TASK-PR-BCD-IT", request_id="RR-PR-BCD-IT", task_epoch="EPOCH-IT")
        ledger = RecoveryLedger(root)
        state = ledger.initialize(
            **ids, intent_sha256="a"*64, probe=b, graph_digest="b"*64
        )
        assert state["phase"] == "MISSING"
        install_cmd = (
            "python -m pip install --no-index --no-deps --disable-pip-version-check "
            "--target deps wheelhouse/smartagent_pr_bcd_live-1.0-py3-none-any.whl"
        )
        mutate = _action("MUTATE-INSTALL", "MUTATE", command=install_cmd)
        assert ledger.before_action(**ids, action=mutate, scope_allowed=True)["phase"] == "REMEDIATING"
        # Only an actual locally staged wheel is installed.
        installed = _run_python(
            bundled, project, "-m", "pip", "install", "--no-index", "--no-deps",
            "--disable-pip-version-check", "--no-cache-dir", "--target", "deps",
            "wheelhouse/smartagent_pr_bcd_live-1.0-py3-none-any.whl",
        )
        assert installed.returncode == 0, installed.stderr
        assert _result(ledger, ids, mutate, installed, "EVIDENCE-INSTALL")["phase"] == "REMEDIATED_PENDING_VERIFY"
        verify = _action("VERIFY-INDEPENDENT", "VERIFY", verifies_action_id="MUTATE-INSTALL")
        verified = _run_python(bundled, project, "-I", "-B", "verify_dependency.py")
        assert verified.returncode == 0 and "PR_BCD_VERIFY_PASS" in verified.stdout, (verified.stdout, verified.stderr)
        assert _result(ledger, ids, verify, verified, "EVIDENCE-VERIFY")["phase"] == "VERIFIED"
        resume = _action("INSPECT-RESUME", "INSPECT", depends_on=["VERIFY-INDEPENDENT"])
        retried = _run_python(bundled, project, "-I", "-B", "inspect_dependency.py")
        assert retried.returncode == 0 and "PR_BCD_DEPENDENCY_PRESENT" in retried.stdout, (retried.stdout, retried.stderr)
        assert _result(ledger, ids, resume, retried, "EVIDENCE-RESUME")["phase"] == "RESUMED"
        assert ledger.terminal_ready(ids["task_id"])[0]

        # Persistence uses a separately authorized project write, never PR-B.
        status, version = _probe_project_package("smartagent-pr-bcd-live", bundled, str(target))
        assert status == "AVAILABLE" and version == "1.0", (status, version)
        saved = publish_verified_dependency(
            project, distribution="smartagent-pr-bcd-live", version=version,
            target=target, request_id=ids["request_id"], action_id=resume["action_id"],
            evidence_ref="EVIDENCE-RESUME", authorized_project_write=True,
        )
        assert saved["status"] == "VERIFIED" and index.is_file()
        assert Path(read_project_environment(project)["dependencies"]["smartagent-pr-bcd-live"]["location"]).samefile(target)
        # SDK/toolchain can be outside the project, but merely recording its
        # path never grants execution permission to any subsequent task.
        tool = publish_verified_tool(
            project, name="python", path=bundled, version="3.11",
            request_id=ids["request_id"], evidence_ref="EVIDENCE-PYTHON",
            authorized_project_write=True,
        )
        assert Path(tool["path"]).samefile(bundled)
        hints = planner_environment_hint(project)
        assert any(row["name"] == "python" for row in hints["tools"])
        assert hints["authority"] == "CANDIDATES_REQUIRING_RUNTIME_REPROBE"
        try:
            publish_verified_tool(
                project, name="untrusted", path=bundled, version="3.11",
                request_id="RR-READONLY", evidence_ref="NONE",
                authorized_project_write=False,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("Read-only task modified environment index")

        reuse = probe_requirements(
            request="Only inspect " + package_request,
            authorized_paths=[str(project)], environment_snapshot=environment,
            install_root=app, project_root=project,
        )
        reused = next(x for x in reuse["facts"] if x["id"] == "package:smartagent-pr-bcd-live")
        assert reused["state"] == "SATISFIED" and reused["source"] == "PROJECT_ENVIRONMENT_REPROBED", reused
        assert not reuse["remediation_requested"]

        # Safe refusal: no default install target without explicit .venv write
        # approval, and an explicit target cannot escape the project.
        try:
            resolve_install_target(project, shared_venv=app / ".venv")
        except PermissionError:
            pass
        else:
            raise AssertionError("Shared venv was authorized without consent")
        try:
            resolve_install_target(project, explicit_target=root / "outside")
        except PermissionError:
            pass
        else:
            raise AssertionError("Escaping the project was authorized")
        assert resolve_install_target(project, explicit_target="deps").samefile(target)

    print("PR_BCD_PROJECT_ENVIRONMENT_OFFLINE_IT_PASS")


if __name__ == "__main__":
    run()
