#!/usr/bin/env python3
"""Durable, exactly-once, transactional local chunk writer."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .artifact_transfer import validate_artifact_content


class ChunkedWriteError(RuntimeError):
    pass


_PROCESS_LOCK = threading.RLock()
TERMINAL_STATES = {"COMMITTED", "ABORTED", "FAILED", "EXPIRED"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class ChunkedWriteManager:
    def __init__(
        self,
        state_root: str | Path,
        allowed_roots: list[str | Path],
        *,
        ttl_sec: float = 24 * 60 * 60,
        fault_hook: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.manifest_dir = self.state_root / "manifests"
        self.staging_dir = self.state_root / "staging"
        self.ledger_dir = self.state_root / "ledger"
        self.allowed_roots = [Path(root).expanduser().resolve() for root in allowed_roots]
        self.ttl_sec = max(1.0, float(ttl_sec))
        self.fault_hook = fault_hook
        for directory in (self.manifest_dir, self.staging_dir, self.ledger_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.cleanup_expired()
        self._recover_staging_boundaries()

    def _fault(self, point: str, manifest: dict) -> None:
        if self.fault_hook:
            self.fault_hook(point, dict(manifest))

    def _manifest_path(self, write_id: str) -> Path:
        return self.manifest_dir / (_sha(write_id.encode("utf-8")) + ".json")

    def _staging_path(self, write_id: str) -> Path:
        return self.staging_dir / (_sha(write_id.encode("utf-8")) + ".part")

    def _ledger_path(self, action_id: str) -> Path:
        return self.ledger_dir / (_sha(action_id.encode("utf-8")) + ".json")

    def _load_manifest(self, write_id: str) -> dict:
        path = self._manifest_path(write_id)
        if not path.exists():
            raise ChunkedWriteError(f"write_session_not_found:{write_id}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("write_id") != write_id:
            raise ChunkedWriteError("manifest_identity_mismatch")
        return manifest

    def _save_manifest(self, manifest: dict) -> None:
        manifest["updated_at"] = time.time()
        _atomic_json(self._manifest_path(str(manifest["write_id"])), manifest)

    def _normalize_destination(self, raw_path: str) -> Path:
        if not raw_path or not Path(raw_path).is_absolute():
            raise ChunkedWriteError("destination_must_be_absolute")
        if ".." in Path(raw_path).parts:
            raise ChunkedWriteError("destination_parent_traversal_forbidden")
        destination = Path(raw_path).expanduser().resolve(strict=False)
        if not any(destination == root or root in destination.parents for root in self.allowed_roots):
            raise ChunkedWriteError(f"destination_outside_authorized_roots:{destination}")
        # Reject any existing symlink/reparse component before creating files.
        current = Path(destination.anchor)
        for part in destination.parts[1:]:
            current = current / part
            if not current.exists():
                continue
            info = current.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if current.is_symlink() or (attrs & reparse):
                raise ChunkedWriteError(f"reparse_or_symlink_forbidden:{current}")
        return destination

    def _open_manifest_for_destination(self, destination: Path, exclude: str = "") -> dict | None:
        normalized = os.path.normcase(str(destination))
        for path in self.manifest_dir.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if candidate.get("write_id") == exclude:
                continue
            if candidate.get("state") in {"OPEN", "COMMITTING"} and os.path.normcase(
                str(candidate.get("destination_path", ""))
            ) == normalized:
                return candidate
        return None

    def _destination_identity(self, destination: Path) -> tuple[bool, str]:
        if not destination.exists():
            return False, ""
        if not destination.is_file():
            raise ChunkedWriteError("destination_not_regular_file")
        return True, _sha(destination.read_bytes())

    def _action(self, payload: dict, operation: str, callback: Callable[[], dict]) -> dict:
        action_id = str(payload.get("action_id", "")).strip()
        if not action_id:
            raise ChunkedWriteError("missing_action_id")
        payload_hash = _sha(_canonical(payload).encode("utf-8"))
        ledger_path = self._ledger_path(action_id)
        with _PROCESS_LOCK:
            if ledger_path.exists():
                record = json.loads(ledger_path.read_text(encoding="utf-8"))
                if record.get("canonical_payload_sha256") != payload_hash:
                    raise ChunkedWriteError("PROTOCOL_VIOLATION:action_id_payload_conflict")
                result = dict(record.get("result") or {})
                result["replay"] = True
                return result
            result = callback()
            _atomic_json(ledger_path, {
                "action_id": action_id,
                "write_id": payload.get("write_id", ""),
                "operation": operation,
                "canonical_payload_sha256": payload_hash,
                "result": result,
                "committed_at": time.time(),
            })
            return result

    def begin(self, payload: dict, *, request_id: str = "", run_id: str = "", turn_id: Any = "") -> dict:
        def perform() -> dict:
            write_id = str(payload.get("write_id", "")).strip()
            if not write_id:
                raise ChunkedWriteError("missing_write_id")
            destination = self._normalize_destination(str(payload.get("path", "")))
            encoding = str(payload.get("encoding", "utf-8")).lower()
            if encoding != "utf-8":
                raise ChunkedWriteError("unsupported_encoding")
            expected_size = payload.get("expected_size")
            chunk_count = payload.get("chunk_count")
            expected_hash = str(payload.get("expected_sha256", "")).lower()
            streaming_mode = expected_size is None and not expected_hash and chunk_count is None
            if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
                raise ChunkedWriteError("invalid_expected_size")
            if chunk_count is not None and (type(chunk_count) is not int or chunk_count < 1):
                raise ChunkedWriteError("invalid_chunk_count")
            if expected_hash and len(expected_hash) != 64:
                raise ChunkedWriteError("invalid_expected_sha256")
            if not streaming_mode and (expected_size is None or not expected_hash or chunk_count is None):
                raise ChunkedWriteError("incomplete_declared_expectations")
            if self._manifest_path(write_id).exists():
                raise ChunkedWriteError("write_id_already_exists")
            lease = self._open_manifest_for_destination(destination)
            if lease:
                raise ChunkedWriteError(f"destination_lease_conflict:{lease.get('write_id')}")
            existed, destination_hash = self._destination_identity(destination)
            if existed and not bool(payload.get("overwrite", False)):
                raise ChunkedWriteError("destination_exists_overwrite_false")
            expected_destination = str(payload.get("expected_destination_sha256", "") or "").lower()
            if expected_destination and expected_destination != destination_hash:
                raise ChunkedWriteError("destination_compare_and_swap_failed_at_begin")
            staging = self._staging_path(write_id)
            with staging.open("xb") as handle:
                handle.flush(); os.fsync(handle.fileno())
            now = time.time()
            manifest = {
                "write_id": write_id, "request_id": request_id, "run_id": run_id,
                "turn_id": turn_id, "destination_path": str(destination),
                "staging_path": str(staging), "encoding": encoding,
                "expected_size": expected_size, "expected_sha256": expected_hash,
                "expected_destination_sha256": expected_destination,
                "destination_existed": existed, "destination_hash_at_begin": destination_hash,
                "chunk_count": chunk_count, "received_chunks": [], "next_offset": 0,
                "streaming_mode": streaming_mode,
                "state": "OPEN", "created_at": now, "updated_at": now,
                "lease_owner": f"pid:{os.getpid()}",
            }
            self._save_manifest(manifest)
            self._fault("manifest_created", manifest)
            return self._public(manifest, operation="begin_file_write")
        return self._action(payload, "begin_file_write", perform)

    def _decode_chunk(self, payload: dict) -> bytes:
        encoding = str(payload.get("content_encoding", "utf-8")).lower()
        content = payload.get("content")
        if not isinstance(content, str):
            raise ChunkedWriteError("chunk_content_must_be_string")
        if encoding == "utf-8":
            return content.encode("utf-8")
        if encoding == "base64":
            try:
                return base64.b64decode(content, validate=True)
            except Exception as exc:
                raise ChunkedWriteError("invalid_base64_chunk") from exc
        raise ChunkedWriteError("unsupported_content_encoding")

    def write_chunk(self, payload: dict) -> dict:
        def perform() -> dict:
            manifest = self._load_manifest(str(payload.get("write_id", "")))
            if manifest.get("state") != "OPEN":
                raise ChunkedWriteError(f"session_not_open:{manifest.get('state')}")
            index = payload.get("chunk_index", len(manifest["received_chunks"]))
            offset = payload.get("offset", manifest["next_offset"])
            if type(index) is not int or type(offset) is not int or index < 0 or offset < 0:
                raise ChunkedWriteError("invalid_chunk_index_or_offset")
            if index != len(manifest["received_chunks"]):
                raise ChunkedWriteError("chunk_out_of_order_or_duplicate_index")
            if offset < manifest["next_offset"]:
                raise ChunkedWriteError("chunk_overlap")
            if offset > manifest["next_offset"]:
                raise ChunkedWriteError("chunk_gap")
            if manifest["chunk_count"] is not None and index >= manifest["chunk_count"]:
                raise ChunkedWriteError("chunk_count_overflow")
            data = self._decode_chunk(payload)
            if "chunk_size" in payload and payload.get("chunk_size") != len(data):
                raise ChunkedWriteError("chunk_size_mismatch")
            if "chunk_sha256" in payload and str(payload.get("chunk_sha256", "")).lower() != _sha(data):
                raise ChunkedWriteError("chunk_sha256_mismatch")
            if manifest["expected_size"] is not None and offset + len(data) > manifest["expected_size"]:
                raise ChunkedWriteError("chunk_overflow")
            staging = Path(manifest["staging_path"])
            if staging.stat().st_size != manifest["next_offset"]:
                raise ChunkedWriteError("staging_manifest_offset_mismatch")
            with staging.open("ab") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            self._fault("chunk_bytes_written", manifest)
            manifest["received_chunks"].append({
                "chunk_index": index, "offset": offset, "size": len(data),
                "sha256": _sha(data), "action_id": payload.get("action_id", ""),
            })
            manifest["next_offset"] = offset + len(data)
            self._save_manifest(manifest)
            self._fault("chunk_manifest_committed", manifest)
            return self._public(manifest, operation="write_file_chunk", chunk_index=index)
        return self._action(payload, "write_file_chunk", perform)

    def commit(self, payload: dict) -> dict:
        def perform() -> dict:
            manifest = self._load_manifest(str(payload.get("write_id", "")))
            if manifest.get("state") != "OPEN":
                raise ChunkedWriteError(f"session_not_open:{manifest.get('state')}")
            supplied_size = payload.get("expected_size")
            supplied_hash = str(payload.get("expected_sha256", "") or "").lower()
            if supplied_size is not None and manifest["expected_size"] is not None and supplied_size != manifest["expected_size"]:
                raise ChunkedWriteError("commit_manifest_expectation_mismatch")
            if supplied_hash and manifest["expected_sha256"] and supplied_hash != manifest["expected_sha256"]:
                raise ChunkedWriteError("commit_manifest_expectation_mismatch")
            if manifest["chunk_count"] is not None and len(manifest["received_chunks"]) != manifest["chunk_count"]:
                raise ChunkedWriteError("missing_chunks")
            if not manifest["received_chunks"]:
                raise ChunkedWriteError("missing_chunks")
            staging = Path(manifest["staging_path"])
            data = staging.read_bytes()
            if manifest["expected_size"] is not None and len(data) != manifest["expected_size"]:
                raise ChunkedWriteError("assembled_size_mismatch")
            assembled_hash = _sha(data)
            if manifest["expected_sha256"] and assembled_hash != manifest["expected_sha256"]:
                raise ChunkedWriteError("assembled_sha256_mismatch")
            if supplied_size is not None and supplied_size != len(data):
                raise ChunkedWriteError("commit_supplied_size_mismatch")
            if supplied_hash and supplied_hash != assembled_hash:
                raise ChunkedWriteError("commit_supplied_sha256_mismatch")
            # In streaming mode LocalAgent, not the language model, derives the
            # mechanical byte count/hash/chunk count before atomic commit.
            manifest["expected_size"] = len(data)
            manifest["expected_sha256"] = assembled_hash
            manifest["chunk_count"] = len(manifest["received_chunks"])
            destination = Path(manifest["destination_path"])
            suffix = destination.suffix.lower()
            valid, reason = validate_artifact_content(data, expected_suffix=suffix)
            if not valid:
                raise ChunkedWriteError(f"format_validation_failed:{reason}")
            existed, current_hash = self._destination_identity(destination)
            if existed != manifest["destination_existed"] or current_hash != manifest["destination_hash_at_begin"]:
                raise ChunkedWriteError("destination_compare_and_swap_failed_at_commit")
            manifest["state"] = "COMMITTING"
            manifest["validator_result"] = reason
            self._save_manifest(manifest)
            self._fault("committing_before_replace", manifest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            commit_temp = destination.with_name(destination.name + f".{manifest['write_id']}.tmp")
            with commit_temp.open("xb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(commit_temp, destination)
            self._fault("atomic_replace_complete", manifest)
            output_hash = _sha(destination.read_bytes())
            if output_hash != assembled_hash:
                manifest["state"] = "FAILED"; self._save_manifest(manifest)
                raise ChunkedWriteError("output_readback_sha256_mismatch")
            manifest["state"] = "COMMITTED"
            manifest["output_sha256"] = output_hash
            manifest["committed_at"] = time.time()
            self._save_manifest(manifest)
            staging.unlink(missing_ok=True)
            return self._public(manifest, operation="commit_file_write")
        return self._action(payload, "commit_file_write", perform)

    def abort(self, payload: dict) -> dict:
        def perform() -> dict:
            manifest = self._load_manifest(str(payload.get("write_id", "")))
            if manifest.get("state") == "COMMITTED":
                raise ChunkedWriteError("cannot_abort_committed_session")
            if manifest.get("state") not in {"ABORTED", "EXPIRED"}:
                manifest["state"] = "ABORTED"
                self._save_manifest(manifest)
            Path(manifest["staging_path"]).unlink(missing_ok=True)
            return self._public(manifest, operation="abort_file_write")
        return self._action(payload, "abort_file_write", perform)

    def _public(self, manifest: dict, **extra: Any) -> dict:
        result = {
            "status": "success", "write_id": manifest["write_id"],
            "state": manifest["state"], "destination_path": manifest["destination_path"],
            "next_offset": manifest["next_offset"],
            "received_chunks": len(manifest["received_chunks"]),
            "expected_size": manifest["expected_size"],
            "expected_sha256": manifest["expected_sha256"],
        }
        if manifest.get("output_sha256"):
            result["output_sha256"] = manifest["output_sha256"]
        result.update(extra)
        return result

    def cleanup_expired(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        expired = 0
        with _PROCESS_LOCK:
            for path in self.manifest_dir.glob("*.json"):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    if manifest.get("state") not in {"OPEN", "COMMITTING"}:
                        continue
                    if now - float(manifest.get("updated_at", 0)) < self.ttl_sec:
                        continue
                    manifest["state"] = "EXPIRED"
                    self._save_manifest(manifest)
                    Path(manifest["staging_path"]).unlink(missing_ok=True)
                    expired += 1
                except Exception:
                    continue
        return {"expired_sessions": expired}

    def abort_open_sessions(self, reason: str = "agent_cancelled") -> dict:
        """Cooperative cancellation path when no Web action envelope is available."""
        aborted = 0
        with _PROCESS_LOCK:
            for path in self.manifest_dir.glob("*.json"):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    if manifest.get("state") not in {"OPEN", "COMMITTING"}:
                        continue
                    # A proven post-replace COMMITTING state is recovered on
                    # startup; cancellation never rolls back an already proven
                    # destination commit.
                    if manifest.get("state") == "COMMITTING":
                        destination = Path(manifest["destination_path"])
                        if destination.exists() and _sha(destination.read_bytes()) == manifest["expected_sha256"]:
                            continue
                    manifest["state"] = "ABORTED"
                    manifest["abort_reason"] = reason
                    self._save_manifest(manifest)
                    Path(manifest["staging_path"]).unlink(missing_ok=True)
                    aborted += 1
                except Exception:
                    continue
        return {"aborted_sessions": aborted}

    def _recover_staging_boundaries(self) -> None:
        # Crash after durable bytes but before manifest commit: truncate the
        # unacknowledged suffix. Never infer success or auto-commit.
        for path in self.manifest_dir.glob("*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if manifest.get("state") == "COMMITTING":
                    destination = Path(manifest["destination_path"])
                    if destination.exists() and _sha(destination.read_bytes()) == manifest["expected_sha256"]:
                        manifest["state"] = "COMMITTED"
                        manifest["output_sha256"] = manifest["expected_sha256"]
                        manifest["recovered_after_replace"] = True
                        manifest["committed_at"] = time.time()
                        self._save_manifest(manifest)
                        Path(manifest["staging_path"]).unlink(missing_ok=True)
                    else:
                        manifest["state"] = "FAILED"
                        manifest["failure_reason"] = "commit_outcome_not_proven_after_restart"
                        self._save_manifest(manifest)
                    continue
                if manifest.get("state") != "OPEN":
                    continue
                staging = Path(manifest["staging_path"])
                if staging.exists() and staging.stat().st_size > manifest["next_offset"]:
                    with staging.open("r+b") as handle:
                        handle.truncate(manifest["next_offset"]); handle.flush(); os.fsync(handle.fileno())
                elif not staging.exists() or staging.stat().st_size < manifest["next_offset"]:
                    manifest["state"] = "FAILED"
                    manifest["failure_reason"] = "staging_shorter_than_manifest"
                    self._save_manifest(manifest)
            except Exception:
                continue
