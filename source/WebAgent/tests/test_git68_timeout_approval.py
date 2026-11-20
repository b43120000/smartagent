"""Git68: 600-second unanswered approval is one-shot, not a scope grant."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_core.security_approval import SecurityApprovalLedger, SecurityApprovalError


class TimeoutApprovalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        with patch("agent_core.windows_security.restricted_executor_required", return_value=False):
            self.ledger = SecurityApprovalLedger(self.root)
        self.ledger.path.parent.mkdir(parents=True, exist_ok=True)
        self.binding = {
            "request_id": "RR-TEST", "task_id": "TASK-TEST",
            "action_id": "ACT-DELETE", "action_digest": "a" * 64,
            "target": str(self.root / "item.txt"), "manifest_digest": "b" * 64,
            "chat_id": "42", "workspace_root": str(self.root),
            "permanent_scope": "", "approval_kind": "DELETE",
        }

    def start(self):
        with patch("agent_core.security_approval.time.time", return_value=1000.0):
            return self.ledger.request(self.binding, ttl_sec=600.0)

    def test_exact_600_seconds_then_consume_once(self):
        row = self.start()
        with patch("agent_core.security_approval.time.time", return_value=1599.9):
            with self.assertRaisesRegex(SecurityApprovalError, "not_elapsed"):
                self.ledger.approve_unanswered_timeout(row["approval_id"], self.binding)
        with patch("agent_core.security_approval.time.time", return_value=1600.0):
            allowed = self.ledger.approve_unanswered_timeout(row["approval_id"], self.binding)
            self.assertEqual(allowed["actor"], "AUTO_APPROVE_TIMEOUT_600S")
            self.assertTrue(allowed["auto_approved_on_timeout"])
            self.ledger.consume(row["approval_id"], self.binding)
            with self.assertRaisesRegex(SecurityApprovalError, "not_pending"):
                self.ledger.approve_unanswered_timeout(row["approval_id"], self.binding)
        self.assertEqual(self.ledger._load()["permanent_delete_workspaces"], {})

    def test_explicit_reject_wins_and_wrong_binding_fails(self):
        row = self.start()
        with patch("agent_core.security_approval.time.time", return_value=1601.0):
            with self.assertRaisesRegex(SecurityApprovalError, "binding_mismatch"):
                self.ledger.approve_unanswered_timeout(
                    row["approval_id"], {**self.binding, "target": "different"})
        with patch("agent_core.security_approval.time.time", return_value=1001.0):
            self.ledger.decide(row["approval_id"], approve=False, chat_id="42", actor="operator")
        with patch("agent_core.security_approval.time.time", return_value=1601.0):
            with self.assertRaisesRegex(SecurityApprovalError, "not_pending"):
                self.ledger.approve_unanswered_timeout(row["approval_id"], self.binding)

    def test_cancel_wins(self):
        row = self.start()
        self.ledger.clear_pending(reason="operator_cancelled")
        with patch("agent_core.security_approval.time.time", return_value=1600.0):
            with self.assertRaisesRegex(SecurityApprovalError, "not_pending"):
                self.ledger.approve_unanswered_timeout(row["approval_id"], self.binding)


if __name__ == "__main__":
    unittest.main()
