"""Cold-start Source Discovery Bootstrap from explicit artifact registrations.

This module reads ONLY Agent-owned metadata and packaged declarative catalogs;
it NEVER reads an artifact, contacts GitHub, downloads an archive, scans disks,
or extends a Task's Tool Scope or write permission. The recorded artifact is a
potential acquisition source; its claimed hashes require real verification.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .paths import install_root, runtime_root
from .process_file_lock import exclusive_process_lock
from .source_goal_identity import context_identity, goal_identity

SCHEMA = "SMARTAGENT_SOURCE_DISCOVERY_CATALOG_V1"
BOOTSTRAP_SCHEMA = "SMARTAGENT_SOURCE_DISCOVERY_BOOTSTRAP_V1"
MAX_BYTES = 262144
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _repository_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute()
            and len(path.parts) > 0
            and all(part not in {"", ".", ".."} for part in value.split("/"))
            and "\\" not in value and ":" not in value
            and "\x00" not in value)


def _valid_artifact(row: object, expected_goal: Mapping) -> dict | None:
    if not isinstance(row, Mapping):
        return None
    if goal_identity(row).get("digest") != expected_goal.get("digest"):
        return None
    artifact = row.get("artifact")
    if not isinstance(artifact, Mapping) or artifact.get("provider") != "github":
        return None
    repo = artifact.get("repository")
    commit = artifact.get("commit")
    location = artifact.get("path")
    sha256 = artifact.get("sha256")
    git_blob = artifact.get("git_blob_sha")
    root = artifact.get("archive_root")
    if (not isinstance(repo, str) or not REPO.fullmatch(repo)
            or not isinstance(commit, str) or not SHA1.fullmatch(commit)
            or not isinstance(sha256, str) or not SHA256.fullmatch(sha256)
            or not isinstance(git_blob, str) or not SHA1.fullmatch(git_blob)
            or not _repository_path(location)
            or not _repository_path(root)
            or "/" in root
            or not str(location).lower().endswith(".zip")
            or not isinstance(row.get("provenance"), str)
            or not row["provenance"]):
        return None
    manifest = artifact.get("manifest_path", "")
    if manifest and not _repository_path(manifest):
        return None
    return {
        "candidate_id": "ART-" + hashlib.sha256(
            (repo + "@" + commit + ":" + location).encode("utf-8")
        ).hexdigest()[:16].upper(),
        "kind": "REMOTE_ARTIFACT_REFERENCE",
        "provider": "github",
        "repository": repo,
        "commit": commit,
        "artifact_path": location,
        "archive_root": root,
        "expected_sha256": sha256,
        "expected_git_blob_sha": git_blob,
        "manifest_path": manifest,
        "provenance_ref": row["provenance"][:240],
        "evidence_level": "E1",
        "repository_access": "NOT_CHECKED",
        "repository_access_authorization_state": "UNKNOWN",
        "artifact_retrieved": False,
        "archive_hash_verified": False,
        "source_integrity": "UNKNOWN",
        "local_source_path": "",
        "local_read_authorization": None,
        "local_read_authorization_state": "UNKNOWN",
        "download_authorization": None,
        "download_authorization_state": "UNKNOWN",
        "write_authorization": None,
        "write_authorization_state": "UNKNOWN",
        "source_is_deployment_ready": False,
        "next_prerequisite": "CHECK_REMOTE_REPOSITORY_ACCESS_AND_AUTHORIZATION",
    }


def _catalog_file(path: Path) -> dict:
    try:
        if not path.is_file():
            return {"status": "NOT_FOUND", "records": []}
        if path.stat().st_size > MAX_BYTES:
            return {"status": "INVALID", "records": []}
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"status": "INVALID", "records": []}
    if (not isinstance(doc, Mapping)
            or doc.get("schema") != SCHEMA
            or not isinstance(doc.get("records"), list)
            or len(doc["records"]) > 64):
        return {"status": "INVALID", "records": []}
    return {"status": "READY" if doc["records"] else "EMPTY",
            "records": doc["records"]}


def _registry_path(agent_root: str | Path) -> Path:
    return runtime_root(agent_root) / "source_artifact_registrations_v1.json"


def bootstrap_source_discovery(
    agent_root: str | Path, target_contract: Mapping | None, *,
    owner: str = "", instance: str = "", workspace: str = "",
    installation_root: str | Path | None = None,
) -> dict:
    """Match exact Goal to packaged catalog and state-scoped registrations.

    The packaged catalog belongs to the Installation Root, never the Telegram
    Task state directory or active Workspace. installation_root is only an
    explicit deployment/test override of the canonical installed app root.
    """
    goal = goal_identity(target_contract)
    empty = {
        "schema": BOOTSTRAP_SCHEMA,
        "state": "NOT_INDEXABLE",
        "catalog_status": "NOT_QUERIED",
        "registration_status": "NOT_QUERIED",
        "catalog_path_authority": "INSTALLATION_ROOT",
        "registration_path_authority": "TASK_STATE_ROOT",
        "goal_identity_status": goal.get("status", "UNKNOWN"),
        "catalog_records_examined": 0,
        "catalog_matched_count": 0,
        "catalog_goal_mismatch_count": 0,
        "catalog_invalid_record_count": 0,
        "registration_records_examined": 0,
        "registration_matched_count": 0,
        "artifact_references": [],
        "artifact_count": 0,
        "source_location_known_as_reference": False,
        "local_source_observed": False,
        "repository_access_verified": False,
        "network_access_attempted": False,
        "disk_source_scan_performed": False,
        "new_authorization_granted": False,
    }
    if goal.get("status") != "READY":
        return empty
    state_root = Path(agent_root).expanduser().resolve()
    package_root = install_root(installation_root)
    catalog = _catalog_file(package_root / "config" / "source_discovery_catalog.json")
    registry = _catalog_file(_registry_path(state_root))
    context = context_identity(owner, instance, workspace)
    output: dict[str, dict] = {}
    catalog_matches = 0
    catalog_mismatches = 0
    catalog_invalid = 0
    registry_matches = 0
    for origin, documents in (("PACKAGED_CATALOG", catalog), ("OWNER_REGISTRY", registry)):
        for row in documents["records"]:
            if not isinstance(row, Mapping):
                if origin == "PACKAGED_CATALOG":
                    catalog_invalid += 1
                continue
            if origin == "OWNER_REGISTRY":
                if not context or row.get("context_digest") != context:
                    continue
            elif goal_identity(row).get("digest") != goal.get("digest"):
                catalog_mismatches += 1
                continue
            item = _valid_artifact(row, goal)
            if item is None:
                if origin == "PACKAGED_CATALOG":
                    catalog_invalid += 1
                continue
            if origin == "PACKAGED_CATALOG":
                catalog_matches += 1
            else:
                registry_matches += 1
            item["registration_origin"] = origin
            output[item["candidate_id"]] = item
    refs = sorted(output.values(), key=lambda row: row["candidate_id"])[:16]
    if refs:
        state = "ARTIFACT_REFERENCE_FOUND"
    elif catalog["status"] == "INVALID" or registry["status"] == "INVALID":
        state = "CATALOG_INVALID_OR_UNREADABLE"
    else:
        state = "NO_REGISTERED_ARTIFACT"
    return {
        **empty, "state": state, "catalog_status": catalog["status"],
        "registration_status": registry["status"],
        "catalog_records_examined": len(catalog["records"]),
        "catalog_matched_count": catalog_matches,
        "catalog_goal_mismatch_count": catalog_mismatches,
        "catalog_invalid_record_count": catalog_invalid,
        "registration_records_examined": len(registry["records"]),
        "registration_matched_count": registry_matches,
        "artifact_references": refs,
        "artifact_count": len(refs),
        "source_location_known_as_reference": bool(refs),
        "new_authorization_granted": False,
    }


def register_source_artifact(
    agent_root: str | Path, *, owner: str, instance: str,
    workspace: str, record: Mapping, explicitly_authorized: bool = False,
) -> dict:
    """Register user-approved remote reference without touching target/source.

    Caller must have obtained an explicit registration approval. Existing
    access / network / writing authorization must never be inferred.
    """
    context = context_identity(owner, instance, workspace)
    identity = goal_identity(record)
    if not explicitly_authorized or not context or identity.get("status") != "READY":
        return {"status": "REJECTED", "reason": "REGISTRATION_AUTHORIZATION_OR_CONTEXT_REQUIRED"}
    valid = _valid_artifact(record, identity)
    if valid is None:
        return {"status": "REJECTED", "reason": "INVALID_ARTIFACT_REFERENCE"}
    target = _registry_path(agent_root)
    row = dict(record)
    row["context_digest"] = context
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_process_lock(
            target.with_suffix(".lock"), timeout_sec=2.0,
            label="source_artifact_registration",
        ):
            doc = _catalog_file(target)
            if doc["status"] == "INVALID":
                return {"status": "REJECTED", "reason": "REGISTRY_INVALID"}
            previous = [
                item for item in doc["records"] if not (
                    isinstance(item, Mapping)
                    and item.get("context_digest") == context
                    and _valid_artifact(item, identity) is not None
                    and _valid_artifact(item, identity)["candidate_id"] ==
                        valid["candidate_id"]
                )
            ]
            entries = (previous + [row])[-64:]
            payload = json.dumps({"schema": SCHEMA, "records": entries},
                                 ensure_ascii=False, sort_keys=True)
            if len(payload.encode("utf-8")) > MAX_BYTES:
                return {"status": "REJECTED", "reason": "REGISTRY_CAPACITY"}
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    except (OSError, RuntimeError, ValueError):
        return {"status": "FAILED", "reason": "REGISTRY_IO_FAILURE"}
    return {"status": "RECORDED", "reason": "REFERENCE_ONLY",
            "candidate_id": valid["candidate_id"],
            "grants_access": False, "grants_write": False}


__all__ = [
    "SCHEMA", "BOOTSTRAP_SCHEMA", "bootstrap_source_discovery",
    "register_source_artifact",
]
