"""Shared local attachment staging for every SmartAgent interface."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterable

from .attachment_cache import AttachmentCache
from .attachment_transaction import AttachmentTransaction


@dataclass(frozen=True)
class StagedAttachment:
    source_path: str
    staged_path: str
    original_name: str
    upload_name: str
    sha256: str
    size_bytes: int
    cache_hit: bool
    attachment_id: str = ""


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def _safe_upload_name(
    source: Path, trace_id: str, ordinal: int, digest: str, name_token: str = ""
) -> str:
    """Build a collision-resistant browser-visible name without touching source."""
    raw_trace = str(trace_id or "request")
    trace_text = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_trace).strip("-_") or "request"
    trace_token = f"{trace_text[:18]}-{_identity(raw_trace)[:6]}"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-") or "attachment"
    suffix = source.suffix
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name_token or "")).strip("-_")[:64]
    token_part = f"__{token}" if token else ""
    return f"{stem[:80]}__{trace_token}__{max(1, int(ordinal)):03d}{token_part}__{digest[:8]}{suffix}"


def _cleanup_old_requests(root: Path, max_age_seconds: float) -> None:
    cutoff = time.time() - max(0.0, float(max_age_seconds))
    if not root.is_dir():
        return
    for child in root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        except OSError:
            continue


def stage_attachments(
    workspace: str | Path,
    conversation_id: str,
    session_id: str,
    request_id: str,
    paths: Iterable[str | Path],
    *,
    stale_after_seconds: float = 24 * 60 * 60,
    ordinal_start: int = 1,
    name_tokens: Iterable[str] | None = None,
) -> list[StagedAttachment]:
    """Create request-isolated upload copies backed by a content cache."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace_not_directory:{root}")
    conversation_id = str(conversation_id or "default")
    session_id = str(session_id or "default")
    request_id = str(request_id or "request")
    upload_root = root / ".agents" / "upload_cache"
    upload_root.mkdir(parents=True, exist_ok=True)
    _cleanup_old_requests(upload_root, stale_after_seconds)
    cache = AttachmentCache(root / ".agents" / "attachment_content_cache")
    transactions = AttachmentTransaction(root)
    request_root = upload_root / _identity(conversation_id) / _identity(session_id) / _identity(request_id)

    source_paths = list(paths)
    tokens = list(name_tokens or [])
    if tokens and len(tokens) != len(source_paths):
        raise ValueError("attachment_name_token_count_mismatch")
    staged_items: list[StagedAttachment] = []
    seen_hashes: set[str] = set()
    first_ordinal = max(1, int(ordinal_start))
    for offset, raw in enumerate(source_paths):
        ordinal = first_ordinal + offset
        source = Path(raw).expanduser()
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        if not source.exists():
            raise FileNotFoundError(f"attachment_not_found:{source}")
        if not source.is_file():
            raise ValueError(f"attachment_not_file:{source}")

        cached, cache_hit, digest = cache.stage_file(
            conversation_id, session_id, source
        )
        # One request must never upload identical bytes twice.  ChatGPT rejects
        # that case with a modal which can otherwise occlude the composer.
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        isolated_dir = request_root / uuid.uuid4().hex
        isolated_dir.mkdir(parents=True, exist_ok=False)
        name_token = tokens[offset] if tokens else ""
        upload_name = _safe_upload_name(source, request_id, ordinal, digest, name_token)
        target = isolated_dir / upload_name
        shutil.copy2(cached, target)
        copied_digest = _digest_file(target)
        if copied_digest != digest:
            shutil.rmtree(isolated_dir, ignore_errors=True)
            raise ValueError("attachment_staged_copy_hash_mismatch")
        transaction = transactions.begin(
            request_id=request_id,
            task_epoch=request_id,
            conversation_id=conversation_id,
            source_path=target,
        )
        transactions.transition(transaction.attachment_id, "STAGED")
        staged_items.append(
            StagedAttachment(
                source_path=str(source),
                staged_path=str(target.resolve()),
                original_name=source.name,
                upload_name=upload_name,
                sha256=digest,
                size_bytes=source.stat().st_size,
                cache_hit=cache_hit,
                attachment_id=transaction.attachment_id,
            )
        )
    return staged_items


__all__ = ["StagedAttachment", "stage_attachments"]
