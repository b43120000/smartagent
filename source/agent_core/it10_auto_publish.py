"""Explicit IT10-only automatic *derived* RCA evidence publishing to private Git Test.

Original raw ZIP never leaves the machine. Publish exact byte excerpts of
controlled Golden path from verified raw blobs, with their original offsets
and SHA256, plus stage boundary index. All remaining payload bytes (private
chat, user identity, Telegram JSON, model prompt, cookies) stay local.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile

REPOSITORY = "b43120000/test"
BRANCH = "main"
_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
_STAGE = re.compile(r"^[A-Za-z0-9_]{1,100}$")
# Includes literal and JSON-escaped versions, plus the deleted-slash mutant.
# No arbitrary Windows path, host path or surrounding context may be published.
_TARGET = re.compile(rb"E:(?:\\{1,2})AIspace(?:\\{0,2})\.case_d_backups")
MAX_ARCHIVE = 80 * 1024 * 1024
MAX_RAW = 2 * 1024 * 1024
MAX_STAGE_ROWS = 2000


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")


def _excerpts(payload: bytes) -> list[dict]:
    return [{
        "offset_in_original_blob": match.start(),
        "length": match.end() - match.start(),
        "utf8_hex_exact": match.group().hex(),
        "text_exact": match.group().decode("ascii"),
        "sha256_exact": _sha(match.group()),
        "interpretation": "ORIGINAL_BYTE_SPAN_NOT_FULL_BLOB",
    } for match in list(_TARGET.finditer(payload))[:16]]


def build_derivative(archive: Path, task: str, request: str) -> tuple[bytes, dict]:
    """Validate the complete original local bundle and derive safe excerpts."""
    if not (_ID.fullmatch(task) and _ID.fullmatch(request)):
        raise ValueError("INVALID_ID")
    if not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE:
        raise ValueError("ORIGINAL_ARCHIVE_INVALID_SIZE")
    original_sha = _sha(archive.read_bytes())
    with zipfile.ZipFile(archive) as src:
        if src.testzip() is not None:
            raise ValueError("ORIGINAL_ARCHIVE_CRC_FAILED")
        manifest = json.loads(src.read("manifest.json"))
        if manifest.get("task_id") != task or manifest.get("request_id") != request:
            raise ValueError("ORIGINAL_ARCHIVE_ID_MISMATCH")
        if manifest.get("schema") not in ("SMARTAGENT_LIVE_RCA_V2", "SMARTAGENT_LIVE_RCA_V3"):
            raise ValueError("ORIGINAL_ARCHIVE_SCHEMA_MISMATCH")
        index = json.loads(src.read("it10_raw_evidence_index.json"))
        matrix = json.loads(src.read("it10_boundary_matrix.json"))
        if index.get("task_id") != task or index.get("request_id") != request:
            raise ValueError("IT10_INDEX_ID_MISMATCH")
        if matrix.get("task_id") != task or matrix.get("request_id") != request:
            raise ValueError("IT10_MATRIX_ID_MISMATCH")
        if index.get("status") in ("NOT_ARMED", "NO_TASK_REQUEST_BOUND_TRACE", "COLLECTOR_ERROR"):
            raise ValueError("IT10_TRACE_NOT_PRESENT")
        rows = matrix.get("stage_records")
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_STAGE_ROWS:
            raise ValueError("IT10_STAGE_ROW_BOUNDS")
        derived_rows = []
        original_verified = 0
        snippets = 0
        seen = set()
        for row in rows:
            stage = row.get("stage", "")
            if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
                raise ValueError("INVALID_STAGE_NAME")
            ref = row.get("raw_blob_ref")
            valid_pointer = row.get("pointer_verified_and_copied") is True
            original_hash = row.get("raw_blob_sha256")
            blob_size = row.get("raw_blob_byte_length")
            fragments = []
            if ref and valid_pointer:
                if not isinstance(ref, str) or not re.fullmatch(r"it10-raw-[A-Za-z0-9_-]{8,120}-[0-9]+-[a-f0-9]{32}\.bin", ref):
                    raise ValueError("INVALID_RAW_POINTER")
                blob_path = "it10/raw/" + ref
                if blob_path not in src.namelist():
                    raise ValueError("MISSING_ORIGINAL_BLOB")
                stat = src.getinfo(blob_path)
                if not 0 < stat.file_size <= MAX_RAW:
                    raise ValueError("RAW_BLOB_OUT_OF_BOUNDS")
                blob = src.read(blob_path)
                if (_sha(blob) != original_hash or len(blob) != blob_size):
                    raise ValueError("ORIGINAL_BLOB_SHA_MISMATCH")
                fragments = _excerpts(blob)
                seen.add(ref)
                original_verified += 1
                snippets += len(fragments)
            elif valid_pointer:
                raise ValueError("VERIFIED_POINTER_WITHOUT_NAME")
            # NO raw HTTP body, no DOM, no full prompt or metadata value here.
            # Original digest and excerpt offset preserve auditability on host.
            derived_rows.append({
                "stage": stage,
                "round": row.get("round") if type(row.get("round")) is int else None,
                "candidate_index": (row.get("candidate_index")
                                    if type(row.get("candidate_index")) is int else None),
                "source_module": Path(str(row.get("source_file") or "")).name[:100],
                "source_function": str(row.get("source_function") or "")[:100],
                "raw_origin": str(row.get("raw_origin") or "")[:75],
                "evidence_status": str(row.get("evidence_status") or "")[:75],
                "original_raw_blob_sha256": original_hash
                    if isinstance(original_hash, str) and re.fullmatch(r"[0-9a-f]{64}", original_hash) else None,
                "original_raw_blob_bytes": blob_size if type(blob_size) is int else None,
                "original_pointer_verified": valid_pointer,
                "exact_original_byte_excerpts": fragments,
            })
        if len(seen) != index.get("raw_blob_count"):
            # Multiple stages can reference same blob; every raw blob must
            # have been linked by at least one selected event.
            raise ValueError("RAW_POINTER_CLOSURE_COUNT_MISMATCH")

    minimal = {
        "schema": "SMARTAGENT_IT10_SAFE_EVIDENCE_V1",
        "task_id": task, "request_id": request,
        "installed_git_head": manifest.get("installed_git_head"),
        "original_local_zip_sha256": original_sha,
        "original_local_zip_bytes": archive.stat().st_size,
        "original_local_zip_never_uploaded": True,
        "archive_format": "REDACTED_DERIVATIVE",
        "data_classification": "ONLY_EXACT_CONTROLLED_PATH_BYTES_AND_DIGESTS",
        "original_blob_count_verified": original_verified,
        "exact_path_excerpts": snippets,
        "raw_collector_status": index.get("status"),
        "execution_path": index.get("execution_path"),
        "missing_stages": [s for s in index.get("missing_stages", []) if isinstance(s, str) and _STAGE.fullmatch(s)][:100],
        "missing_raw_stages": [s for s in index.get("missing_raw_stages", []) if isinstance(s, str) and _STAGE.fullmatch(s)][:100],
        "missing_refs_count": len(index.get("missing_refs") or []),
        "stage_records": derived_rows,
        "limitations": [
            "Only original byte spans of controlled Golden Path are shared.",
            "Unredacted source blob, Telegram HTTP JSON and private prompts stay local.",
            "For full causal RCA, request the local original ZIP through a secure channel.",
            "A raw blob SHA256 cannot be recomputed from its published excerpt.",
        ],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        dst.writestr("safe_evidence.json", _json_bytes(minimal))
        dst.writestr("it10_boundary_matrix.json", _json_bytes({
            "schema": "SMARTAGENT_IT10_SAFE_BOUNDARY_MATRIX_V1",
            "task_id": task, "request_id": request,
            "stage_records": derived_rows,
        }))
        dst.writestr("README.txt",
                     "REDACTED_DERIVATIVE; original raw bytes remain only on the Windows host.\n"
                     "Only exact controlled-path UTF-8 byte spans and original offsets are included.\n")
    return out.getvalue(), minimal


def _run_gh(args: list[str], *, input_text: str | None = None,
            timeout: int = 40, run=subprocess.run):
    cmd = ["gh", "api", *args]
    result = run(cmd, input=input_text, capture_output=True,
                 text=True, errors="replace", timeout=timeout)
    if result.returncode:
        raise ValueError("GH_API_FAILED")
    return (result.stdout or "").strip()


def upload_derivative(payload: bytes, evidence: dict, *,
                      run_id: str, run=subprocess.run) -> dict:
    """Upload only sanitized outputs via an already-authorized GitHub CLI."""
    if not _ID.fullmatch(run_id):
        raise ValueError("RUN_ID_INVALID")
    metadata = json.loads(_run_gh(["repos/" + REPOSITORY], run=run))
    if metadata.get("private") is not True or metadata.get("full_name") != REPOSITORY:
        raise ValueError("GIT_TEST_PRIVATE_REPOSITORY_NOT_VERIFIED")
    if metadata.get("permissions", {}).get("push") is not True:
        raise ValueError("GIT_TEST_PUSH_PERMISSION_MISSING")
    task, request = evidence["task_id"], evidence["request_id"]
    nonce = uuid.uuid4().hex[:12]
    base = f"evidence/git67/GIT67-AUTO-{run_id}/{task}_{request}_{nonce}"
    files = {
        "it10_safe_evidence.zip": payload,
        "safe_evidence.json": _json_bytes(evidence),
        "it10_boundary_matrix.json": _json_bytes({
            "schema": "SMARTAGENT_IT10_SAFE_BOUNDARY_MATRIX_V1",
            "task_id": task, "request_id": request,
            "stage_records": evidence["stage_records"],
        }),
    }
    last_commit = ""
    for name, content in files.items():
        path = base + "/" + name
        receipt = json.loads(_run_gh(
            ["--method", "PUT", "repos/" + REPOSITORY + "/contents/" + path,
             "--input", "-"],
            input_text=json.dumps({
                "message": f"git67: derived path-integrity evidence for {task}",
                "content": base64.b64encode(content).decode("ascii"),
                "branch": BRANCH,
            }, separators=(",", ":")),
            timeout=150, run=run,
        ))
        blob_hash = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content
        ).hexdigest()
        if (receipt.get("content", {}).get("path") != path
                or receipt.get("content", {}).get("sha") != blob_hash
                or not re.fullmatch(r"[a-f0-9]{40}",
                                    str(receipt.get("commit", {}).get("sha", "")))):
            raise ValueError("GITHUB_UPLOAD_RECEIPT_MISMATCH")
        confirmation = _run_gh(
            ["repos/" + REPOSITORY + "/contents/" + path, "--jq", ".sha"],
            timeout=45, run=run,
        )
        if confirmation != blob_hash:
            raise ValueError("GITHUB_REMOTE_BLOB_MISMATCH")
        last_commit = receipt["commit"]["sha"]
    return {
        "status": "SUCCESS",
        "source": "REDACTED_DERIVATIVE",
        "github_commit": last_commit,
        "github_evidence_url": f"https://github.com/{REPOSITORY}/tree/{last_commit}/{base}",
        "github_safe_index_url": f"https://github.com/{REPOSITORY}/blob/{last_commit}/{base}/safe_evidence.json",
        "github_zip_url": f"https://github.com/{REPOSITORY}/blob/{last_commit}/{base}/it10_safe_evidence.zip",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True, type=Path)
    ap.add_argument("--task", required=True)
    ap.add_argument("--request", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if not args.dry_run and (
            os.environ.get("SMARTAGENT_IT10_AUTO_PUBLISH") != "YES"
            or os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RAW") != "YES"
            or os.environ.get("SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED") != "YES"
        ):
            raise ValueError("AUTO_PUBLISH_NOT_ARMED_BY_INIT")
        original = args.zip.resolve(strict=True)
        payload, evidence = build_derivative(original, args.task, args.request)
        if args.dry_run:
            print("[GITTEST] DRY_RUN_SAFE_DERIVATIVE bytes=" + str(len(payload)))
            return 0
        run_id = os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RUN_ID", "")
        result = upload_derivative(payload, evidence, run_id=run_id)
        print("[GITTEST] STATUS=" + result["status"])
        print("[GITTEST] COMMIT=" + result["github_commit"])
        print("[GITTEST] EVIDENCE=" + result["github_evidence_url"])
        print("[GITTEST] INDEX=" + result["github_safe_index_url"])
        print("[GITTEST] ZIP=" + result["github_zip_url"])
        return 0
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
            zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        # Do not reflect exception text; it might contain a response body.
        code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print("[GITTEST] STATUS=FAILED")
        print("[GITTEST] REASON=" + code[:90])
        print("[GITTEST] ORIGINAL_LOCAL_ZIP_RETAINED=YES")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
