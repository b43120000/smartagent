#!/usr/bin/env python3
"""Bundle ONLY SR first-readback capture and SR regression evidence.

No browser profile, cookies, OAuth tokens, secrets, arbitrary runtime logs,
environment dumps, local source files or whole-page HTML are included.
Contains model response text; operator MUST review before uploading.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SINGLE_LIMIT = 3 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
CAPTURE_FILES = frozenset({
    "capture.json", "extracted_response.txt", "dom_turn_inner_text.txt",
    "dom_selected_structure.json", "dom_turn_text_content.txt",
    "turn_identity.json", "turn_observation.json",
})
REGRESSION_FILES = frozenset({
    "summary.txt", "validate_shared_runtime_sr02_sr03.log",
    "validate_protocol_transport_normalization.log",
    "validate_narrative_v9_bridge.log",
    "validate_single_fence_transport_contract.log",
    "validate_protocol_recovery_freshness.log",
    "validate_protocol_recovery_readback.log",
    "validate_tri_one_5a_protocol_it.log",
    "validate_sr_first_readback_gate.log", "validate_sr04_sr05_evidence.log",
    "strict_manifest.log",
})



def _capture_integrity_report(captures: Path) -> list[dict]:
    """Compare actual file bytes with captured in-memory hashes."""
    records = []
    if not captures.is_dir():
        return records
    for folder in sorted(captures.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        meta = folder / "capture.json"
        if not meta.is_file() or meta.is_symlink():
            continue
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            records.append({"folder": folder.name, "status": "INVALID_METADATA"})
            continue
        fidelity = info.get("byte_fidelity")
        if not isinstance(fidelity, dict):
            records.append({"folder": folder.name, "status": "LEGACY_UNVERIFIED"})
            continue
        problems = []
        for key, name in (
            ("extracted_response", "extracted_response.txt"),
            ("dom_turn_inner_text", "dom_turn_inner_text.txt"),
            ("dom_turn_text_content", "dom_turn_text_content.txt"),
        ):
            rec = fidelity.get(key)
            if rec is None:
                continue
            path = folder / name
            if not path.is_file() or path.is_symlink():
                problems.append(name + ":MISSING")
                continue
            data = path.read_bytes()
            if (len(data) != rec.get("stored_bytes")
                    or hashlib.sha256(data).hexdigest() != rec.get("stored_sha256")):
                problems.append(name + ":BYTE_MISMATCH")
            if not rec.get("matches_original") and not rec.get("truncated"):
                problems.append(name + ":NOT_ORIGINAL")
        # Optional for historical captures, mandatory structural validity if present.
        roster_file = folder / "turn_observation.json"
        if roster_file.exists():
            if roster_file.is_symlink() or not roster_file.is_file():
                problems.append("turn_observation.json:UNSAFE_PATH")
            else:
                try:
                    roster = json.loads(roster_file.read_text(encoding="utf-8"))
                    if (roster.get("schema") != "SR05_TURN_OBSERVATION_V1"
                            or not isinstance(roster.get("before_submit"), dict)
                            or not isinstance(roster.get("after_completion"), dict)
                            or not isinstance(roster.get("delta"), dict)):
                        problems.append("turn_observation.json:INVALID_SCHEMA")
                except (OSError, ValueError, AttributeError):
                    problems.append("turn_observation.json:INVALID_JSON")
        records.append({
            "folder": folder.name,
            "request_id": str(info.get("request_id", "")),
            "status": "PASS" if not problems else "FAIL",
            "problems": problems,
        })
    return records


def _sr05_pair_report(captures: Path) -> dict:
    """Read newest 2 SR05 captures; compare decoded content vs turn identities."""
    entries = []
    if captures.is_dir():
        for folder in sorted(captures.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            p = folder / "capture.json"
            if not p.is_file() or p.is_symlink():
                continue
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (obj.get("test_case") or {}).get("test_case") == "SR-05":
                entries.append((folder.name, obj))
    if len(entries) < 2:
        return {"status": "NEED_TWO_SR05_CAPTURES", "found": len(entries)}
    entries.sort(key=lambda x: (str(x[1].get("utc", "")), x[0]))
    (name_a, a), (name_b, b) = entries[-2:]
    aid = (a.get("turn_identity") or {}).get("current_assistant") or {}
    bid = (b.get("turn_identity") or {}).get("current_assistant") or {}
    prev_b = ((b.get("turn_identity") or {}).get("before_submit") or {}).get(
        "previous_assistant") or {}
    def eq_nonempty(first, second):
        return first == second if first and second else None
    content_same = eq_nonempty(a.get("final_content_sha256"),
                               b.get("final_content_sha256"))
    assistant_same = eq_nonempty(aid.get("durable_id_sha256"),
                                 bid.get("durable_id_sha256"))
    scope_same = eq_nonempty(a.get("scope_id_sha256"),
                              b.get("scope_id_sha256"))
    previous_links = eq_nonempty(aid.get("durable_id_sha256"),
                                  prev_b.get("durable_id_sha256"))
    different_request = (bool(a.get("request_id") and b.get("request_id"))
                         and a.get("request_id") != b.get("request_id"))
    markers_both_present = all(
        (row.get("test_case") or {}).get("status") == "MARKER_PRESENT"
        for row in (a, b)
    )
    if not markers_both_present:
        assessment = "SR05_MARKER_MISSING_IN_MODEL_CONTENT"
    elif not different_request or assistant_same is True or scope_same is True:
        assessment = "POSSIBLE_REUSED_TURN_REVIEW_REQUIRED"
    elif content_same is False:
        assessment = "MODEL_CONTENT_DIFFERS"
    elif content_same is True and assistant_same is False and scope_same is False and previous_links is True:
        assessment = "CONSISTENT_WITH_DISTINCT_TURNS"
    else:
        assessment = "INCONCLUSIVE"
    return {
        "schema": "SR05_TWO_REQUEST_COMPARISON_V1",
        "status": assessment,
        "first_capture": name_a, "second_capture": name_b,
        "distinct_request_ids": different_request,
        "both_sr05_markers_present": markers_both_present,
        "decoded_final_content_sha_equal": content_same,
        "assistant_id_sha_equal": assistant_same,
        "request_scope_sha_equal": scope_same,
        "second_previous_assistant_matches_first": previous_links,
        "note": "Hashed IDs and request scope are diagnostic evidence, not an independent screenshot.",
    }


def bundle(root: Path) -> Path:
    root = Path(root).resolve()
    evidence = root / "localdata" / "logs" / "sr-first-readback"
    captures = evidence / "captures"
    regress = root / "localdata" / "logs" / "sr-first-readback-regression"
    target_dir = evidence / "bundles"
    target_dir.mkdir(parents=True, exist_ok=True)
    integrity = _capture_integrity_report(captures)
    corrupted = [item["folder"] for item in integrity if item["status"] == "FAIL"]
    if corrupted:
        raise RuntimeError("SR_BUNDLE_FIDELITY_MISMATCH: " + ",".join(corrupted))
    sr05_pair = _sr05_pair_report(captures)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"SR_FIRST_READBACK_{stamp}_{uuid.uuid4().hex[:6]}.zip"
    entries: list[tuple[Path, str]] = []
    if captures.exists():
        for folder in sorted(captures.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for name in sorted(CAPTURE_FILES):
                p = folder / name
                if p.is_file() and not p.is_symlink():
                    entries.append((p, f"captures/{folder.name}/{name}"))
    if regress.exists():
        for folder in sorted(regress.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for name in sorted(REGRESSION_FILES):
                p = folder / name
                if p.is_file() and not p.is_symlink():
                    entries.append((p, f"regression/{folder.name}/{name}"))
    if not entries:
        raise RuntimeError(
            "SR_BUNDLE_NO_EVIDENCE: run regression / SR-02 / SR-03 before packaging"
        )
    total = 0
    manifest = []
    for p, arc in entries:
        size = p.stat().st_size
        if size > SINGLE_LIMIT:
            raise RuntimeError(f"SR_BUNDLE_FILE_TOO_LARGE: {arc}")
        total += size
        if total > TOTAL_LIMIT:
            raise RuntimeError("SR_BUNDLE_TOTAL_TOO_LARGE: remove old SR captures first")
        manifest.append((p, arc))
    try:
        git_sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        git_sha = "UNKNOWN"
    note = (
        "SR FIRST READBACK EVIDENCE (NOT A LIVE SUCCESS RECEIPT)\n"
        "Model-generated assistant response text is included verbatim.\n"
        "Inspect all extracted_response.txt and dom_turn_inner_text.txt files "
        "before sharing. Do NOT add secrets, tokens or browser profiles.\n"
        "Scope: selected request-owned assistant turn only; the separate "
        "DOM innerText may include controls and need not match extracted text.\n"
        f"git_sha={git_sha}\n"
        "No model replay/repair/Tool Action was authorized by this gate.\n"
    )
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        zf.writestr("README_REVIEW_BEFORE_UPLOAD.txt", note)
        zf.writestr("evidence_integrity.json",
                    json.dumps(integrity, ensure_ascii=False, indent=2))
        zf.writestr("sr05_pair_comparison.json",
                    json.dumps(sr05_pair, ensure_ascii=False, indent=2))
        records = []
        for path, arc in manifest:
            data = path.read_bytes()
            zf.writestr(arc, data)
            records.append({
                "file": arc, "utf8_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        zf.writestr(
            "bundle_manifest.json",
            json.dumps({
                "schema": "SR_FIRST_READBACK_BUNDLE_V1",
                "git_sha": git_sha,
                "included": records,
                "excluded": [
                    "browser_profile", "cookies", "tokens", "environment",
                    "whole_page_html", "generic_runtime_logs",
                ],
            }, ensure_ascii=False, indent=2),
        )
    # Open and verify each member immediately; a truncated ZIP is not useful.
    with zipfile.ZipFile(target, "r") as zf:
        if zf.testzip() is not None:
            raise RuntimeError("SR_BUNDLE_ZIP_CORRUPTED")
    return target


if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[3]
    if len(sys.argv) > 1:
        repo = Path(sys.argv[1]).resolve()
    archive = bundle(repo)
    print("[SR-GATE] ZIP_CREATED:", archive, flush=True)
    print("[SR-GATE] REVIEW BEFORE UPLOAD: assistant raw text is included.", flush=True)
