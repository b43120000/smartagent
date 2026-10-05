#!/usr/bin/env python3
"""Regression coverage for the class-specific 10 MiB source-code ceiling."""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.project_artifact_policy import MiB, POLICIES, SOURCE_CODE, TEXT_DATA_CONFIG
from agent_core.attachment_policy import resolve_attachment_policy
from agent_core.project_bundle import build_source_bundles, reconstruct_bundle
from agent_core.project_sync_transaction import build_project_sync_plan


def record(path: Path, root: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "artifact_class": SOURCE_CODE,
        "extension": path.suffix,
        "language": "cpp",
    }


def run() -> dict:
    assert POLICIES[SOURCE_CODE].max_single_file_bytes == 10 * MiB
    assert POLICIES[TEXT_DATA_CONFIG].max_single_file_bytes == 4 * MiB
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        accepted = root / "large.cpp"
        accepted.write_bytes(b"x" * (1 * MiB + 1))
        result = build_source_bundles(
            root, [record(accepted, root)], root / "bundles",
            max_bytes=500_000, max_files=50,
        )
        assert result["complete"] is True
        assert result["source_file_count"] == 1
        assert result["bundle_count"] >= 3
        for bundle in result["bundles"]:
            token = bundle["receipt_token"]
            data = Path(bundle["path"]).read_bytes()
            assert token.startswith("PSR1-") and len(token) == 29
            assert data.startswith(
                f"PROJECT_SYNC_RECEIPT_BEGIN {bundle['bundle_index']} {token}\n".encode("ascii")
            )
            assert data.endswith(
                f"\nPROJECT_SYNC_RECEIPT_END {bundle['bundle_index']} {token}\n".encode("ascii")
            )
            assert len(data) <= 500_000
            assert reconstruct_bundle(bundle["path"])
        plan = build_project_sync_plan(
            "SNAPSHOT-RECEIPT-TEST", result["bundles"], resolve_attachment_policy()
        )
        assert plan["schema"] == "PROJECT_SYNC_TRANSACTION_V2"
        assert plan["expected_receipts"] == [
            item["receipt_token"] for item in result["bundles"]
        ]

        rejected = root / "too_large.cpp"
        rejected.write_bytes(b"x" * (10 * MiB + 1))
        try:
            build_source_bundles(
                root, [record(rejected, root)], root / "rejected",
                max_bytes=500_000, max_files=50,
            )
        except ValueError as exc:
            assert "project_file_exceeds_class_limit:SOURCE_CODE:too_large.cpp" in str(exc)
        else:
            raise AssertionError("SOURCE_CODE larger than 10 MiB must fail closed")

    return {
        "source_code_limit_is_10_mib": True,
        "large_source_is_chunked": True,
        "over_10_mib_source_is_rejected": True,
        "other_class_limits_unchanged": True,
        "bundle_receipts_are_embedded_and_transaction_owned": True,
    }


if __name__ == "__main__":
    print(run())
