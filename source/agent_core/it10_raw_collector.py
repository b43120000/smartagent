"""Manual-only IT10 raw Evidence collector. Read only; never invoked by automatic upload.

Copies exact traced byte blobs AND their referencing trace files, preserving
byte identities and missing-reference evidence. Never resolves arbitrary
paths from a ledger or raw pointer: only strict same-directory IT10 names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

TRACE_NAME = re.compile(r"^it10-path-([A-Za-z0-9_-]{8,80}-[a-f0-9]{24})-[0-9]+\.jsonl$")
BLOB_NAME = re.compile(r"^it10-raw-([A-Za-z0-9_-]{8,80}-[a-f0-9]{24})-[0-9]+-[a-f0-9]{32}\.bin$")
IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
MAX_TRACE = 4 * 1024 * 1024
MAX_BLOB = 2 * 1024 * 1024
MAX_BUNDLE = 8 * 1024 * 1024
LEGACY_REQUIRED = (
    "02_telegram_message_text", "03_receiver_normalized", "04_gateway_input",
    "06_worker_input", "07_loop_input", "08_user_request_block",
    "09_composer_input", "11_planner_response", "11_parser_input",
)
# These are the REAL Git67 Minimal Executor stages, not the legacy protocol loop.
# Some stages can be legitimately UNOBSERVED on a blocked/no-tool task; the
# collector must preserve that status instead of claiming a chain PASS.
MINIMAL_REQUIRED = (
    "02_telegram_message_text", "03_receiver_normalized", "04_gateway_input",
    "05_task_store", "06_worker_input", "07_loop_input",
    "08_user_request_block", "08_minimal_prompt", "09_composer_input",
    "10_submitted_innerText", "10_submitted_textContent",
    "10d_provider_response_return", "11_minimal_planner_response",
    "11_minimal_parser_input", "12_minimal_decision",
    "13_minimal_admission_input", "13_minimal_admission_output",
    "13_minimal_pre_dispatch_action",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_rows(source: Path) -> list[dict] | None:
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_TRACE:
        return None
    try:
        rows = [json.loads(line) for line in source.read_bytes().decode("utf-8").splitlines()]
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not rows or not all(isinstance(row, dict) and
                           row.get("schema") == "SMARTAGENT_IT10_PATH_TRACE_V1"
                           for row in rows):
        return None
    return rows


def collect(task: str, request: str, raw_root: Path, stage: Path) -> dict:
    """Copy only one task/request's verifiable trace and all its raw pointers."""
    if not IDENTITY.fullmatch(task) or not IDENTITY.fullmatch(request):
        raise ValueError("INVALID_TASK_OR_REQUEST_ID")
    result = {
        "schema": "SMARTAGENT_IT10_RAW_COLLECT_V1",
        "status": "NOT_ARMED", "task_id": task, "request_id": request,
        "selected_trace_ids": [], "trace_file_count": 0, "raw_blob_count": 0,
        "raw_bytes": 0, "source_files": [], "missing_refs": [],
        "missing_stages": [], "untouched_raw_bytes": True,
        "observed_stages": {}, "execution_path": "UNDETERMINED",
        "unobserved_external_boundaries": [
            "PHONE_SIDE_BYTES_NOT_CAPTURED_BY_SMARTAGENT",
            "MODEL_SERVICE_SERVER_RAW_OUTPUT_NOT_CAPTURED_BY_BROWSER_UI",
        ],
    }
    if not (os.environ.get("SMARTAGENT_IT10_PATH_TRACE") == "1"
            and os.environ.get("SMARTAGENT_IT10_PATH_TRACE_RAW") == "YES"
            and os.environ.get("SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED") == "YES"):
        return result
    if not raw_root.is_absolute() or not raw_root.is_dir() or raw_root.is_symlink():
        result["status"] = "INVALID_PRIVATE_ROOT"
        return result
    if not stage.is_dir():
        result["status"] = "MISSING_STAGE"
        return result
    trace_files = sorted(p for p in raw_root.iterdir()
                         if TRACE_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink())
    cached = {}
    selected = set()
    for source in trace_files:
        rows = _load_rows(source)
        if rows is None:
            continue
        cached[source] = rows
        if any(row.get("task_id") == task and row.get("request_id") == request
               and row.get("trace_id") == TRACE_NAME.fullmatch(source.name).group(1)
               for row in rows):
            selected.add(TRACE_NAME.fullmatch(source.name).group(1))
    result["selected_trace_ids"] = sorted(selected)
    if not selected:
        result["status"] = "NO_TASK_REQUEST_BOUND_TRACE"
        return result

    budget = MAX_BUNDLE
    references: dict[str, dict] = {}
    seen_stages = set()
    stage_counts: dict[str, int] = {}
    transport_modes: set[str] = set()
    trace_out = stage / "it10" / "trace"
    blob_out = stage / "it10" / "raw"
    trace_out.mkdir(parents=True, exist_ok=True)
    blob_out.mkdir(parents=True, exist_ok=True)
    for source, rows in cached.items():
        trace_id = TRACE_NAME.fullmatch(source.name).group(1)
        if trace_id not in selected:
            continue
        if any(row.get("trace_id") != trace_id or
               row.get("task_id") not in ("", None, task) or
               row.get("request_id") not in ("", None, request) for row in rows):
            result["missing_refs"].append("TRACE_IDENTITY_MISMATCH:" + source.name)
            continue
        original = source.read_bytes()
        if len(original) > budget:
            result["missing_refs"].append("TRACE_OVERSIZED:" + source.name)
            continue
        destination = trace_out / source.name
        # Byte-for-byte; do not ConvertFrom-Json/ConvertTo-Json the source file.
        destination.write_bytes(original)
        budget -= len(original)
        result["trace_file_count"] += 1
        result["source_files"].append({
            "path": "it10/trace/" + source.name, "sha256": _sha(original),
            "bytes": len(original), "kind": "ORIGINAL_TRACE_JSONL",
        })
        for row in rows:
            seen_stages.add(str(row.get("stage") or ""))
            stage_key = str(row.get("stage") or "")
            stage_counts[stage_key] = stage_counts.get(stage_key, 0) + 1
            if row.get("transport"):
                transport_modes.add(str(row.get("transport")).upper())
            if row.get("raw_blob_status") != "PERSISTED":
                if row.get("raw_blob_status") in ("OVERSIZED", "WRITE_FAILED"):
                    result["missing_refs"].append(
                        "RAW_CAPTURE_" + str(row["raw_blob_status"]) + ":" + str(row.get("event_id", ""))[:48])
                continue
            ref = row.get("raw_blob_ref")
            match = BLOB_NAME.fullmatch(ref) if isinstance(ref, str) else None
            if not match or match.group(1) != trace_id:
                result["missing_refs"].append("RAW_POINTER_INVALID:" + str(row.get("event_id", ""))[:48])
                continue
            fingerprint = (row.get("raw_blob_sha256"), row.get("raw_blob_byte_length"))
            if (not isinstance(fingerprint[0], str) or
                    not re.fullmatch(r"[a-f0-9]{64}", fingerprint[0]) or
                    type(fingerprint[1]) is not int or not 0 < fingerprint[1] <= MAX_BLOB):
                result["missing_refs"].append("RAW_POINTER_BAD_DIGEST_OR_SIZE:" + ref)
                continue
            if ref in references and references[ref] != fingerprint:
                result["missing_refs"].append("RAW_POINTER_CONFLICT:" + ref)
                continue
            references[ref] = fingerprint
    for name, (expected_hash, expected_size) in sorted(references.items()):
        original_path = raw_root / name
        if (original_path.is_symlink() or not original_path.is_file() or
                original_path.stat().st_size != expected_size or expected_size > budget):
            result["missing_refs"].append("RAW_MISSING_OR_OVERSIZED:" + name)
            continue
        original = original_path.read_bytes()
        if _sha(original) != expected_hash:
            result["missing_refs"].append("RAW_SHA256_MISMATCH:" + name)
            continue
        destination = blob_out / name
        destination.write_bytes(original)
        if _sha(destination.read_bytes()) != expected_hash:
            result["missing_refs"].append("RAW_COPY_VERIFICATION_FAILED:" + name)
            destination.unlink(missing_ok=True)
            continue
        budget -= len(original)
        result["raw_blob_count"] += 1
        result["raw_bytes"] += len(original)
        result["source_files"].append({
            "path": "it10/raw/" + name, "sha256": expected_hash,
            "bytes": len(original), "kind": "BYTE_EXACT_RAW_BLOB",
        })
    minimal = "08_minimal_prompt" in seen_stages
    needed = MINIMAL_REQUIRED if minimal else LEGACY_REQUIRED
    if "TELEGRAM" in transport_modes:
        needed = ("01b_telegram_http_body",) + needed
    result["execution_path"] = "MINIMAL_EXECUTOR" if minimal else "LEGACY_OR_UNKNOWN"
    result["observed_stages"] = dict(sorted(stage_counts.items()))
    result["missing_stages"] = [stage for stage in needed if stage not in seen_stages]
    result["status"] = ("CAPTURED_WITH_EXPECTED_STAGES"
                        if not result["missing_refs"] and not result["missing_stages"]
                        else "PARTIAL")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--stage", required=True, type=Path)
    args = parser.parse_args()
    try:
        outcome = collect(args.task, args.request, args.root, args.stage)
    except (OSError, ValueError) as error:
        outcome = {"schema": "SMARTAGENT_IT10_RAW_COLLECT_V1",
                   "status": "COLLECTOR_ERROR", "error_type": type(error).__name__,
                   "source_files": [], "missing_refs": ["COLLECTOR_ERROR"], "missing_stages": []}
    destination = args.stage / "it10_raw_evidence_index.json"
    destination.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    print("IT10_RAW_STATUS=" + outcome["status"])
    return 0 if outcome["status"] != "COLLECTOR_ERROR" else 2


if __name__ == "__main__":
    raise SystemExit(main())
