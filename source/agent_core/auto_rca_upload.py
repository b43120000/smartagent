"""Temporary software-owned RCA upload after a SmartAgent worker interruption.

This module never participates in Planner actions or changes Task terminal state.
It uses the already installed GitHub CLI credential; no credential is read here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR = Path(r"C:\Users\user\Desktop\remoteagent\smartagent\collect.bat")
REPOSITORY = "b43120000/test"
BRANCH = "main"
DESTINATION = "tasks"
MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_ZIP_LINE = re.compile(r"^\[COLLECT\] ZIP:\s*(.+?)\s*$", re.MULTILINE)


def collect_and_upload(task_id: str, request_id: str, *, collector: Path = COLLECTOR,
                       run=subprocess.run) -> dict:
    """Collect one RCA bundle and publish it to a private GitHub repository.

    Always returns a bounded, credential-free status. On failure the local ZIP
    remains available. This is best-effort diagnostics, never task recovery.
    """
    result = {"status": "FAILED", "phase": "PRECHECK", "reason": "NOT_STARTED"}
    try:
        if not _ID.fullmatch(task_id) or not _ID.fullmatch(request_id):
            raise ValueError("INVALID_TASK_OR_REQUEST_ID")
        if not task_id.startswith("TASK-") or not request_id.startswith("RR-"):
            raise ValueError("INVALID_TASK_OR_REQUEST_PREFIX")
        collector = Path(collector)
        if not collector.is_file():
            raise ValueError("HARDCODED_COLLECTOR_NOT_FOUND")
        result["phase"] = "COLLECT"
        completed = run(["cmd.exe", "/d", "/c", str(collector), task_id, request_id],
                        capture_output=True, text=True, errors="replace", timeout=180)
        if completed.returncode != 0:
            raise ValueError("COLLECTOR_EXIT_NONZERO")
        matches = _ZIP_LINE.findall(completed.stdout or "")
        if len(matches) != 1:
            raise ValueError("COLLECTOR_ZIP_OUTPUT_MISSING_OR_AMBIGUOUS")
        archive = Path(matches[0].strip()).resolve(strict=True)
        expected_root = (collector.parent / "localdata" / "diagnostics" / "gar_live_rca").resolve()
        if archive.parent != expected_root or archive.suffix.lower() != ".zip":
            raise ValueError("COLLECTOR_ZIP_OUTSIDE_EXPECTED_DIR")
        result["local_zip"] = str(archive)
        if not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
            raise ValueError("ARCHIVE_SIZE_OUT_OF_BOUNDS")
        with zipfile.ZipFile(archive) as handle:
            if handle.testzip() is not None:
                raise ValueError("ARCHIVE_CRC_INVALID")
            manifest = json.loads(handle.read("manifest.json"))
            if manifest.get("task_id") != task_id or manifest.get("request_id") != request_id:
                raise ValueError("ARCHIVE_TASK_ID_MISMATCH")
            if manifest.get("schema") not in {"SMARTAGENT_LIVE_RCA_V2", "SMARTAGENT_LIVE_RCA_V3"}:
                raise ValueError("ARCHIVE_SCHEMA_MISMATCH")
            # New Collector output is required to carry explicit causal
            # coverage. A valid archive is not a verified complete RCA.
            declared = manifest.get("causal_coverage_schema")
            if declared:
                if declared != "SMARTAGENT_RCA_CAUSAL_COVERAGE_V1":
                    raise ValueError("RCA_CAUSAL_COVERAGE_SCHEMA_UNKNOWN")
                try:
                    coverage = json.loads(handle.read("rca_causal_coverage.json"))
                except (KeyError, ValueError, UnicodeDecodeError):
                    raise ValueError("RCA_CAUSAL_COVERAGE_MISSING") from None
                if (coverage.get("schema") != declared
                        or coverage.get("status") != manifest.get("causal_coverage_status")
                        or coverage.get("status") not in {
                            "COVERED_WITH_BOUNDED_WINDOW", "PARTIAL"
                        }):
                    raise ValueError("RCA_CAUSAL_COVERAGE_INVALID")
                result["causal_coverage_status"] = coverage["status"]
                result["causal_coverage_complete"] = (
                    coverage["status"] == "COVERED_WITH_BOUNDED_WINDOW"
                )
                result["causal_coverage_issue_codes"] = [
                    str(x)[:80] for x in list(coverage.get("issues") or [])[:16]
                    if isinstance(x, str) and re.fullmatch(r"[A-Z_]{1,80}", x)
                ]
        archive_bytes = archive.read_bytes()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        result["sha256"] = digest
        result["bytes"] = len(archive_bytes)

        result["phase"] = "GITHUB_PRIVACY_CHECK"
        privacy = run(["gh", "api", f"repos/{REPOSITORY}", "--jq", ".private"],
                      capture_output=True, text=True, errors="replace", timeout=30)
        if privacy.returncode != 0 or (privacy.stdout or "").strip() != "true":
            raise ValueError("GITHUB_PRIVATE_REPOSITORY_NOT_VERIFIED")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = (f"GAR_LIVE_RCA_{stamp}_{task_id[:64]}_{request_id[:64]}_"
                    f"{digest[:12]}_{uuid.uuid4().hex[:8]}.zip")
        remote_path = f"{DESTINATION}/{filename}"
        blob_sha = hashlib.sha1(f"blob {len(archive_bytes)}\0".encode("ascii") + archive_bytes).hexdigest()
        data = {"message": f"debug: interrupted RCA {task_id} {request_id}",
                "content": base64.b64encode(archive_bytes).decode("ascii"), "branch": BRANCH}
        result["phase"] = "GITHUB_UPLOAD"
        upload = run(["gh", "api", "--method", "PUT",
                      f"repos/{REPOSITORY}/contents/{remote_path}", "--input", "-"],
                     input=json.dumps(data, separators=(",", ":")), capture_output=True,
                     text=True, errors="replace", timeout=120)
        if upload.returncode != 0:
            raise ValueError("GITHUB_UPLOAD_EXIT_NONZERO")
        reply = json.loads(upload.stdout or "{}")
        content = reply.get("content") or {}
        commit = reply.get("commit") or {}
        if (content.get("path") != remote_path or content.get("sha") != blob_sha
                or int(content.get("size", -1)) != len(archive_bytes)
                or not re.fullmatch(r"[a-f0-9]{40}", str(commit.get("sha", "")))):
            raise ValueError("GITHUB_UPLOAD_RECEIPT_INVALID")
        result["phase"] = "GITHUB_VERIFY"
        verified = run(["gh", "api", f"repos/{REPOSITORY}/contents/{remote_path}",
                        "--jq", ".sha"], capture_output=True, text=True,
                       errors="replace", timeout=30)
        if verified.returncode != 0 or (verified.stdout or "").strip() != blob_sha:
            raise ValueError("GITHUB_UPLOADED_BLOB_VERIFICATION_FAILED")
        result.update(
            status="SUCCESS", phase="COMPLETE",
            reason=("UPLOAD_VERIFIED_CAUSAL_COVERAGE_PARTIAL"
                    if result.get("causal_coverage_status") == "PARTIAL"
                    else "UPLOAD_VERIFIED"),
                      github_url=f"https://github.com/{REPOSITORY}/blob/{BRANCH}/{remote_path}",
                      github_commit=commit["sha"], github_path=remote_path)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        result["reason"] = reason[:100]
    return result
