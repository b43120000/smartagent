"""Offline regressions for immutable deployment target and terminal path admission."""
from __future__ import annotations

import sys
import tempfile
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.target_path_contract import (
    extract_target_contract, observe_target, verify_target_completion,
)
from agent_core.terminal_evidence_reconciliation import (
    capture_existing_tree, reconcile_existing_tree,
)


class ReadScope:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def require_read(self, path, *, require_absolute=False):
        candidate = Path(path)
        if require_absolute and not candidate.is_absolute():
            raise ValueError("absolute_path_required")
        result = candidate.resolve(strict=False)
        if not result.is_relative_to(self.root):
            raise PermissionError("outside_authorized_scope")
        return result


class TargetPathContractRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.destination = self.root / "deployment"
        self.scope = ReadScope(self.root)
        self.requirements = ("case_a", "case_b", "case_c")

    def contract(self, *, exception=True, include_entries=True):
        base = f"預期部署根目錄：{self.destination}\n"
        if include_entries:
            base += "預期包含：case_a、case_b、case_c\n"
        if exception:
            base += "如果目標已經存在且會覆蓋資料，請選擇新的獨立目錄並明確回報。\n"
        parsed = extract_target_contract("請將測試包部署完成。\n" + base)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["requested_target"], str(self.destination))
        return parsed

    def create_children(self, path=None):
        target = path or self.destination
        target.mkdir(parents=True, exist_ok=True)
        for name in self.requirements:
            (target / name).mkdir(exist_ok=True)

    def verify(self, contract, baseline, *, reported=""):
        return verify_target_completion(
            contract, baseline, self.scope, final_text=reported,
            evidence_pass=True)

    def test_immutable_explicit_target_and_no_implicit_path_binding(self):
        c = self.contract()
        self.assertEqual(c["binding_status"], "BOUND")
        self.assertTrue(c["relocation_permitted_if_collision"])
        self.assertEqual(c["required_top_level_entries"], list(self.requirements))
        self.assertFalse(c["authorization_granted"])
        self.assertIsNone(extract_target_contract(
            f"研究 {self.destination} 的腳本，請解釋內容。"))

    def test_explicit_natural_language_destination_is_bound(self):
        for request in (
            f"請把測試包部署到 {self.destination}",
            f"請在 {self.destination} 下部署測試包",
            f"Deploy to {self.destination}",
        ):
            contract = extract_target_contract(request)
            self.assertIsNotNone(contract, request)
            self.assertEqual(contract["requested_target"],
                             str(self.destination), contract)

    def test_readonly_deployment_inspection_does_not_bind_mutation_gate(self):
        c = extract_target_contract(
            f"請唯讀檢查部署結果。預期部署根目錄：{self.destination}")
        self.assertIsNone(c)

    def test_preexisting_contents_removed_does_not_pass_terminal(self):
        c = self.contract()
        self.destination.mkdir()
        old = self.destination / "important.txt"
        old.write_text("user data")
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        old.unlink()
        self.create_children()
        result = self.verify(c, pre)
        self.assertEqual(result["reason"], "PREEXISTING_ENTRY_REMOVED")
        self.assertEqual(result["preservation_status"], "UNVERIFIED")


    def test_nested_directory_mtime_change_with_all_original_bytes_preserved(self):
        c = self.contract()
        previous = self.destination / "case_d_deploy_20261106_01"
        original = previous / "SmartAgent_GoalDriven_IT" / "case_a"
        original.mkdir(parents=True)
        (original / "original.csv").write_text("camera,12345\\n", encoding="utf-8")
        pre = observe_target(str(self.destination), self.scope,
                             capture_preexisting=True)
        self.assertEqual(pre["preservation_baseline"]["status"], "READY")
        # New child changes the old parent directory's timestamp and size,
        # but is not damage to its existing entries.
        (original / "new_report.txt").write_text("new,not overwrite", encoding="utf-8")
        before = pre["entries"][previous.name]["mtime_ns"]
        os.utime(previous, ns=(before + 5_000_000_000,
                               before + 5_000_000_000))
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertEqual(outcome["status"], "PASS", outcome)
        self.assertEqual(outcome["preservation_status"], "VERIFIED")
        self.assertEqual(outcome["reason"], "REQUESTED_ROOT_VERIFIED")
        self.assertGreater(outcome["metadata_only_directory_changes"], 0)

    def test_old_file_same_length_same_mtime_but_modified_bytes_rejected(self):
        c = self.contract()
        self.destination.mkdir()
        original = self.destination / "important.txt"
        original.write_bytes(b"aaaa")
        pre = observe_target(str(self.destination), self.scope,
                             capture_preexisting=True)
        stamp = original.stat().st_mtime_ns
        original.write_bytes(b"bbbb")
        os.utime(original, ns=(stamp, stamp))
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertEqual(outcome["status"], "UNVERIFIED")
        self.assertEqual(outcome["reason"], "PREEXISTING_FILE_CONTENT_CHANGED")
        self.assertEqual(outcome["preservation_status"], "UNVERIFIED")

    def test_nested_original_file_removed_fails_closed(self):
        c = self.contract()
        original = self.destination / "saved" / "keep.csv"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"keep me")
        pre = observe_target(str(self.destination), self.scope,
                             capture_preexisting=True)
        original.unlink()
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertEqual(outcome["reason"], "PREEXISTING_ENTRY_REMOVED")

    def test_old_top_level_file_replaced_with_directory_does_not_pass(self):
        c = self.contract()
        original = self.destination / "userfile"
        original.parent.mkdir()
        original.write_bytes(b"user-data")
        pre = observe_target(str(self.destination), self.scope,
                             capture_preexisting=True)
        original.unlink()
        original.mkdir()
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertIn(outcome["reason"], {
            "PREEXISTING_ENTRY_TYPE_CHANGED", "PREEXISTING_ENTRY_REPLACED"})
        self.assertNotEqual(outcome["status"], "PASS")

    def test_unattested_preexisting_tree_stays_unverified(self):
        c = self.contract()
        old = self.destination / "legacy" / "large.bin"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"q" * 1024)
        pre = observe_target(str(self.destination), self.scope,
                             capture_preexisting=True)
        bad = capture_existing_tree(self.destination, self.scope, max_bytes=8)
        self.assertEqual(bad["status"], "UNVERIFIED")
        pre["preservation_baseline"] = bad
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertEqual(outcome["status"], "UNVERIFIED")
        self.assertEqual(outcome["reason"],
                         "PREEXISTING_CONTENT_EVIDENCE_UNVERIFIED")

    def test_snapshot_cannot_be_created_after_mutation_and_used_as_preproof(self):
        c = self.contract()
        original = self.destination / "legacy"
        original.mkdir(parents=True)
        pre = observe_target(str(self.destination), self.scope)
        self.create_children()
        outcome = self.verify(c, pre)
        self.assertEqual(outcome["reason"],
                         "PREEXISTING_CONTENT_EVIDENCE_UNVERIFIED")

    def test_hash_proof_only_checks_preexisting_file_bytes(self):
        old = self.destination / "legacy" / "keep.txt"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"stable")
        proof = capture_existing_tree(self.destination, self.scope)
        self.assertEqual(proof["status"], "READY")
        (old.parent / "newchild.txt").write_bytes(b"added")
        check = reconcile_existing_tree(self.destination, self.scope, proof)
        self.assertEqual(check["status"], "VERIFIED", check)
        old.write_bytes(b"damage")
        check = reconcile_existing_tree(self.destination, self.scope, proof)
        self.assertNotEqual(check["status"], "VERIFIED")

    def test_runtime_bookkeeping_alone_does_not_count_as_deployment(self):
        c = self.contract(include_entries=False)
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.destination.mkdir()
        (self.destination / ".agents").mkdir()
        (self.destination / ".cache").mkdir()
        result = self.verify(c, pre)
        self.assertNotEqual(result["status"], "PASS")

    def test_absent_target_direct_deployment_passes(self):
        c = self.contract()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.assertEqual(pre["state"], "ABSENT")
        self.create_children()
        result = self.verify(c, pre)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["actual_target"], str(self.destination))
        self.assertEqual(result["relocation_justification"], "NONE")

    def test_absent_root_nested_wrapper_cannot_claim_success(self):
        c = self.contract()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.create_children(self.destination / "case_d_deploy_01" / "SmartAgent_GoalDriven_IT")
        result = self.verify(c, pre)
        self.assertNotEqual(result["status"], "PASS")
        self.assertEqual(result["reason"], "OVERWRITE_RISK_NOT_ESTABLISHED")

    def test_existing_parent_with_unrelated_content_is_not_collision(self):
        c = self.contract()
        self.destination.mkdir()
        (self.destination / "old").mkdir()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.create_children(self.destination / "new_isolated" / "package")
        result = self.verify(c, pre, reported=str(self.destination / "new_isolated" / "package"))
        self.assertEqual(result["reason"], "OVERWRITE_RISK_NOT_ESTABLISHED")

    def test_preexisting_collision_relocation_requires_disclosure(self):
        c = self.contract()
        self.destination.mkdir()
        (self.destination / "case_a").mkdir()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        actual = self.destination / "new_isolated" / "package"
        self.create_children(actual)
        concealed = self.verify(c, pre)
        self.assertEqual(concealed["reason"], "RELOCATION_NOT_DISCLOSED_TO_USER")
        reported = self.verify(c, pre, reported=f"部署位置：{actual}")
        self.assertEqual(reported["status"], "PASS", reported)
        self.assertEqual(reported["actual_target"], str(actual))
        self.assertEqual(reported["relocation_justification"], "PREEXISTING_ENTRY_COLLISION")

    def test_relocation_without_original_exception_is_denied(self):
        c = self.contract(exception=False)
        self.destination.mkdir()
        (self.destination / "case_a").mkdir()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        actual = self.destination / "new_isolated"
        self.create_children(actual)
        result = self.verify(c, pre, reported=str(actual))
        self.assertEqual(result["reason"], "DEPLOYMENT_LAYOUT_NOT_AT_REQUESTED_ROOT")

    def test_direct_deployment_into_occupied_parent_can_pass_without_deletion(self):
        c = self.contract()
        self.destination.mkdir()
        (self.destination / "unrelated_old_folder").mkdir()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.create_children()
        result = self.verify(c, pre)
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue((self.destination / "unrelated_old_folder").is_dir())

    def test_existing_unchanged_content_is_not_a_new_deployment(self):
        c = self.contract()
        self.create_children()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        result = self.verify(c, pre)
        self.assertEqual(result["reason"],
                         "UNCHANGED_EXISTING_CONTENT_REQUIRES_INDEPENDENT_REUSE_PROOF")

    def test_invalid_preflight_and_no_verified_actions_fail_closed(self):
        c = self.contract()
        pre = observe_target(str(self.destination), self.scope, capture_preexisting=True)
        self.create_children()
        unverified = verify_target_completion(c, pre, self.scope,
                                               evidence_pass=False)
        self.assertEqual(unverified["reason"], "INDEPENDENT_ACTION_VERIFICATION_REQUIRED")
        denied = self.verify(c, {"state": "UNKNOWN", "reason": "PermissionError"})
        self.assertEqual(denied["reason"], "PRE_MUTATION_EVIDENCE_UNVERIFIED")

    def test_unknown_scope_and_ambiguous_contract_fail_closed(self):
        c = self.contract()
        bad = dict(c, binding_status="AMBIGUOUS", requested_target="")
        self.assertEqual(self.verify(bad, {})["reason"], "REQUEST_TARGET_AMBIGUOUS")
        observed = observe_target(str(self.root.parent / (self.root.name + "_outside")), self.scope)
        self.assertEqual(observed["state"], "UNKNOWN")
        self.assertEqual(observed["reason"], "PermissionError")


if __name__ == "__main__":
    unittest.main()
