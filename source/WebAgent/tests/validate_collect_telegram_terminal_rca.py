"""Windows collector regression: real collect.bat ZIP, Telegram terminal and privacy.

No Telegram API, network, browser, tokens or user project is involved.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def event(*, eid: str, task: str, request: str, transport="TELEGRAM",
          delivery="DELIVERED", summary="已完成", timestamp=50.0):
    return {
        "event_id": eid,
        "event_type": "TASK_COMPLETED",
        "task_id": task,
        "request_id": request,
        "status": "COMPLETED",
        "reply_route": {
            "transport": transport,
            "chat_id": 998877665544,
            "message_id": 881100,
        },
        "payload": {
            "summary": summary,
            "task_outcome": "SUCCESS",
            "terminal_acceptance": {
                "admission_status": "ACCEPTED",
                "decision": "COMPLETE",
                "outcome": "SUCCESS",
                "runtime_state": "COMPLETED",
                "receipt_id": "TERM-ABCDE123",
            },
            "delivery_receipt": {
                "receipt_id": "DREC-ABC123",
                "transport": transport,
                "receipt_kind": "RUNTIME_ADAPTER_ACK",
                "adapter_confirmed": delivery == "DELIVERED",
                "reconciled_observed": False,
                "message_count": 1,
                "provider_message_id_count": 1,
                "provider_message_ids_sha256": "1" * 64,
            },
        },
        "delivery_state": delivery,
        "created_at": timestamp,
        "delivered_at": 51.0 if delivery == "DELIVERED" else 0.0,
        "attempts": 1,
        "last_error": "DONOT_EXPORT_ERROR_SECRETS",
    }


class CollectTelegramRcaIT(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            self.skipTest("collect.bat and Windows PowerShell 5.1 are required")
        self.tmp = tempfile.TemporaryDirectory(prefix="sa-collect-tg-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copyfile(REPO / "collect.bat", self.root / "collect.bat")
        stub = self.root / "source" / "WebAgent" / "protocol_loop.py"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("pass\n", encoding="utf-8")
        self.event_file = self.root / "localdata" / "runtime" / "remote_events.json"
        self.event_file.parent.mkdir(parents=True, exist_ok=True)

    def collect(self, events, *, task="TASK-COLLECT-TG-A", request="RR-COLLECT-TG-A"):
        if events is not None:
            self.event_file.write_text(json.dumps({"version": 1, "events": {
                row["event_id"]: row for row in events
            }}, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(self.root / "collect.bat"), task, request],
            cwd=str(self.root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        zipped = sorted((self.root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
        self.assertEqual(len(zipped), 1, zipped)
        with zipfile.ZipFile(zipped[0]) as archive:
            names = set(archive.namelist())
            self.assertIn("telegram_terminal_summary.json", names)
            self.assertIn("telegram_final_reply_redacted.txt", names)
            details = json.loads(archive.read("telegram_terminal_summary.json").decode("utf-8-sig"))
            response = archive.read("telegram_final_reply_redacted.txt").decode("utf-8-sig")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            entire = b"".join(archive.read(name) for name in names)
        return details, response, manifest, entire

    def test_task_request_bound_delivered_reply_redacts_secrets(self):
        task, request = "TASK-COLLECT-TG-A", "RR-COLLECT-TG-A"
        secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        body = (
            "結果／中斷原因：實際完成。\n"
            "狀態：COMPLETE/SUCCESS\n任務結果：🟢 已完成\n"
            "Terminal Receipt: TERM-ABCDE123\n"
            "BOT_TOKEN=" + secret + "\n"
            "Authorization: Bearer abcdefghijklmnopqrstuv\n"
            "https://example.invalid/?api_key=secretquery2026\n"
            "收基測資：保留繁體中文與行分隔。"
        )
        data, text, manifest, entire = self.collect([
            event(eid="EVT-OTHER-REQUEST", task=task, request="RR-FOREIGN", summary="FOREIGN_SAME_TASK"),
            event(eid="EVT-OTHER-TASK", task="TASK-FOREIGN", request=request, summary="FOREIGN_SAME_REQUEST"),
            event(eid="EVT-WEB", task=task, request=request, transport="WEBGPT", summary="FOREIGN_WEBGPT"),
            event(eid="EVT-TG-FINAL", task=task, request=request, summary=body),
        ])
        self.assertTrue(data["found"], data)
        self.assertTrue(data["request_task_bound"], data)
        self.assertTrue(data["delivery_confirmed"], data)
        self.assertTrue(data["summary_present"], data)
        self.assertFalse(data["summary_truncated"], data)
        self.assertGreaterEqual(data["redaction_count"], 2, data)
        self.assertEqual(data["event_id"], "EVT-TG-FINAL")
        self.assertEqual(data["terminal_acceptance"]["receipt_id"], "TERM-ABCDE123")
        self.assertEqual(data["delivery_receipt"]["receipt_id"], "DREC-ABC123")
        self.assertIn("結果／中斷原因：實際完成。", text)
        self.assertIn("收基測資：保留繁體中文與行分隔。", text)
        self.assertIn("REDACTED", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("secretquery2026", text)
        self.assertNotIn("FOREIGN", text)
        self.assertNotIn("chat_id", data)
        self.assertTrue(manifest["telegram_terminal_found"])
        self.assertTrue(manifest["telegram_delivery_confirmed"])
        for forbidden in (
            secret.encode(), b"secretquery2026", b"FOREIGN_SAME_TASK",
            b"FOREIGN_SAME_REQUEST", b"FOREIGN_WEBGPT", b"DONOT_EXPORT_ERROR_SECRETS",
            b"998877665544",
        ):
            self.assertNotIn(forbidden, entire, forbidden)

    def test_completed_task_recovers_request_from_exact_ledger_and_keeps_action_reason(self):
        task, request = "TASK-COLLECT-TG-A", "RR-COLLECT-TG-A"
        recovery = self.root / "localdata" / "runtime" / "prerequisite_v3_recovery"
        recovery.mkdir(parents=True, exist_ok=True)
        (recovery / (task + ".json")).write_text(json.dumps({
            "schema": "SMARTAGENT_PREREQ_V3_BCD_LEDGER_V1",
            "identity": {"task_id": task, "request_id": request},
        }), encoding="utf-8")
        events = self.root / "localdata" / "logs" / "remote_runtime.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(json.dumps({
            "event": "STATUS", "stage": "action_evidence_recorded",
            "task_id": task, "request_id": request, "action_id": "READ-ABSENT",
            "tool": "read_file", "result_status": "FAILED",
            "execution_status": "FAILED", "result_reason": "FILE_NOT_FOUND",
            "executed": False, "timestamp": 1793719100.0,
        }) + "\n", encoding="utf-8")
        details, _, manifest, _ = self.collect([
            event(eid="EVT-TG-FINAL", task=task, request=request),
        ], request="")
        self.assertTrue(details["request_task_bound"], details)
        self.assertEqual(manifest["request_id"], request)
        zipped = next((self.root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
        with zipfile.ZipFile(zipped) as archive:
            timeline = json.loads(archive.read("timeline.json").decode("utf-8-sig"))
        matches = [row for row in timeline if row.get("event") == "action_evidence_recorded"]
        self.assertEqual(len(matches), 1, timeline)
        self.assertEqual(matches[0]["result_reason"], "FILE_NOT_FOUND")
        self.assertIs(matches[0]["executed"], False)

    def test_pending_delivery_never_claims_confirmed(self):
        data, text, manifest, _ = self.collect([
            event(eid="EVT-TG-PENDING", task="TASK-COLLECT-TG-A",
                  request="RR-COLLECT-TG-A", delivery="READY",
                  summary="任務尚未確定送達。")
        ])
        self.assertTrue(data["found"])
        self.assertFalse(data["delivery_confirmed"])
        self.assertFalse(manifest["telegram_delivery_confirmed"])
        self.assertIn("任務尚未確定送達", text)

    def test_missing_telegram_source_is_explicit_not_found(self):
        data, text, manifest, _ = self.collect(None)
        self.assertFalse(data["found"])
        self.assertEqual(data["selection"], "SOURCE_MISSING")
        self.assertFalse(data["delivery_confirmed"])
        self.assertFalse(manifest["telegram_terminal_found"])
        self.assertIn("TELEGRAM_TERMINAL_NOT_FOUND", text)

    def test_mismatched_request_cannot_pull_other_task_terminal(self):
        data, text, manifest, _ = self.collect([
            event(eid="EVT-TG-OTHER", task="TASK-COLLECT-TG-A",
                  request="RR-SOMEONE-ELSE", summary="PRIVATE_OTHER_REQUEST")
        ])
        self.assertFalse(data["found"])
        self.assertFalse(manifest["telegram_terminal_found"])
        self.assertNotIn("PRIVATE_OTHER_REQUEST", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
