"""Runtime-owned multi-file deployment proof for Minimal Core v10.

This is intentionally not a generic successful-command => goal conversion.
Original request, pinned source bytes, pre-mutation preservation, current
request VERIFY ToolResult and exact working_directory must all agree.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
from collections.abc import Mapping

from agent_core.tri_one_runtime_terminal import GoalEvidence
from agent_core.target_path_contract import (
    extract_target_contract, verify_target_completion,
)
from agent_core.terminal_evidence_reconciliation import qualifying_reuse_verifier
from agent_core.security_context import SecurityContext

_MAX_SOURCE_FILE = 16 * 1024 * 1024
_MAX_SOURCE_TOTAL = 48 * 1024 * 1024


def _same_path(a: object, b: object) -> bool:
    return (bool(a and b) and
            ntpath.normcase(ntpath.normpath(str(a))) ==
            ntpath.normcase(ntpath.normpath(str(b))))


def _pinned_blob_integrity(target: str, scope, required: list[str]) -> tuple[bool, str]:
    """Verify real bytes against trusted catalog identities, not model prose.

    For the registered fixture we pin all source files to the original Git
    commit's blob identities and separately validate the manifest SHA256 inputs.
    A missing or invalid pin is a blocker, never implied integrity.
    """
    try:
        catalog_path = Path(__file__).resolve().parents[2] / "config" / "source_discovery_catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        records = [record for record in catalog.get("records", [])
                   if isinstance(record, dict) and
                   _same_path(record.get("requested_target"), target)]
        if len(records) != 1:
            return False, "PINNED_SOURCE_CATALOG_NOT_UNIQUE"
        artifact = records[0].get("artifact") or {}
        pins = artifact.get("pinned_source_blobs")
        if (not isinstance(pins, dict) or not pins or len(pins) > 64
                or not set(required).issubset(
                    {str(PurePosixPath(name).parts[0]) for name in pins})):
            return False, "PINNED_SOURCE_FILES_INCOMPLETE"
        root = Path(target)
        total = 0
        for rel, expected in pins.items():
            relative = PurePosixPath(rel)
            if (relative.is_absolute() or not relative.parts or
                    any(part in {".", ".."} for part in relative.parts) or
                    not all(part and part not in {"\\", "/"} for part in relative.parts) or
                    not isinstance(expected, str) or len(expected) != 40 or
                    any(c not in "0123456789abcdef" for c in expected)):
                return False, "PINNED_SOURCE_PATH_OR_BLOB_INVALID"
            item = root.joinpath(*relative.parts)
            resolved = scope.require_read(str(item), require_absolute=True)
            info = resolved.lstat()
            import stat
            if (not stat.S_ISREG(info.st_mode) or
                    bool(getattr(info, "st_file_attributes", 0) & 0x400) or
                    info.st_size > _MAX_SOURCE_FILE or
                    total + info.st_size > _MAX_SOURCE_TOTAL):
                return False, "SOURCE_CONTENT_TYPE_OR_SIZE_UNVERIFIED"
            payload = resolved.read_bytes()
            total += len(payload)
            blob = hashlib.sha1(
                b"blob " + str(len(payload)).encode("ascii") + b"\x00" + payload
            ).hexdigest()
            if blob != expected:
                return False, "PINNED_SOURCE_BLOB_MISMATCH"
        if "fixture_manifest.json" in required:
            manifest_path = scope.require_read(
                str(root / "fixture_manifest.json"), require_absolute=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            hashes = manifest.get("source_input_hashes")
            if not isinstance(hashes, dict) or not hashes:
                return False, "SOURCE_INPUT_HASH_MANIFEST_INVALID"
            for rel, want in hashes.items():
                if (rel not in pins or not isinstance(want, str) or len(want) != 64 or
                        any(c not in "0123456789abcdef" for c in want)):
                    return False, "SOURCE_INPUT_HASH_ENTRY_INVALID"
                source = root.joinpath(*PurePosixPath(rel).parts)
                data = scope.require_read(str(source), require_absolute=True).read_bytes()
                if hashlib.sha256(data).hexdigest() != want:
                    return False, "SOURCE_INPUT_SHA256_MISMATCH"
        return True, "PINNED_SOURCE_BYTES_VERIFIED"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False, "SOURCE_CONTENT_INTEGRITY_UNAVAILABLE"


def evaluate_deployment_goal(runtime, goal: str, history: list[dict]) -> GoalEvidence | None:
    """A Goal Proof only after a real independent VERIFY in exact target CWD."""
    contract = getattr(runtime, "_target_path_contract", None)
    if not isinstance(contract, Mapping):
        contract = extract_target_contract(goal)
    if not isinstance(contract, Mapping) or not contract.get("required_top_level_entries"):
        return None
    target = str(contract.get("requested_target") or "")
    required = list(contract.get("required_top_level_entries") or [])
    if (contract.get("binding_status") != "BOUND" or not target or
            not required or not all(isinstance(x, str) for x in required)):
        return GoalEvidence("UNKNOWN", "DEPLOYMENT_TARGET_CONTRACT_INVALID")
    baseline = getattr(runtime, "_target_pre_mutation", None)
    if not isinstance(baseline, Mapping):
        return GoalEvidence("UNKNOWN", "PRE_MUTATION_BASELINE_NOT_CAPTURED")
    if baseline.get("state") not in {"ABSENT", "EMPTY", "OCCUPIED"}:
        return GoalEvidence("UNKNOWN", "PRE_MUTATION_BASELINE_NOT_CONFIRMED")

    for idx in range(len(history) - 1, -1, -1):
        row = history[idx]
        action, receipt = row.get("action") or {}, row.get("receipt") or {}
        if not (action.get("tool") == "run_command" and
                action.get("operation") == "VERIFY" and
                _same_path(action.get("working_directory"), target) and
                action.get("verifies_action_id") and action.get("verify") and
                receipt.get("status") == "SUCCEEDED" and
                receipt.get("executed") is True and
                (receipt.get("data") or {}).get("verification_status") == "PASS"):
            continue
        # No later unverified changes may invalidate an earlier VERIFY.
        if any(next_row.get("action", {}).get("tool") not in {
                    "list_directory", "inspect_directory", "observe_fact", "read_file"
                } for next_row in history[idx + 1:]):
            return GoalEvidence("UNKNOWN", "ACTION_AFTER_INDEPENDENT_VERIFY")
        verified_target = action["verifies_action_id"]
        if not any(prior.get("action", {}).get("action_id") == verified_target and
                   prior.get("receipt", {}).get("executed") is True and
                   prior.get("receipt", {}).get("status") == "SUCCEEDED"
                   for prior in history[:idx]):
            return GoalEvidence("UNKNOWN", "VERIFY_SOURCE_ACTION_NOT_OBSERVED")
        action_id = str(action.get("action_id") or "")
        results = getattr(runtime, "action_result_ledger", None) or {}
        evidence = results.get(action_id) or {}
        if (not action_id or evidence.get("request_id") != runtime.run_id or
                evidence.get("task_epoch") != runtime.task_epoch or
                evidence.get("evidence_status") != "ACTIVE" or
                evidence.get("verification_status") != "PASS"):
            return GoalEvidence("UNKNOWN", "VERIFY_LEDGER_IDENTITY_OR_STATUS_INVALID")
        actions = getattr(runtime, "action_ledger", None) or {}
        reuse_ref = qualifying_reuse_verifier(
            actions, results, [action_id], target, runtime.run_id, runtime.task_epoch)
        if baseline["state"] == "OCCUPIED" and not reuse_ref:
            return GoalEvidence("UNKNOWN", "EXACT_ROOT_REUSE_VERIFY_NOT_LINKED")
        scope = SecurityContext.from_agent(
            runtime.tools, fallback_workspace=runtime.workspace)
        verdict = verify_target_completion(
            contract, baseline, scope, evidence_pass=True,
            reuse_verification_ref=reuse_ref)
        if verdict.get("status") != "PASS":
            return GoalEvidence("UNKNOWN",
                                "DEPLOYMENT_TARGET_" + str(verdict.get("reason") or "UNVERIFIED"))
        integrity, reason = _pinned_blob_integrity(target, scope, required)
        if not integrity:
            return GoalEvidence("UNKNOWN", reason)
        return GoalEvidence(
            "PASS", "EXACT_ROOT_PINNED_CONTENT_INDEPENDENTLY_VERIFIED",
            (action_id,), target, "DEPLOYMENT_CONTENT_VERIFIED")
    return GoalEvidence("UNKNOWN", "EXACT_ROOT_INDEPENDENT_VERIFY_REQUIRED")
