"""Offline tests for temporary software-owned interruption RCA upload."""
from __future__ import annotations
import base64
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_core.auto_rca_upload import collect_and_upload


class FakeGitHub:
    def __init__(self, archive: Path, *, private=True, fail_upload=False):
        self.archive = archive
        self.private = private
        self.fail_upload = fail_upload
        self.remote_sha = ""
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "cmd.exe":
            return SimpleNamespace(returncode=0, stdout=f"[COLLECT] ZIP: {self.archive}\n")
        if cmd[-1] == ".private":
            return SimpleNamespace(returncode=0, stdout="true\n" if self.private else "false\n")
        if cmd[-1] == ".sha":
            return SimpleNamespace(returncode=0, stdout=self.remote_sha + "\n")
        if "PUT" in cmd:
            if self.fail_upload:
                return SimpleNamespace(returncode=1, stdout="")
            data = json.loads(kwargs["input"])
            raw = base64.b64decode(data["content"])
            self.remote_sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "content": {"path": cmd[4].split("/contents/", 1)[1],
                            "sha": self.remote_sha, "size": len(raw)},
                "commit": {"sha": "a" * 40},
            }))
        raise AssertionError("unexpected command: " + repr(cmd))


class AutoRcaUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.collector = root / "collect.bat"
        self.collector.touch()
        out = root / "localdata" / "diagnostics" / "gar_live_rca"
        out.mkdir(parents=True)
        self.archive = out / "GAR_LIVE_RCA_test.zip"
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "schema": "SMARTAGENT_LIVE_RCA_V3",
                "task_id": "TASK-EXAMPLE", "request_id": "RR-EXAMPLE",
            }))
            z.writestr("README.txt", "sanitized")

    def test_upload_and_remote_blob_verified(self):
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["status"], "SUCCESS", result)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertIn("/blob/main/tasks/GAR_LIVE_RCA_", result["github_url"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(len(fake.calls), 4)
        self.assertTrue(self.archive.exists())

    def test_legacy_v2_archive_supported(self):
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "schema": "SMARTAGENT_LIVE_RCA_V2",
                "task_id": "TASK-EXAMPLE", "request_id": "RR-EXAMPLE",
            }))
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["status"], "SUCCESS", result)

    def test_other_archive_schema_rejected(self):
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "schema": "OTHER",
                "task_id": "TASK-EXAMPLE", "request_id": "RR-EXAMPLE",
            }))
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["reason"], "ARCHIVE_SCHEMA_MISMATCH")
        self.assertFalse(any(c[0] == "gh" for c in fake.calls))

    def test_invalid_id_never_executes(self):
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-EXAMPLE & del", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["reason"], "INVALID_TASK_OR_REQUEST_ID")
        self.assertEqual(fake.calls, [])

    def test_public_repo_rejected_before_upload(self):
        fake = FakeGitHub(self.archive, private=False)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["reason"], "GITHUB_PRIVATE_REPOSITORY_NOT_VERIFIED")
        self.assertFalse(any("PUT" in c for c in fake.calls))

    def test_upload_failure_keeps_local_zip(self):
        fake = FakeGitHub(self.archive, fail_upload=True)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reason"], "GITHUB_UPLOAD_EXIT_NONZERO")
        self.assertEqual(result["local_zip"], str(self.archive))
        self.assertTrue(self.archive.exists())

    def test_archive_task_identity_required(self):
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-OTHER", "RR-EXAMPLE",
                                    collector=self.collector, run=fake)
        self.assertEqual(result["reason"], "ARCHIVE_TASK_ID_MISMATCH")
        self.assertFalse(any("gh" == c[0] for c in fake.calls))

    def test_collector_must_exist(self):
        fake = FakeGitHub(self.archive)
        result = collect_and_upload("TASK-EXAMPLE", "RR-EXAMPLE",
                                    collector=self.collector.parent / "missing.bat", run=fake)
        self.assertEqual(result["reason"], "HARDCODED_COLLECTOR_NOT_FOUND")
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
