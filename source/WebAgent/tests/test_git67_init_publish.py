"""Offline Git67 private auto-publishing contract: raw bytes stay local."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from agent_core.it10_auto_publish import build_derivative, upload_derivative

TASK = "TASK-IT10AUTOTEST123"
REQUEST = "RR-IT10AUTOTEST123"
GOOD = r"E:\AIspace\.case_d_backups"
BAD = r"E:\AIspace.case_d_backups"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _blob_ref(i):
    return ("it10-raw-GIT67TEST-abcdef1234567890abcdef12-123-"
            + f"{i:032x}" + ".bin")


def _original_bundle(tmp_path):
    output = tmp_path / "full_original_do_not_upload.zip"
    payloads = [
        ("06_worker_input", ("SECRET_PRIVATE_TOKEN=NEVER_PUBLISH\n" + GOOD).encode()),
        ("11_minimal_planner_response",
         ("SECRET_PASSWORD=NEVER_PUBLISH\n"
          + json.dumps({"arguments": {"path": GOOD}})).encode()),
        ("11_minimal_path_integrity_rejected",
         ("PRIVATE_PHONE_NUMBER=NEVER_PUBLISH\n" + BAD).encode()),
    ]
    rows = []
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "schema": "SMARTAGENT_LIVE_RCA_V3",
            "task_id": TASK, "request_id": REQUEST,
            "installed_git_head": "b" * 40,
        }))
        for i, (stage, raw) in enumerate(payloads, 1):
            ref = _blob_ref(i)
            zf.writestr("it10/raw/" + ref, raw)
            rows.append({
                "stage": stage, "round": 1, "candidate_index": 0,
                "source_file": r"C:\Users\SECRET_USERNAME\repo\file.py",
                "source_function": "test_hook", "raw_origin": "UTF8_FROM_PYTHON_STR",
                "raw_blob_ref": ref, "raw_blob_sha256": _sha(raw),
                "raw_blob_byte_length": len(raw),
                "pointer_verified_and_copied": True,
            })
        zf.writestr("it10_raw_evidence_index.json", json.dumps({
            "status": "PARTIAL", "task_id": TASK, "request_id": REQUEST,
            "raw_blob_count": 3, "missing_stages": ["10_submitted_innerText"],
            "missing_refs": [], "execution_path": "MINIMAL_EXECUTOR",
        }))
        zf.writestr("it10_boundary_matrix.json", json.dumps({
            "task_id": TASK, "request_id": REQUEST,
            "stage_records": rows,
        }))
    return output


def test_derived_archive_contains_exact_path_bytes_but_no_private_payload(tmp_path):
    original = _original_bundle(tmp_path)
    safe, report = build_derivative(original, TASK, REQUEST)
    assert report["original_local_zip_never_uploaded"] is True
    assert report["original_local_zip_sha256"] == _sha(original.read_bytes())
    assert report["original_blob_count_verified"] == 3
    assert report["exact_path_excerpts"] == 3
    assert report["missing_stages"] == ["10_submitted_innerText"]
    assert b"NEVER_PUBLISH" not in safe
    assert b"SECRET_USERNAME" not in safe
    assert b"SECRET_PRIVATE_TOKEN" not in safe
    assert b"PRIVATE_PHONE_NUMBER" not in safe
    assert b"SECRET_PASSWORD" not in safe
    assert len(safe) > 100  # The ZIP contains only constrained derived evidence.
    with zipfile.ZipFile(io.BytesIO(safe)) as zf:
        assert zf.testzip() is None
        derived = json.loads(zf.read("safe_evidence.json"))
        rows = derived["stage_records"]
        assert all(r["original_pointer_verified"] for r in rows)
        assert rows[0]["source_module"] == "file.py"
        assert rows[0]["exact_original_byte_excerpts"][0]["utf8_hex_exact"] == GOOD.encode().hex()
        assert rows[2]["exact_original_byte_excerpts"][0]["utf8_hex_exact"] == BAD.encode().hex()


def test_wrong_task_and_broken_pointer_fail_closed(tmp_path):
    original = _original_bundle(tmp_path)
    with pytest.raises(ValueError, match="ORIGINAL_ARCHIVE_ID_MISMATCH"):
        build_derivative(original, "TASK-DIFFERENT", REQUEST)
    with zipfile.ZipFile(original, "a") as zf:
        zf.writestr("other_private_payload.txt", "NOT_EXPORTED")
    # Unlisted content is not exported even if present in original ZIP.
    safe, _ = build_derivative(original, TASK, REQUEST)
    assert b"NOT_EXPORTED" not in safe


class FakeGh:
    def __init__(self, private=True, push=True):
        self.private = private
        self.push = push
        self.contents = {}
        self.receipts = []

    def __call__(self, command, **kwargs):
        assert command[:2] == ["gh", "api"]
        if command[2] == "repos/b43120000/test":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "private": self.private, "full_name": "b43120000/test",
                "permissions": {"push": self.push},
            }))
        if "--method" in command:
            request = json.loads(kwargs["input"])
            path = next(c for c in command if c.startswith("repos/b43120000/test/contents/")).split("/contents/", 1)[1]
            data = base64.b64decode(request["content"])
            sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            self.contents[path] = (sha, data)
            self.receipts.append(path)
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "content": {"path": path, "sha": sha},
                "commit": {"sha": "a" * 40},
            }))
        if command[-2:] == ["--jq", ".sha"]:
            path = command[2].split("/contents/", 1)[1]
            return SimpleNamespace(returncode=0, stdout=self.contents[path][0] + "\n")
        raise AssertionError(command)


def test_auto_publish_to_verified_private_repo_only(tmp_path):
    safe, report = build_derivative(_original_bundle(tmp_path), TASK, REQUEST)
    fake = FakeGh()
    receipt = upload_derivative(safe, report, run_id="GIT67_abcdef0123456789", run=fake)
    assert receipt["status"] == "SUCCESS"
    assert len(fake.contents) == 3
    assert "safe_evidence.json" in receipt["github_safe_index_url"]
    assert all(b"NEVER_PUBLISH" not in content for _, content in fake.contents.values())
    assert all("GIT67-AUTO-" in p for p in fake.contents)
    rejected = FakeGh(private=False)
    with pytest.raises(ValueError, match="GIT_TEST_PRIVATE_REPOSITORY_NOT_VERIFIED"):
        upload_derivative(safe, report, run_id="GIT67_abcdef0123456789", run=rejected)
    assert not rejected.contents
    no_push = FakeGh(push=False)
    with pytest.raises(ValueError, match="GIT_TEST_PUSH_PERMISSION_MISSING"):
        upload_derivative(safe, report, run_id="GIT67_abcdef0123456789", run=no_push)
    assert not no_push.contents


@pytest.mark.skipif(os.name != "nt", reason="Windows same-CMD initializer only")
def test_init_bat_persists_environment_and_protects_private_acl(tmp_path):
    root = Path(__file__).resolve().parents[3]
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        pytest.skip("checkout .venv not provisioned")
    gh = tmp_path / "gh.cmd"
    gh.write_text("@echo off\nif /I \"%~1\"==\"api\" echo true\n", encoding="ascii")
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    # Use an actual parent CMD script, not subprocess.list2cmdline quoting
    # of a nested /c string. Windows may re-quote the entire command and
    # return "'<path>init.bat' not recognized" without invoking init.
    caller = tmp_path / "invoke_init_and_check_environment.cmd"
    caller.write_text(
        '@echo off\n'
        'call "' + str(root / "init.bat") + '"\n'
        'if errorlevel 1 exit /b 4\n'
        'echo GIT67_AFTER_CALL=%SMARTAGENT_IT10_AUTO_PUBLISH%\n'
        'echo GIT67_TRACE=%SMARTAGENT_IT10_PATH_TRACE_RAW%\n'
        'echo GIT67_ID=%SMARTAGENT_IT10_PATH_TRACE_RUN_ID%\n',
        encoding="ascii",
    )
    result = subprocess.run(["cmd.exe", "/d", "/c", str(caller)], cwd=root, env=env,
                            capture_output=True, text=True, errors="replace", timeout=120)
    assert result.returncode == 0, result.stdout[-2800:] + result.stderr[-2800:]
    assert "GIT67_AFTER_CALL=YES" in result.stdout
    assert "GIT67_TRACE=YES" in result.stdout
    found = re.search(r"GIT67_ID=(GIT67_[a-f0-9]{32})", result.stdout)
    assert found
    raw_root = root / "localdata" / "diagnostics" / ("it10_" + found.group(1))
    assert raw_root.is_dir()
    # The current Windows user has FullControl on this newly restricted dir.
    status = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                             "(Get-Acl -LiteralPath '" + str(raw_root) + "').AreAccessRulesProtected"],
                            capture_output=True, text=True, timeout=25)
    assert status.returncode == 0 and status.stdout.strip() == "True"
    raw_root.rmdir()
