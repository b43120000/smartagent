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


class TimedDeleteIntegration(unittest.TestCase):
    """Isolated temp-file execution; no production targets or live Telegram."""

    def _run(self, *, reject=False, auto_enabled=True):
        from WebAgent.tool_context import WebAgentToolContext
        from agent_core.tool_result_contract import decode_result
        with tempfile.TemporaryDirectory(prefix="git68-delete-timeout-") as tmp:
            root = Path(tmp).resolve()
            target = root / "disposable.txt"
            target.write_text("ONLY_DELETE_THIS_ISOLATED_FIXTURE", encoding="utf-8")
            ctx = WebAgentToolContext(root, interface_name="remote")
            ctx.begin_run("RR-GIT68-DELETE", "Delete isolated test file " + str(target),
                          [str(root)])
            ctx.current_task_id = "TASK-GIT68-DELETE"
            ctx.security_approval_ledger_root = root
            requests = []

            def notify(record, manifest):
                requests.append(record["approval_id"])
                if reject:
                    SecurityApprovalLedger(root).decide(
                        record["approval_id"], approve=False,
                        chat_id="42", actor="test-human-reject",
                    )

            ctx.configure_security_approval(notify, chat_id="42")
            access = {
                "policy": {
                    "revision": 68, "auto_approve_requested": auto_enabled,
                    "turbo_requested": False, "denied_write_roots": [],
                },
                "writable_workspaces": (str(root),), "read_only_roots": (),
            }
            current = [1000.0]

            def advance(_):
                # Simulate elapsed seconds without a real 10-minute wait.
                current[0] += 60.0

            with patch("agent_core.auto_approve_mode.access_snapshot",
                       return_value=access), patch(
                "agent_core.auto_approve_mode.acl_mode", return_value="off"
            ), patch("agent_core.turbo_broker_admission.assess",
                     return_value={"decision": "NORMAL", "evidence": {},
                                   "reason_code": "TURBO_DISABLED"}), patch(
                "agent_core.tools.time.time", side_effect=lambda: current[0]
            ), patch("agent_core.tools.time.sleep", side_effect=advance):
                result = decode_result(ctx.execute({
                    "tool": "delete_path", "action_id": "ACT-DELETE-GIT68",
                    "path": str(target), "recursive": False,
                    "reason": "Only remove the isolated disposable test file",
                }))
            return result, target.exists(), requests, current[0]

    def test_no_click_600_seconds_deletes_only_fixture(self):
        result, exists, requests, ended = self._run()
        self.assertEqual(len(requests), 1)
        self.assertGreaterEqual(ended, 1600.0)
        self.assertEqual(result["status"], "SUCCEEDED", result)
        self.assertTrue(result["executed"])
        self.assertFalse(exists)
        self.assertEqual(result["data"]["authorization"],
                         "AUTO_APPROVE_TIMEOUT_ONCE")

    def test_human_reject_is_never_overridden(self):
        result, exists, requests, _ = self._run(reject=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["status"], "REJECTED", result)
        self.assertTrue(exists)

    def test_auto_off_timeout_never_deletes(self):
        result, exists, requests, _ = self._run(auto_enabled=False)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["status"], "REJECTED", result)
        self.assertTrue(exists)


if __name__ == "__main__":
    unittest.main()
