"""Deterministic data model and batching for project synchronization."""
from __future__ import annotations

import hashlib
import json
from typing import Sequence

from .attachment_policy import AttachmentPolicy


def build_project_sync_plan(
    snapshot_id: str,
    source_bundles: Sequence[dict],
    policy: AttachmentPolicy,
) -> dict:
    bundles = sorted(source_bundles, key=lambda item: int(item["bundle_index"]))
    indexes = [int(item["bundle_index"]) for item in bundles]
    expected_indexes = list(range(1, len(bundles) + 1))
    if indexes != expected_indexes:
        raise ValueError("project_sync_bundle_indexes_not_contiguous")
    hashes = [str(item.get("sha256", "")) for item in bundles]
    if any(len(value) != 64 for value in hashes):
        raise ValueError("project_sync_bundle_hash_invalid")
    receipts = [str(item.get("receipt_token", "")) for item in bundles]
    if any(not value.startswith("PSR1-") or len(value) != 29 for value in receipts):
        raise ValueError("project_sync_bundle_receipt_invalid")
    per_message = policy.max_attachments_per_message
    batch_count = (len(bundles) + per_message - 1) // per_message
    if batch_count > policy.max_batches:
        raise ValueError(
            f"project_sync_batch_limit_exceeded:{batch_count}>{policy.max_batches}"
        )
    identity = {
        "snapshot_id": str(snapshot_id),
        "bundle_indexes": indexes,
        "bundle_hashes": hashes,
        "bundle_receipts": receipts,
        "max_attachments_per_message": per_message,
    }
    sync_id = "PSYNC-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20].upper()
    batches = []
    for offset in range(0, len(bundles), per_message):
        chunk = bundles[offset : offset + per_message]
        batch_index = len(batches) + 1
        batches.append(
            {
                "batch_index": batch_index,
                "batch_count": batch_count,
                "bundle_indexes": [int(item["bundle_index"]) for item in chunk],
                "bundle_paths": [str(item["path"]) for item in chunk],
                "bundle_hashes": [str(item["sha256"]) for item in chunk],
                "receipt_tokens": [str(item["receipt_token"]) for item in chunk],
                "is_final_batch": batch_index == batch_count,
            }
        )
    return {
        "schema": "PROJECT_SYNC_TRANSACTION_V2",
        "sync_id": sync_id,
        "snapshot_id": str(snapshot_id),
        "bundle_count": len(bundles),
        "expected_hashes": hashes,
        "expected_receipts": receipts,
        "batch_count": batch_count,
        "batches": batches,
        "local_status": "LOCAL_READY",
        "sync_status": "LOCAL_READY",
    }


__all__ = ["build_project_sync_plan"]
