"""Windows-only Git67 collect.bat --it10-raw ZIP integration smoke test.

Creates a synthetic, marked trace; never talks to Telegram or ChatGPT and
never dispatches a Runtime action. Runs against the current repo checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

from agent_core import path_integrity_trace as PIT

ROOT = Path(__file__).resolve().parents[3]
REQUEST = ("NLIT_CASE_ID=IT10\n"
           "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
           "E:\\AIspace\\.case_d_backups\\CI\\probe.txt\r\n")


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 integration only")
class CollectBatWindowsTests(unittest.TestCase):
    def test_manual_raw_flag_joins_trace_and_binary(self):
        python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
        self.assertTrue(python_exe.is_file(), "CI must create the checkout .venv")
        run = "GIT67-COLLECT-" + uuid.uuid4().hex[:12].upper()
        task = "TASK-" + uuid.uuid4().hex[:14].upper()
        request = "RR-" + uuid.uuid4().hex[:14].upper()
        private = ROOT / "localdata" / "diagnostics" / ("it10_ci_" + run)
        private.mkdir(parents=True, exist_ok=False)
        env = {
            "SMARTAGENT_IT10_PATH_TRACE": "1",
            "SMARTAGENT_IT10_PATH_TRACE_RAW": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
            "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": run,
            "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(private),
        }
        archives = []
        try:
            with patch.dict(os.environ, env):
                PIT.begin(REQUEST, task_id=task, request_id=request, transport="LOCAL_TEST")
                PIT.record("06_worker_input", REQUEST)
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(ROOT / "collect.bat"),
                     task, request, "--it10-raw"],
                    cwd=ROOT, env=os.environ.copy(), text=True, errors="replace",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                )
            self.assertEqual(result.returncode, 0,
                             result.stdout[-3500:] + "\n" + result.stderr[-3500:])
            match = re.search(r"^\[COLLECT\] ZIP:\s*(.+)$", result.stdout, flags=re.MULTILINE)
            self.assertIsNotNone(match, result.stdout[-3500:])
            archive = Path(match.group(1).strip())
            archives.append(archive)
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as zf:
                self.assertIsNone(zf.testzip())
                manifest = json.loads(zf.read("manifest.json"))
                index = json.loads(zf.read("it10_raw_evidence_index.json"))
                boundary = json.loads(zf.read("it10_boundary_matrix.json"))
                self.assertEqual(boundary["task_id"], task)
                self.assertEqual(boundary["request_id"], request)
                self.assertEqual(len(boundary["stage_records"]), 1)
                self.assertEqual(boundary["stage_records"][0]["stage"], "06_worker_input")
                self.assertTrue(boundary["stage_records"][0]["pointer_verified_and_copied"])
                self.assertEqual(manifest["it10_raw_evidence_status"], "PARTIAL")
                self.assertEqual(index["trace_file_count"], 1)
                self.assertEqual(index["raw_blob_count"], 1)
                self.assertEqual(index["missing_refs"], [])
                traces = [item for item in zf.namelist() if item.startswith("it10/trace/")]
                self.assertEqual(len(traces), 1)
                rows = [json.loads(line) for line in zf.read(traces[0]).decode("utf-8").splitlines()]
                self.assertEqual(len(rows), 1)
                target = rows[0]["raw_blob_ref"]
                actual = zf.read("it10/raw/" + target)
                self.assertEqual(actual, REQUEST.encode("utf-8"))
                self.assertEqual(hashlib.sha256(actual).hexdigest(), rows[0]["raw_blob_sha256"])
            # A second invocation with the same IDs inside the same second
            # must NEVER overwrite the previously generated original/raw ZIP.
            with patch.dict(os.environ, env):
                again = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(ROOT / "collect.bat"),
                     task, request, "--it10-raw"],
                    cwd=ROOT, env=os.environ.copy(), text=True, errors="replace",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                )
            self.assertEqual(again.returncode, 0, again.stdout[-2500:] + again.stderr[-2500:])
            match2 = re.search(r"^\[COLLECT\] ZIP:\s*(.+)$", again.stdout, flags=re.MULTILINE)
            self.assertIsNotNone(match2, again.stdout[-2500:])
            other = Path(match2.group(1).strip())
            archives.append(other)
            self.assertTrue(other.is_file())
            self.assertNotEqual(other.resolve(), archive.resolve())
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(other) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(json.loads(zf.read("it10_raw_evidence_index.json"))["missing_refs"], [])
        finally:
            PIT.begin("")
            for archive in archives:
                archive.unlink(missing_ok=True)
            for file in private.iterdir():
                file.unlink()
            private.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
