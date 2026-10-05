#!/usr/bin/env python3
from __future__ import annotations

"""Validate and render files returned through Telegram's Bot API."""

import hashlib
import re
from pathlib import Path


TELEGRAM_BOT_DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _telegram_file_request(request: str) -> bool:
    text = str(request or "")
    destination = re.search(r"telegram|bot|機器人", text, re.IGNORECASE)
    transfer = re.search(r"上傳|傳送|發送|回傳|送到|send|upload", text, re.IGNORECASE)
    return bool(destination and transfer)


def prepare_telegram_completion(
    *,
    request: str,
    summary: str,
    workspace: str | Path,
    artifacts: list[dict] | tuple[dict, ...],
) -> dict:
    """Return a truthful TASK_COMPLETED payload and verified artifacts.

    WebGPT may request a transfer, but only this software boundary may assert
    that a local file is eligible for Telegram delivery.
    """
    root = Path(workspace).expanduser().resolve()
    accepted: list[dict] = []
    rejected: list[dict] = []

    for row in list(artifacts or []):
        if not isinstance(row, dict):
            rejected.append({"name": "<unknown>", "reason": "artifact 格式無效"})
            continue
        raw = Path(str(row.get("path") or ""))
        target = (raw if raw.is_absolute() else root / raw).expanduser().resolve()
        name = Path(str(row.get("name") or target.name)).name or target.name
        try:
            target.relative_to(root)
            if not target.is_file():
                raise FileNotFoundError(str(target))
            size = target.stat().st_size
            digest = _sha256(target)
            expected = str(row.get("sha256") or "").strip().lower()
            if expected and digest.lower() != expected:
                raise ValueError("檔案 SHA-256 已改變")
            if size > TELEGRAM_BOT_DOCUMENT_LIMIT_BYTES:
                rejected.append({
                    "name": name,
                    "size_bytes": size,
                    "sha256": digest,
                    "reason": (
                        f"大小 {size:,} bytes，超過 Telegram Bot API "
                        f"50 MB 上限"
                    ),
                })
                continue
            kind = str(row.get("kind") or "").strip().lower()
            if kind not in {"photo", "image", "document"}:
                kind = "photo" if target.suffix.lower() in _IMAGE_SUFFIXES else "document"
            accepted.append({
                "path": str(target),
                "kind": kind,
                "name": name,
                "caption": str(row.get("caption") or ""),
                "workspace": str(root),
                "size_bytes": size,
                "sha256": digest,
            })
        except Exception as exc:
            rejected.append({
                "name": name or "<unknown>",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    lines: list[str] = []
    if accepted:
        names = "、".join(row["name"] for row in accepted)
        lines.append(f"已準備由 Telegram Bot 回傳：{names}。")
    for row in rejected:
        lines.append(f"檔案未上傳到 Telegram：{row['name']}；{row['reason']}。")
    if not accepted and not rejected and _telegram_file_request(request):
        lines.append("檔案未上傳到 Telegram：此次流程未產生 Telegram outbound artifact。")

    return {
        "summary": "\n".join(lines) if lines else str(summary or ""),
        "workspace": str(root),
        "artifacts": accepted,
        "artifact_rejections": rejected,
    }


__all__ = [
    "TELEGRAM_BOT_DOCUMENT_LIMIT_BYTES",
    "prepare_telegram_completion",
]
