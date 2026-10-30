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
    "dom_selected_structure.json",
})
REGRESSION_FILES = frozenset({
    "summary.txt", "validate_shared_runtime_sr02_sr03.log",
    "validate_protocol_transport_normalization.log",
    "validate_narrative_v9_bridge.log",
    "validate_single_fence_transport_contract.log",
    "validate_protocol_recovery_freshness.log",
    "validate_protocol_recovery_readback.log",
    "validate_tri_one_5a_protocol_it.log",
    "validate_sr_first_readback_gate.log", "strict_manifest.log",
})


def bundle(root: Path) -> Path:
    root = Path(root).resolve()
    evidence = root / "localdata" / "logs" / "sr-first-readback"
    captures = evidence / "captures"
    regress = root / "localdata" / "logs" / "sr-first-readback-regression"
    target_dir = evidence / "bundles"
    target_dir.mkdir(parents=True, exist_ok=True)
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
