"""Non-authorizing, evidence-bound GitHub source-stage diagnostics.

Never treats remote refs as pinned-commit proof or local hashes as remote
blob proof. Each stage is independent and never grants tool admission.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping


def github_source_stage_evidence(action: Mapping, receipt: Mapping,
                                 evidence_ref: str, *, prior_stages=()) -> dict | None:
    if action.get("tool") != "run_command":
        return None
    command = str(action.get("command") or "")
    data = receipt.get("data")
    if not isinstance(data, Mapping):
        return None
    is_git = bool(re.search(r"(?i)\bgit(?:\.exe)?\b", command))
    is_hash = bool(re.search(r"(?i)\bGet-FileHash\b", command))
    if not is_git and not is_hash:
        return None
    ancestry = re.fullmatch(
        r"(?i)git\s+(?:-C\s+(?:'[^'\r\n]+'|\"[^\"\r\n]+\")\s+)?"
        r"merge-base\s+--is-ancestor\s+([0-9a-f]{40})\s+([0-9a-f]{40})",
        command.strip(),
    )
    if is_git and ancestry:
        stage = "LOCAL_COMMIT_ANCESTRY"
    elif is_git and re.search(r"(?i)\bls-remote\b", command):
        stage = "REMOTE_REFS"
    elif is_git and re.search(r"(?i)\bfetch\b", command):
        stage = "REMOTE_FETCH"
    elif is_git and re.search(r"(?i)\bcat-file\s+-t\s+[0-9a-f]{40}\b", command):
        stage = "LOCAL_COMMIT_OBJECT"
    elif is_git and re.search(r"(?i)\bls-tree\b", command):
        stage = "PINNED_TREE_BLOB"
    elif is_git and re.search(r"(?i)\bhash-object\b", command):
        stage = "LOCAL_BLOB_HASH"
    elif is_hash:
        stage = "LOCAL_ARCHIVE_SHA256"
    else:
        return None
    stdout = str(data.get("stdout") or "").strip()
    stderr = str(data.get("stderr") or "")
    executed = receipt.get("executed") is True
    result = {
        "schema": "SMARTAGENT_GITHUB_SOURCE_STAGE_EVIDENCE_V1",
        "evidence_ref": evidence_ref, "stage": stage,
        "executed": executed, "stage_verified": False,
        "status": "NOT_VERIFIED",
        "dns_status": "NOT_OBSERVED",
        "repo_read_status": "NOT_OBSERVED",
        "credential_identity": "NOT_OBSERVED",
        "pinned_commit_status": "NOT_VERIFIED",
        "blob_identity_status": "NOT_VERIFIED",
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
    }
    if not executed:
        result["status"] = "NOT_EXECUTED"
        return result
    error = (stderr + "\n" + stdout).casefold()
    if receipt.get("status") == "FAILED" or (
        type(data.get("exit_code")) is int and data["exit_code"] != 0
    ):
        if "could not resolve host" in error or "name or service not known" in error:
            result["status"] = "DNS_RESOLUTION_FAILED"
            result["dns_status"] = "FAILED"
        elif ("authentication failed" in error
              or "permission denied (publickey)" in error
              or "could not read username" in error
              or "repository not found" in error):
            result["status"] = "REPOSITORY_ACCESS_FAILED"
            result["repo_read_status"] = "FAILED"
        else:
            result["status"] = "COMMAND_FAILED_OTHER"
        return result
    if receipt.get("status") != "SUCCEEDED" or data.get("exit_code") != 0:
        return result
    verified = False
    if stage == "REMOTE_REFS":
        refs = [
            line for line in stdout.splitlines()
            if re.fullmatch(r"[0-9a-fA-F]{40}\s+(?:HEAD|refs/[^\s]+)", line)
        ]
        verified = bool(refs)
        if verified:
            result["dns_status"] = "RESOLVED_THIS_REQUEST"
            result["repo_read_status"] = "REMOTE_REFS_READ"
            result["ref_count"] = len(refs)
            # Only the positively classified exact HTTPS HEAD read may
            # anchor a later local ancestry result to a remote repository.
            from agent_core.action_resolution_trace import (
                exact_remote_git_read_repository,
            )
            repository = exact_remote_git_read_repository(command)
            heads = [
                line.split()[0].lower()
                for line in refs if line.split()[-1] == "HEAD"
            ]
            if repository and len(heads) == 1:
                result["repository"] = repository
                result["remote_head_sha"] = heads[0]
    elif stage == "LOCAL_COMMIT_ANCESTRY":
        # Exit 0 from Git's --is-ancestor verifies the local commit DAG.
        # Remote attribution additionally requires a prior real GitHub HEAD
        # receipt in the same task, with the exact descendant hash.
        verified = bool(ancestry)
        if verified:
            ancestor, descendant = (x.lower() for x in ancestry.groups())
            result["ancestor_sha"] = ancestor
            result["descendant_sha"] = descendant
            result["pinned_commit_status"] = "LOCAL_ANCESTRY_ONLY"
            for earlier in reversed(list(prior_stages or ())):
                if not isinstance(earlier, Mapping):
                    continue
                if (earlier.get("schema") != result["schema"]
                        or earlier.get("stage") != "REMOTE_REFS"
                        or earlier.get("stage_verified") is not True
                        or earlier.get("remote_head_sha") != descendant
                        or not earlier.get("repository")
                        or not earlier.get("evidence_ref")):
                    continue
                result["pinned_commit_status"] = "REMOTE_HEAD_ANCESTOR_PROVED"
                result["source_provenance"] = {
                    "repository": earlier["repository"],
                    "pinned_commit": ancestor,
                    "remote_head_sha": descendant,
                    "remote_head_evidence_ref": earlier["evidence_ref"],
                    "local_ancestry_evidence_ref": evidence_ref,
                    "proof_kind": "GIT_HASH_CHAIN_FROM_AUTHENTICATED_REMOTE_REF",
                    "credential_identity_verified": False,
                }
                break
    elif stage == "REMOTE_FETCH":
        # Exit 0 does not establish which object was fetched.
        result["status"] = "FETCH_EXIT_ZERO_OBJECT_UNVERIFIED"
        return result
    elif stage == "LOCAL_COMMIT_OBJECT":
        verified = stdout == "commit"
        if verified:
            result["pinned_commit_status"] = "LOCAL_OBJECT_ONLY"
    elif stage == "PINNED_TREE_BLOB":
        verified = bool(re.search(r"(?m)^\d+\s+blob\s+[0-9a-fA-F]{40}\s+", stdout))
        if verified:
            result["blob_identity_status"] = "COMMIT_TREE_ENTRY_ONLY"
    elif stage == "LOCAL_BLOB_HASH":
        verified = bool(re.fullmatch(r"[0-9a-fA-F]{40}", stdout))
        if verified:
            result["blob_identity_status"] = "LOCAL_GIT_BLOB_ONLY"
    elif stage == "LOCAL_ARCHIVE_SHA256":
        verified = bool(re.fullmatch(r"[0-9a-fA-F]{64}", stdout))
        if verified:
            result["blob_identity_status"] = "LOCAL_ARCHIVE_SHA256_ONLY"
    result["stage_verified"] = verified
    result["status"] = "STAGE_OBSERVED" if verified else "OUTPUT_UNVERIFIED"
    return result


__all__ = ["github_source_stage_evidence"]
