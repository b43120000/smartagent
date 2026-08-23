#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

PROTOCOL_NAME = "smartagent_artifact_bundle"
PROTOCOL_VERSION = 1
MANIFEST_NAME = "manifest.json"
ACTIONS_NAME = "smartagent_actions.jsonl"

MAX_FILES = 128
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ACTIONS = 128

BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,191}$")

CONTROL_TOOLS = {"final_response", "turn_commit"}
FORBIDDEN_NESTED_TOOLS = {"execute_artifact_bundle"}


class ArtifactBundleError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_member_name(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute():
        raise ArtifactBundleError(f"absolute ZIP member forbidden: {name}")
    if any(part in {"", ".", ".."} for part in p.parts):
        raise ArtifactBundleError(f"unsafe ZIP member path: {name}")
    if p.parts and ":" in p.parts[0]:
        raise ArtifactBundleError(f"drive-qualified ZIP member forbidden: {name}")
    return p


def validate_zip_structure(path: Path) -> dict:
    if not path.is_file():
        raise ArtifactBundleError(f"bundle not found: {path}")
    if path.suffix.lower() != ".zip":
        raise ArtifactBundleError("artifact bundle must be a .zip file")

    total = 0
    names = []
    with zipfile.ZipFile(path, "r") as z:
        infos = z.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise ArtifactBundleError(f"invalid ZIP member count: {len(infos)}")

        for info in infos:
            _safe_member_name(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ArtifactBundleError(f"symlink ZIP member forbidden: {info.filename}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ArtifactBundleError(f"ZIP member too large: {info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactBundleError("ZIP uncompressed size exceeds limit")
            names.append(info.filename)

    return {
        "zip_sha256": sha256_file(path),
        "member_count": len(names),
        "uncompressed_bytes": total,
        "members": names,
    }


def find_payload_prefix(names: list[str]) -> str:
    manifests = [n for n in names if n == MANIFEST_NAME or n.endswith("/" + MANIFEST_NAME)]
    if len(manifests) != 1:
        raise ArtifactBundleError("bundle must contain exactly one manifest.json")
    manifest = manifests[0]
    return manifest[:-len(MANIFEST_NAME)]


def load_bundle(path: str | Path, expected_sha256: str = "") -> tuple[dict, list[dict], dict]:
    bundle = Path(path).expanduser().resolve()
    evidence = validate_zip_structure(bundle)

    if expected_sha256:
        expected = expected_sha256.strip().lower()
        if evidence["zip_sha256"].lower() != expected:
            raise ArtifactBundleError(
                f"bundle sha256 mismatch: expected={expected}, actual={evidence['zip_sha256']}"
            )

    with zipfile.ZipFile(bundle, "r") as z:
        prefix = find_payload_prefix(evidence["members"])
        manifest_member = prefix + MANIFEST_NAME
        actions_member = prefix + ACTIONS_NAME
        if actions_member not in evidence["members"]:
            raise ArtifactBundleError("smartagent_actions.jsonl is missing")

        try:
            manifest = json.loads(z.read(manifest_member).decode("utf-8"))
        except Exception as exc:
            raise ArtifactBundleError(f"invalid manifest.json: {exc}") from exc

        actions_raw = z.read(actions_member)
        actions_sha = sha256_bytes(actions_raw)

    if not isinstance(manifest, dict):
        raise ArtifactBundleError("manifest must be a JSON object")
    if manifest.get("protocol_name") != PROTOCOL_NAME:
        raise ArtifactBundleError("unsupported artifact-bundle protocol_name")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise ArtifactBundleError("unsupported artifact-bundle protocol_version")

    bundle_id = str(manifest.get("bundle_id", "") or "")
    if not BUNDLE_ID_RE.fullmatch(bundle_id):
        raise ArtifactBundleError("invalid bundle_id")

    expected_actions_sha = str(manifest.get("actions_sha256", "") or "").lower()
    if not expected_actions_sha or expected_actions_sha != actions_sha:
        raise ArtifactBundleError(
            f"action stream hash mismatch: expected={expected_actions_sha or '<missing>'}, actual={actions_sha}"
        )

    actions = []
    seen_ids = set()
    for line_no, raw_line in enumerate(actions_raw.decode("utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        if len(actions) >= MAX_ACTIONS:
            raise ArtifactBundleError("too many actions")
        try:
            action = json.loads(raw_line)
        except Exception as exc:
            raise ArtifactBundleError(f"invalid JSON action at line {line_no}: {exc}") from exc
        if not isinstance(action, dict):
            raise ArtifactBundleError(f"action line {line_no} is not an object")
        tool = str(action.get("tool", "") or "")
        action_id = str(action.get("action_id", "") or "")
        if not tool:
            raise ArtifactBundleError(f"action line {line_no} missing tool")
        if tool in CONTROL_TOOLS:
            raise ArtifactBundleError(f"control tool forbidden inside bundle: {tool}")
        if tool in FORBIDDEN_NESTED_TOOLS:
            raise ArtifactBundleError("nested execute_artifact_bundle is forbidden")
        if not ACTION_ID_RE.fullmatch(action_id):
            raise ArtifactBundleError(f"invalid action_id at line {line_no}")
        if action_id in seen_ids:
            raise ArtifactBundleError(f"duplicate action_id: {action_id}")
        seen_ids.add(action_id)
        actions.append(action)

    if not actions:
        raise ArtifactBundleError("bundle contains no executable actions")

    evidence.update(
        bundle_id=bundle_id,
        actions_sha256=actions_sha,
        action_count=len(actions),
        payload_prefix=prefix,
    )
    return manifest, actions, evidence
