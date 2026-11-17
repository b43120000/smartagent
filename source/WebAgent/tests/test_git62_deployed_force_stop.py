"""Git62: deployed installation (no .git) and Force Stop root binding regressions.

All fixtures live in pytest tmp_path. No live Agent, UAC or ProgramData touched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core import force_stop_all_agents as stop


def _git(*args, cwd):
    return subprocess.check_output(
        ["git", *args], cwd=cwd, stderr=subprocess.PIPE,
    ).decode("utf-8").strip()


@pytest.fixture
def deployed_repo(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git binary required for checkout provenance regression")
    source = tmp_path / "source-checkout"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Git62 Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=source, check=True)
    manifest = source / "config" / "protocol_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text('{"schema":"GIT62_FIXTURE","core_files":{}}\n', encoding="utf-8")
    subprocess.run(["git", "add", "config/protocol_manifest.json"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "Test source release"], cwd=source, check=True)
    sha = _git("rev-parse", "HEAD", cwd=source)

    installed = tmp_path / "installed"
    (installed / "config").mkdir(parents=True)
    (installed / "config" / "protocol_manifest.json").write_bytes(manifest.read_bytes())
    receipt = installed / "localdata" / "update" / "deployment_baseline.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "schema": "SMARTAGENT_DEPLOYMENT_BASELINE_V1",
        "install_root": str(installed),
        "source_root": str(source),
        "release_id": "test",
        "files": {"config/protocol_manifest.json": "test"},
    }), encoding="utf-8")

    # This regression isolates provenance and SHA matching; the complete 279
    # core-file manifest is already tested by the normal CI manifest gate.
    monkeypatch.setattr(stop, "_verify_startup_contract",
                        lambda root: {"hash_verification": "PASS"})
    return source, installed, sha


def test_deployed_install_without_git_uses_verified_source(deployed_repo):
    source, installed, sha = deployed_repo
    assert not (installed / ".git").exists()
    stop._verify_upgrade_commit(installed, sha)
    stop._verify_upgrade_commit(installed, sha, source)


def test_deployed_install_rejects_wrong_sha(deployed_repo):
    source, installed, sha = deployed_repo
    with pytest.raises(RuntimeError, match="force_stop_upgrade_source_head_mismatch"):
        stop._verify_upgrade_commit(installed, "0" * 40)


def test_deployed_install_rejects_modified_manifest(deployed_repo):
    source, installed, sha = deployed_repo
    (installed / "config" / "protocol_manifest.json").write_text("TAMPERED")
    with pytest.raises(RuntimeError, match="force_stop_upgrade_manifest_differs_from_commit"):
        stop._verify_upgrade_commit(installed, sha)


def test_deployed_install_missing_source_receipt_fails_closed(deployed_repo):
    source, installed, sha = deployed_repo
    (installed / "localdata" / "update" / "deployment_baseline.json").unlink()
    with pytest.raises(RuntimeError, match="force_stop_upgrade_source_checkout_required"):
        stop._verify_upgrade_commit(installed, sha)


def test_deployed_install_source_root_must_be_exact_git_toplevel(deployed_repo):
    source, installed, sha = deployed_repo
    (source / "subdir").mkdir()
    with pytest.raises(RuntimeError, match="force_stop_upgrade_source_git_root_mismatch"):
        stop._verify_upgrade_commit(installed, sha, source / "subdir")


def test_root_check_refuses_foreign_directory_before_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "install"
    fake_file = root / "source" / "agent_core" / "force_stop_all_agents.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("fixture", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "protocol_manifest.json").write_text("fixture")
    mode = root / "localdata" / "secure" / "windows_security" / "acl_mode.json"
    mode.parent.mkdir(parents=True)
    mode.write_text("fixture")
    monkeypatch.setattr(stop, "__file__", str(fake_file))
    assert stop._verified_install_root(root) == root.resolve()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with pytest.raises(RuntimeError, match="force_stop_install_root_mismatch"):
        stop._verified_install_root(foreign)
    (root / "config" / "protocol_manifest.json").unlink()
    with pytest.raises(RuntimeError, match="force_stop_install_marker_missing"):
        stop._verified_install_root(root)
