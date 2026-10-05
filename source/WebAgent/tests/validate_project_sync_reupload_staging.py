#!/usr/bin/env python3
"""Validate attempt-scoped Project Sync staging for selective re-uploads."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.project_sync_runner import run_project_sync_transaction
from agent_core.project_sync_protocol import (
    ProjectSyncProtocol,
    ProjectSyncProtocolError,
    build_batch_ack_contract,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload(prompt: str) -> dict:
    value = prompt.split("[PROJECT_SYNC_BATCH]\n", 1)[1].split("\n", 1)[0]
    return json.loads(value)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="smartagent-project-sync-reupload-") as temp:
        workspace = Path(temp)
        first = workspace / "part-001.txt"
        second = workspace / "part-002.txt"
        first.write_text("first bundle\n", encoding="utf-8")
        second.write_text("second bundle\n", encoding="utf-8")
        hashes = [_sha256(first), _sha256(second)]
        receipts = ["PSR1-" + "A" * 24, "PSR1-" + "B" * 24]
        plan = {
            "sync_id": "PSYNC-REUPLOAD-STAGING-TEST",
            "snapshot_id": "SNAPSHOT-REUPLOAD-STAGING-TEST",
            "batch_count": 1,
            "bundle_count": 2,
            "expected_hashes": hashes,
            "expected_receipts": receipts,
            "batches": [{
                "bundle_indexes": [1, 2],
                "bundle_hashes": hashes,
                "receipt_tokens": receipts,
                "bundle_paths": [str(first), str(second)],
                "is_final_batch": True,
            }],
        }
        binding = {
            "interface_name": "remote",
            "conversation_id": "conversation-test",
            "session_id": "session-test",
        }
        attempts: list[dict] = []

        def transport(prompt: str, attachments: list[str]):
            payload = _payload(prompt)
            paths = [Path(path) for path in attachments]
            attempts.append({"payload": payload, "paths": paths})
            assert "expected_hashes" not in payload
            assert "PROJECT_SYNC_ATTACHMENT_ALIASES" not in prompt
            for index, path in zip(payload["reupload_bundle_indexes"], paths):
                assert f"B{index:06d}-{receipts[index - 1]}" in path.name
            assert all(path.is_file() for path in paths)
            if len(attempts) == 1:
                return {
                    "type": "PROJECT_SYNC_BATCH_ACK",
                    "sync_id": plan["sync_id"],
                    "snapshot_id": plan["snapshot_id"],
                    "batch_index": 1,
                    "received_receipts": [
                        {"bundle_index": 1, "receipt_token": receipts[0]}
                    ],
                    "batch_complete": False,
                    "missing_bundle_indexes": [2],
                    "unreadable_bundle_indexes": [],
                    "incomplete_reason": "bundle 2 requires re-upload",
                    "previous_batch_ack": "",
                    "receiver_binding": binding,
                    "send_attempt_id": payload["send_attempt_id"],
                    "web_batch_ack_id": "ACK-INCOMPLETE-1",
                }
            return {
                "type": "PROJECT_SYNC_BATCH_ACK",
                "sync_id": plan["sync_id"],
                "snapshot_id": plan["snapshot_id"],
                "batch_index": 1,
                "received_receipts": [
                    {"bundle_index": 1, "receipt_token": receipts[0]},
                    {"bundle_index": 2, "receipt_token": receipts[1]},
                ],
                "batch_complete": True,
                "missing_bundle_indexes": [],
                "unreadable_bundle_indexes": [],
                "incomplete_reason": "",
                "previous_batch_ack": "",
                "receiver_binding": binding,
                "send_attempt_id": payload["send_attempt_id"],
                "web_batch_ack_id": "ACK-COMPLETE-2",
            }

        result = run_project_sync_transaction(
            workspace,
            plan,
            transport,
            interface_name=binding["interface_name"],
            conversation_id=binding["conversation_id"],
            session_id=binding["session_id"],
            request_id="REQUEST-REUPLOAD-STAGING-TEST",
        )

        assert result["status"] == "PROJECT_SYNC_READY"
        assert len(attempts) == 2
        assert [path.name for path in attempts[0]["paths"]] != [path.name for path in attempts[1]["paths"]]
        assert len(attempts[0]["paths"]) == 2
        assert len(attempts[1]["paths"]) == 1
        assert f"B000002-{receipts[1]}" in attempts[1]["paths"][0].name
        assert _sha256(attempts[1]["paths"][0]) == hashes[1]
        assert attempts[0]["payload"]["send_attempt_id"] != attempts[1]["payload"]["send_attempt_id"]
        state_path = (
            workspace / ".agents" / "project_sync_transactions"
            / f"{plan['sync_id']}.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["schema"] == "PROJECT_SYNC_PROTOCOL_STATE_V3"
        acknowledged = state["acknowledged_batches"]["1"]
        assert acknowledged["bundle_hashes"] == hashes
        assert acknowledged["received_receipts"] == [
            {"bundle_index": 1, "receipt_token": receipts[0]},
            {"bundle_index": 2, "receipt_token": receipts[1]},
        ]

        # V2 ACK state had no attachment-owned receipt proof. It must be
        # invalidated instead of authorizing a resumed V3 transaction.
        legacy_root = workspace / "legacy"
        legacy_state_path = (
            legacy_root / ".agents" / "project_sync_transactions"
            / f"{plan['sync_id']}.json"
        )
        legacy_state_path.parent.mkdir(parents=True)
        legacy_state_path.write_text(json.dumps({
            "schema": "PROJECT_SYNC_PROTOCOL_STATE_V2",
            "sync_id": plan["sync_id"],
            "snapshot_id": plan["snapshot_id"],
            "receiver_binding": binding,
            "status": "WAITING_BATCH_ACK",
            "next_batch_index": 1,
            "acknowledged_batches": {},
        }), encoding="utf-8")
        receipt_protocol = ProjectSyncProtocol(
            legacy_root, plan, receiver_binding=binding, request_id="REQUEST-V3"
        )
        assert receipt_protocol.state["schema"] == "PROJECT_SYNC_PROTOCOL_STATE_V3"
        assert receipt_protocol.state["next_batch_index"] == 1
        assert receipt_protocol.state["superseded_state"]["schema"] == "PROJECT_SYNC_PROTOCOL_STATE_V2"

        notification = receipt_protocol.next_notification("PSEND-RECEIPT-TEST")
        forged = build_batch_ack_contract(
            plan, 1, "", binding, notification["payload"]["send_attempt_id"]
        )
        forged["received_receipts"][0]["receipt_token"] = "PSR1-" + "F" * 24
        forged["web_batch_ack_id"] = "ACK-FORGED"
        try:
            receipt_protocol.accept_ack(
                forged, send_attempt_id=notification["payload"]["send_attempt_id"]
            )
        except ProjectSyncProtocolError as exc:
            assert str(exc) == "project_sync_ack_receipts_mismatch"
        else:
            raise AssertionError("forged receipt must be rejected")

        overlapping = build_batch_ack_contract(
            plan, 1, "", binding, notification["payload"]["send_attempt_id"]
        )
        overlapping["received_receipts"] = [
            {"bundle_index": 1, "receipt_token": receipts[0]}
        ]
        overlapping["batch_complete"] = False
        overlapping["missing_bundle_indexes"] = [1]
        overlapping["unreadable_bundle_indexes"] = [2]
        overlapping["incomplete_reason"] = "overlapping receiver report"
        details = receipt_protocol.incomplete_details(
            overlapping,
            notification["payload"]["send_attempt_id"],
        )
        assert details["diagnosis_valid"] is False
        assert details["target_bundle_indexes"] == [1, 2]

    print("validate_project_sync_reupload_staging: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
