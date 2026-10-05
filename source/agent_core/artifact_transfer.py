#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared WebGPT artifact discovery/download implementation.

Stage 2 reliability goals:
- bind downloads to the freshest assistant UI instead of blindly clicking any Download;
- understand file cards *and* generated-image <img>/preview UI;
- prefer direct authenticated fetch when a stable URL exists;
- fall back to native Playwright download, response interception, and preview UI;
- validate bytes before atomically replacing the destination;
- preserve structured attempt evidence instead of swallowing every exception.

The public ``download_latest_artifact`` function intentionally keeps the Stage 1
signature so LocalAgent/RemoteAgent callers do not need to know how ChatGPT
represents downloadable media.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse

from .web_ui import ArtifactObservation, create_web_ui_for_page


class ArtifactTransferError(RuntimeError):
    """Artifact download failed after all safe strategies were exhausted."""


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
HTML_PREFIXES = (b"<!doctype html", b"<html", b"<head", b"<body")
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".pyw", ".cpp", ".cc", ".c", ".h",
    ".hpp", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".bat", ".ps1", ".js", ".ts", ".css", ".html", ".htm",
}
OFFICE_REQUIRED_MEMBERS = {
    ".docx": {"[Content_Types].xml", "word/document.xml"},
    ".xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
    ".pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
}
SUPPORTED_ARTIFACT_EXTS = TEXT_EXTS | IMAGE_EXTS | {
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"
}
DEFAULT_ARTIFACT_STAGING_ROOT = (
    Path(__file__).resolve().parents[2] / "localdata" / "runtime" / "artifact_staging"
)


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_id: str
    request_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    logical_filename: str = ""
    expected_extension: str = ""
    expected_mime: str = ""
    size: int | None = None
    sha256: str = ""
    created_at: float = field(default_factory=time.time)
    source_kind: str = "webgpt"


class ArtifactConsumedLedger:
    """Request-local exactly-once assignment of candidates to destinations."""

    def __init__(self) -> None:
        self._consumed: dict[str, str] = {}

    def is_consumed(self, candidate_id: str) -> bool:
        return candidate_id in self._consumed

    def consume(self, candidate_id: str, destination: str) -> None:
        normalized = str(Path(destination).expanduser().resolve())
        previous = self._consumed.get(candidate_id)
        if previous and previous != normalized:
            raise ArtifactTransferError(
                f"candidate_already_consumed:candidate_id={candidate_id}:destination={previous}"
            )
        self._consumed[candidate_id] = normalized

    def snapshot(self) -> dict[str, str]:
        return dict(self._consumed)


@dataclass
class ArtifactCandidate:
    element: Any
    kind: str
    score: int
    href: str = ""
    src: str = ""
    filename: str = ""
    text: str = ""
    aria_label: str = ""
    title: str = ""
    testid: str = ""
    root_rank: int = 0
    turn_index: int = -1
    turn_fingerprint: str = ""

    def summary(self) -> str:
        return (
            f"kind={self.kind} score={self.score} filename={self.filename!r} "
            f"href={self.href[:100]!r} src={self.src[:100]!r} "
            f"text={self.text[:80]!r} aria={self.aria_label[:80]!r} "
            f"turn_index={self.turn_index} turn_fp={self.turn_fingerprint[:12]}"
        )

    def identity_signature(self) -> str:
        """Stable identity used to deduplicate a discovered artifact candidate."""
        payload = {
            "kind": self.kind,
            "href": self.href,
            "src": self.src,
            "filename": self.filename,
            "text": self.text[:160],
            "aria": self.aria_label[:160],
            "title": self.title[:160],
            "testid": self.testid[:160],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def file_evidence(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    data = p.read_bytes()
    return {"path": str(p), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _filename_from_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        name = Path(unquote(parsed.path)).name
        return name if name and "." in name else ""
    except Exception:
        return ""


def _candidate_filename(observation: ArtifactObservation, href: str, src: str, text: str) -> str:
    for value in (
        observation.download_name,
        observation.data_filename,
        observation.filename_attr,
        observation.aria_label,
        text,
        _filename_from_url(href),
        _filename_from_url(src),
    ):
        value = str(value or "").strip()
        extensions = "|".join(
            re.escape(ext.lstrip(".")) for ext in sorted(SUPPORTED_ARTIFACT_EXTS, key=len, reverse=True)
        )
        match = re.search(rf"([\w .()\-]+\.(?:{extensions}))\b", value, re.I)
        if match:
            return match.group(1).strip()
    return ""


def candidate_matches_manifest(candidate: ArtifactCandidate, manifest: ArtifactManifest) -> tuple[bool, str]:
    """Fail-closed identity/type matching before any candidate is acquired."""
    expected_name = Path(manifest.logical_filename).name.casefold() if manifest.logical_filename else ""
    actual_name = Path(candidate.filename).name.casefold() if candidate.filename else ""
    expected_ext = (manifest.expected_extension or Path(expected_name).suffix).casefold()
    if expected_name and actual_name != expected_name:
        # Generated image DOM often has no stable logical filename. It may use
        # a provider filename unrelated to the user's requested destination.
        # Fresh request scope owns identity; generated raster bytes are safely
        # converted later when their format differs from the requested suffix.
        # Named non-image file cards must remain exact.
        if not (candidate.kind == "image" and expected_ext in IMAGE_EXTS):
            return False, f"logical_filename_mismatch:expected={expected_name},actual={actual_name or '<missing>'}"
    if (
        expected_ext and actual_name and candidate.kind != "image"
        and Path(actual_name).suffix.casefold() != expected_ext
    ):
        return False, f"extension_mismatch:expected={expected_ext},actual={Path(actual_name).suffix.casefold()}"
    return True, "identity_match"


def validate_artifact_against_manifest(
    data: bytes, manifest: ArtifactManifest, *, content_type: str = ""
) -> tuple[bool, str]:
    if manifest.size is not None and len(data) != manifest.size:
        return False, f"manifest_size_mismatch:expected={manifest.size},actual={len(data)}"
    actual_hash = hashlib.sha256(data).hexdigest()
    if manifest.sha256 and actual_hash.lower() != manifest.sha256.lower():
        return False, "manifest_sha256_mismatch"
    if manifest.expected_mime and content_type:
        expected = manifest.expected_mime.split(";", 1)[0].strip().lower()
        actual = content_type.split(";", 1)[0].strip().lower()
        if actual != expected:
            return False, f"manifest_mime_mismatch:expected={expected},actual={actual}"
    return validate_artifact_content(
        data, content_type=content_type,
        expected_suffix=manifest.expected_extension or Path(manifest.logical_filename).suffix,
    )


def _candidate_score(*, kind: str, href: str, src: str, filename: str, text: str,
                     aria_label: str, title: str, testid: str, expected_name: str,
                     root_rank: int) -> int:
    blob = " ".join((href, src, filename, text, aria_label, title, testid)).lower()
    score = 0
    expected = (expected_name or "").lower().strip()
    if expected:
        if filename.lower() == expected:
            score += 140
        elif expected in blob:
            score += 90
    if filename:
        score += 45
    if "download" in blob or "下載" in blob:
        score += 45
    if "/files/" in blob or "sandbox:" in blob:
        score += 35
    if kind == "image":
        score += 28
        if any(token in blob for token in ("generated", "image", "dall", "asset", "usercontent")):
            score += 18
    if href.startswith(("http://", "https://", "/")) or src.startswith(("http://", "https://", "/")):
        score += 15
    if href.startswith(("blob:", "data:")) or src.startswith(("blob:", "data:")):
        score += 20
    if "file" in blob or "artifact" in blob or "attachment" in blob:
        score += 15
    # Latest assistant root should outrank older assistant-turn candidates.
    score += max(0, 20 - root_rank * 8)
    # Penalize obvious UI chrome/profile/icons.
    if any(token in blob for token in ("avatar", "profile", "logo", "icon", "favicon", "emoji")):
        score -= 80
    return score


def _root_candidates(page, scope: Optional[dict] = None, strict_scope: bool = False) -> list[tuple[Any, int, int, str]]:
    adapter = create_web_ui_for_page(page)
    turns = list(adapter.observation_turns("assistant"))
    raw_baseline_count = (scope or {}).get("assistant_count_before", -1)
    baseline_count = int(raw_baseline_count) if raw_baseline_count is not None else -1
    baseline_fp = str((scope or {}).get("last_assistant_fp_before", "") or "")
    roots: list[tuple[Any, int, int, str]] = []
    if scope:
        fresh: list[tuple[int, Any, str]] = []
        for index, turn in enumerate(turns):
            element = turn.element
            if element is None:
                continue
            fp = adapter.element_fingerprint(element)
            if baseline_count >= 0 and index >= baseline_count:
                fresh.append((index, element, fp))
            elif baseline_count > 0 and index == baseline_count - 1 and baseline_fp and fp != baseline_fp:
                fresh.append((index, element, fp))
        for rank, (index, element, fp) in enumerate(reversed(fresh)):
            roots.append((element, rank, index, fp))
        # Request-scoped discovery never falls back to older turns or the page root.
        return roots
    for rank, turn in enumerate(reversed(turns[-3:])):
        if turn.element is None:
            continue
        index = max(0, len(turns) - 1 - rank)
        roots.append((turn.element, rank, index, adapter.element_fingerprint(turn.element)))
    return roots


def _candidate_from_observation(
    observation: ArtifactObservation, *, expected_name: str, root_rank: int,
    turn_index: int, turn_fp: str,
) -> ArtifactCandidate | None:
    tag = observation.tag
    href = observation.href
    src = observation.src
    text = observation.text
    aria = observation.aria_label
    title = observation.title
    testid = observation.testid
    filename = _candidate_filename(observation, href, src, text)
    kind = "image" if tag == "img" else ("link" if tag == "a" else "button")
    blob = " ".join((href, src, filename, text, aria, title, testid)).lower()
    plausible = bool(
        filename
        or "download" in blob
        or "下載" in blob
        or "/files/" in blob
        or "sandbox:" in blob
        or "artifact" in blob
        or (kind == "image" and src and not src.startswith("data:image/svg"))
    )
    if not plausible:
        return None
    score = _candidate_score(
        kind=kind, href=href, src=src, filename=filename, text=text,
        aria_label=aria, title=title, testid=testid,
        expected_name=expected_name, root_rank=root_rank,
    )
    if score <= 0:
        return None
    return ArtifactCandidate(
        element=observation.element, kind=kind, score=score, href=href, src=src,
        filename=filename, text=text, aria_label=aria, title=title,
        testid=testid, root_rank=root_rank, turn_index=turn_index,
        turn_fingerprint=turn_fp,
    )


def _ready_page_image_candidates(page, expected_name: str = "") -> list[ArtifactCandidate]:
    """Return only fully-loaded, substantial page-level generated images."""
    adapter = create_web_ui_for_page(page)
    candidates: list[ArtifactCandidate] = []
    for observation in adapter.artifact_observations(page):
        if observation.tag != "img" or not observation.generated_media:
            continue
        if not (
            observation.visible and observation.complete
            and observation.natural_width > 0 and observation.natural_height > 0
        ):
            continue
        candidate = _candidate_from_observation(
            observation, expected_name=expected_name, root_rank=0,
            turn_index=-3, turn_fp="request-page-image",
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def _candidate_diagnostic(candidate: ArtifactCandidate) -> dict:
    """Return identity evidence without logging URLs, names, or page text."""
    identity = candidate.identity_signature()
    return {
        "candidate_id": identity[:16],
        "kind": candidate.kind,
        "score": int(candidate.score),
        "has_href": bool(candidate.href),
        "has_src": bool(candidate.src),
        "href_sha": hashlib.sha256(
            candidate.href.encode("utf-8", errors="replace")
        ).hexdigest()[:12] if candidate.href else "",
        "src_sha": hashlib.sha256(
            candidate.src.encode("utf-8", errors="replace")
        ).hexdigest()[:12] if candidate.src else "",
        "filename_sha": hashlib.sha256(
            candidate.filename.encode("utf-8", errors="replace")
        ).hexdigest()[:12] if candidate.filename else "",
        "turn_index": int(candidate.turn_index),
        "turn_fp": str(candidate.turn_fingerprint or "")[:12],
    }


def snapshot_page_ready_image_signatures(page) -> list[str]:
    """Capture page-level ready image identities immediately before submit."""
    try:
        return sorted({c.identity_signature() for c in _ready_page_image_candidates(page)})
    except Exception:
        return []


def _request_proven_page_image_candidates(
    page, expected_name: str, scope: Optional[dict], strict_scope: bool,
    diagnostics: Optional[dict] = None,
) -> list[ArtifactCandidate]:
    """Admit page-level images only with end-to-end current-request proof."""
    gate = diagnostics if diagnostics is not None else {}
    gate.update({
        "scope_present": bool(scope),
        "strict_scope": bool(strict_scope),
        "fresh_page_image_proven": bool((scope or {}).get("fresh_page_image_proven")),
    })
    if not scope or not strict_scope or not bool(scope.get("fresh_page_image_proven")):
        gate["result"] = "REJECT"
        gate["reason"] = "request_page_image_proof_missing"
        return []
    before_fp = str(scope.get("page_ready_image_fingerprint_before") or "")
    proven_fp = str(scope.get("page_ready_image_fingerprint_after") or "")
    gate["scope_before_fp"] = before_fp[:16]
    gate["scope_proven_fp"] = proven_fp[:16]
    if not proven_fp or proven_fp == before_fp:
        gate["result"] = "REJECT"
        gate["reason"] = "scope_fingerprint_not_fresh"
        return []
    try:
        current = create_web_ui_for_page(page).media_state(page) or {}
    except Exception as exc:
        gate["result"] = "REJECT"
        gate["reason"] = "current_media_state_unavailable"
        gate["error_type"] = type(exc).__name__
        return []
    current_fp = str(current.get("ready_image_fingerprint") or "")
    gate["current_ready_count"] = int(current.get("image_ready") or 0)
    gate["current_fp"] = current_fp[:16]
    gate["current_signature_count"] = len(current.get("ready_image_signatures") or [])
    bound_candidate_ids = {
        str(value) for value in (scope.get("candidate_ids") or []) if str(value)
    }
    gate["bound_candidate_count"] = len(bound_candidate_ids)
    if int(current.get("image_ready") or 0) <= 0:
        gate["result"] = "REJECT"
        gate["reason"] = "no_current_ready_image"
        return []
    # The whole-page fingerprint is temporal proof used only before exact
    # candidate identities have been bound.  Once a producing request owns a
    # candidate_id, unrelated image mounts/unmounts must not revoke it.
    if not bound_candidate_ids and current_fp != proven_fp:
        gate["result"] = "REJECT"
        gate["reason"] = "current_fingerprint_differs_from_proven_fingerprint"
        return []
    baseline = set(scope.get("page_ready_image_signatures_before") or [])
    ready_candidates = _ready_page_image_candidates(page, expected_name)
    if bound_candidate_ids:
        fresh_candidates = [
            candidate for candidate in ready_candidates
            if candidate.identity_signature() in bound_candidate_ids
        ]
    else:
        fresh_candidates = [
            candidate for candidate in ready_candidates
            if candidate.identity_signature() not in baseline
        ]
    gate["baseline_signature_count"] = len(baseline)
    gate["ready_candidate_count"] = len(ready_candidates)
    gate["ready_candidate_ids"] = [
        item.identity_signature()[:16] for item in ready_candidates[:8]
    ]
    gate["fresh_candidate_count"] = len(fresh_candidates)
    gate["fresh_candidate_ids"] = [
        item.identity_signature()[:16] for item in fresh_candidates[:8]
    ]
    gate["result"] = "PASS" if fresh_candidates else "REJECT"
    if fresh_candidates:
        gate["reason"] = (
            "bound_page_candidate_admitted"
            if bound_candidate_ids else "fresh_page_candidate_admitted"
        )
    else:
        gate["reason"] = (
            "bound_page_candidate_not_mounted"
            if bound_candidate_ids else "all_ready_images_match_baseline"
        )
    return fresh_candidates


def discover_artifact_candidates(page, expected_name: str = "", *, scope: Optional[dict] = None,
                                 strict_scope: bool = False, trusted_preview: bool = False,
                                 diagnostics: Optional[dict] = None) -> list[ArtifactCandidate]:
    candidates: list[ArtifactCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    adapter = create_web_ui_for_page(page)
    roots = _root_candidates(page, scope=scope, strict_scope=strict_scope)
    if diagnostics is not None:
        diagnostics.update({
            "scope_present": bool(scope),
            "strict_scope": bool(strict_scope),
            "trusted_preview": bool(trusted_preview),
            "request_id": str((scope or {}).get("request_id") or ""),
            "artifact_id": str((scope or {}).get("artifact_id") or ""),
            "root_count": len(roots),
            "roots": [
                {"rank": rank, "turn_index": index, "turn_fp": str(fp or "")[:12]}
                for _root, rank, index, fp in roots[:8]
            ],
        })
    if trusted_preview:
        if scope is not None or strict_scope:
            raise ValueError("trusted_preview cannot be combined with request-scope discovery")
        roots = [(page, 0, -2, "trusted-preview")]
    for root, root_rank, turn_index, turn_fp in roots:
        for observation in adapter.artifact_observations(root):
            candidate = _candidate_from_observation(
                observation, expected_name=expected_name, root_rank=root_rank,
                turn_index=turn_index, turn_fp=turn_fp,
            )
            if candidate is None:
                continue
            key = (candidate.kind, candidate.href, candidate.src, candidate.filename or (candidate.aria_label + candidate.text)[:120])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    page_image_gate: dict = {}
    page_candidates = _request_proven_page_image_candidates(
        page, expected_name, scope, strict_scope, page_image_gate
    )
    for candidate in page_candidates:
        key = (candidate.kind, candidate.href, candidate.src, candidate.filename or (candidate.aria_label + candidate.text)[:120])
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    prefilter_candidates = list(candidates)
    allowed_identities: set[str] = set()
    if scope and scope.get("candidate_ids"):
        allowed_identities = {
            str(value) for value in (scope.get("candidate_ids") or []) if str(value)
        }
        candidates = [
            candidate for candidate in candidates
            if candidate.identity_signature() in allowed_identities
        ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    if diagnostics is not None:
        diagnostics.update({
            "page_image_gate": page_image_gate,
            "page_candidate_count": len(page_candidates),
            "prefilter_candidate_count": len(prefilter_candidates),
            "prefilter_candidates": [
                _candidate_diagnostic(item) for item in prefilter_candidates[:12]
            ],
            "scope_candidate_ids": [value[:16] for value in sorted(allowed_identities)[:12]],
            "identity_filter_rejected_count": len(prefilter_candidates) - len(candidates),
            "accepted_candidate_count": len(candidates),
            "accepted_candidates": [
                _candidate_diagnostic(item) for item in candidates[:12]
            ],
        })
        if candidates:
            diagnostics["result"] = "PASS"
            diagnostics["reason"] = "candidate_admitted"
        elif prefilter_candidates and allowed_identities:
            diagnostics["result"] = "REJECT"
            diagnostics["reason"] = "candidate_identity_changed_or_not_registered"
        elif page_image_gate.get("reason"):
            diagnostics["result"] = "REJECT"
            diagnostics["reason"] = str(page_image_gate.get("reason"))
        else:
            diagnostics["result"] = "REJECT"
            diagnostics["reason"] = "no_downloadable_observation"
    return candidates


def snapshot_artifact_signatures(page) -> list[str]:
    """Capture assistant-turn artifact identities before submit for diagnostics.

    Request-bound freshness is established by assistant-turn boundaries, not by
    admitting page-root candidates.
    """
    try:
        candidates = discover_artifact_candidates(page, scope=None, strict_scope=False)
    except Exception:
        return []
    return sorted({c.identity_signature() for c in candidates})


def has_fresh_artifact(page, scope: Optional[dict]) -> bool:
    """True only when a current-request artifact can be temporally proven fresh."""
    try:
        return bool(discover_artifact_candidates(page, scope=scope, strict_scope=True))
    except Exception:
        return False


def _looks_like_html(data: bytes, content_type: str = "") -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype:
        return True
    prefix = data[:512].lstrip().lower()
    return any(prefix.startswith(marker) for marker in HTML_PREFIXES)


def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if len(data) >= 12 and data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    return ""


def _decode_text(data: bytes) -> tuple[str | None, str]:
    if b"\x00" in data:
        return None, "text_contains_null_bytes"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            return None, "text_not_utf8"


def _html_error_reason(text: str) -> str:
    lowered = text.lower()
    suspicious = (
        "log in", "login", "sign in", "access denied", "unauthorized", "forbidden",
        "cf-chl-", "cloudflare", "just a moment", "download_url", "backend-api/estuary",
        "登入", "存取遭拒", "無權限",
    )
    return "html_error_or_login_page" if any(token in lowered for token in suspicious) else ""


def validate_artifact_content(
    data: bytes, *, content_type: str = "", expected_suffix: str = ""
) -> tuple[bool, str]:
    """Format-aware, fail-closed validation before destination commit."""
    if not data:
        return False, "empty_body"
    suffix = (expected_suffix or "").lower()
    html_payload = _looks_like_html(data, content_type)
    if html_payload:
        if suffix not in {".html", ".htm"}:
            return False, "html_error_or_login_page"
        text, encoding = _decode_text(data)
        if text is None:
            return False, encoding
        error = _html_error_reason(text)
        return (False, error) if error else (True, "ok:html")

    if suffix in IMAGE_EXTS:
        detected = _detect_image_ext(data)
        if detected:
            normalized_expected = ".jpg" if suffix in {".jpg", ".jpeg"} else suffix
            normalized_detected = ".jpg" if detected in {".jpg", ".jpeg"} else detected
            if normalized_expected != normalized_detected:
                return False, f"image_format_mismatch:expected={suffix},detected={detected}"
            structural_ok = {
                ".png": len(data) >= 24 and data[12:16] == b"IHDR" and b"IEND" in data[-32:],
                ".jpg": len(data) >= 4 and data.endswith(b"\xff\xd9"),
                ".gif": len(data) >= 14 and data.endswith(b";"),
                ".webp": len(data) >= 16,
                ".bmp": len(data) >= 14,
                ".avif": len(data) >= 16,
            }.get(normalized_detected, True)
            if not structural_ok:
                return False, f"damaged_image_structure:{normalized_detected}"
        else:
            # Unknown image formats are accepted only when the HTTP response
            # explicitly identifies them as image/*; known mismatches above are
            # rejected so a .webp is never silently stored as .png.
            if not (content_type or "").lower().startswith("image/"):
                return False, "expected_image_but_magic/content-type_not_image"
        return True, "ok:image"

    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            return False, "pdf_magic_missing"
        if b"%%EOF" not in data[-2048:]:
            return False, "pdf_eof_missing"
        return True, "ok:pdf"

    if suffix in {".zip", *OFFICE_REQUIRED_MEMBERS.keys()}:
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                bad_member = archive.testzip()
                if bad_member:
                    return False, f"zip_crc_failed:{bad_member}"
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile, RuntimeError):
            return False, "invalid_zip_container"
        required = OFFICE_REQUIRED_MEMBERS.get(suffix, set())
        missing = sorted(required - names)
        if missing:
            return False, "office_package_missing:" + ",".join(missing)
        return True, "ok:" + (suffix.lstrip(".") or "zip")

    if suffix == ".json":
        text, encoding = _decode_text(data)
        if text is None:
            return False, encoding
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"invalid_json:{exc.msg}"
        return True, "ok:json"

    if suffix in {".py", ".pyw"}:
        text, encoding = _decode_text(data)
        if text is None:
            return False, encoding
        try:
            parsed = ast.parse(text, "<artifact>", "exec")
            compile(parsed, "<artifact>", "exec")
        except (SyntaxError, ValueError) as exc:
            return False, f"invalid_python:{type(exc).__name__}:{exc}"
        # A JSON object is also a syntactically valid Python expression.  A
        # download broker/login endpoint can therefore pass compile() while
        # still being transport metadata rather than the requested program.
        if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr):
            value = parsed.body[0].value
            if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.Constant)):
                return False, "python_is_data_only_payload"
        return True, "ok:python"

    if suffix in TEXT_EXTS or (content_type or "").lower().startswith("text/"):
        text, encoding = _decode_text(data)
        if text is None:
            return False, encoding
        return True, "ok:text"

    # Unknown/binary formats remain eligible, but HTML masquerading and empty
    # payloads were already rejected above. Callers should provide an expected
    # suffix whenever format identity matters.
    return True, "ok:generic"


def _convert_image_for_destination(
    data: bytes, expected_suffix: str
) -> tuple[bytes, str, str]:
    """Convert a valid generated image when the requested file type differs.

    ChatGPT commonly emits PNG even when the user asks for ``.jpg``.  Renaming
    those bytes would create a corrupt contract, while rejecting them prevents
    the requested local delivery.  Conversion is deliberately limited to
    ordinary raster formats supported by Pillow and is revalidated by the
    caller before the destination is atomically replaced.
    """
    expected = (expected_suffix or "").lower()
    expected = ".jpg" if expected in {".jpg", ".jpeg"} else expected
    detected = _detect_image_ext(data)
    detected = ".jpg" if detected in {".jpg", ".jpeg"} else detected
    if not expected or expected not in IMAGE_EXTS or not detected or detected == expected:
        return data, "", ""
    formats = {
        ".jpg": ("JPEG", "image/jpeg"),
        ".png": ("PNG", "image/png"),
        ".webp": ("WEBP", "image/webp"),
        ".bmp": ("BMP", "image/bmp"),
    }
    if expected not in formats:
        raise ArtifactTransferError(
            f"image_conversion_unsupported:detected={detected}:expected={expected}"
        )
    valid, reason = validate_artifact_content(data, expected_suffix=detected)
    if not valid:
        raise ArtifactTransferError(f"image_conversion_source_invalid:{reason}")
    try:
        from PIL import Image

        source = io.BytesIO(data)
        output = io.BytesIO()
        with Image.open(source) as image:
            image.load()
            if expected == ".jpg":
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output, formats[expected][0], quality=95, optimize=True)
            else:
                image.save(output, formats[expected][0])
        converted = output.getvalue()
    except Exception as exc:
        raise ArtifactTransferError(
            f"image_conversion_failed:{detected}->{expected}:{type(exc).__name__}:{exc}"
        ) from exc
    return converted, formats[expected][1], f"converted:{detected}->{expected}"


def _validate_download_bytes(data: bytes, *, content_type: str = "", expected_suffix: str = "") -> tuple[bool, str]:
    """Backward-compatible wrapper for the centralized validator."""
    return validate_artifact_content(
        data, content_type=content_type, expected_suffix=expected_suffix
    )


def _decode_data_url(url: str) -> tuple[bytes, str]:
    header, payload = url.split(",", 1)
    media = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    if ";base64" in header:
        return base64.b64decode(payload), media
    return unquote(payload).encode("utf-8"), media


def _fetch_via_page(page, url: str) -> tuple[bytes, str]:
    """Fetch blob/data/authenticated resources inside the logged-in page context."""
    if url.startswith("data:"):
        return _decode_data_url(url)
    result = page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: 'include'});
            const b = await r.arrayBuffer();
            let binary = '';
            const bytes = new Uint8Array(b);
            const chunk = 0x8000;
            for (let i=0;i<bytes.length;i+=chunk) {
              binary += String.fromCharCode(...bytes.subarray(i, i+chunk));
            }
            return {ok:r.ok, status:r.status, type:r.headers.get('content-type')||'', body:btoa(binary)};
        }""",
        url,
    )
    if not result or not result.get("ok"):
        raise ArtifactTransferError(f"page_fetch_http_{(result or {}).get('status', 'unknown')}")
    return base64.b64decode(result.get("body") or ""), str(result.get("type") or "")


def _fetch_direct(page, browser_context, url: str) -> tuple[bytes, str]:
    if url.startswith(("blob:", "data:", "sandbox:")):
        return _fetch_via_page(page, url)
    absolute = urljoin(page.url, url)
    response = browser_context.request.get(absolute, timeout=8000)
    if not response.ok:
        raise ArtifactTransferError(f"http_status_{response.status}")
    return response.body(), str(response.headers.get("content-type", ""))


def _resolve_target(output_path: str, candidate: Optional[ArtifactCandidate], data: bytes = b"",
                    content_type: str = "") -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(output_path)))
    path = Path(raw)
    explicit_dir = raw.endswith(("/", "\\")) or (path.exists() and path.is_dir())
    candidate_name = (candidate.filename if candidate else "") or ""

    # A non-existing extension-less path is treated as a directory only when we
    # can infer a sensible filename. This supports commands such as C:/out/images/.
    inferred_ext = _detect_image_ext(data)
    if not inferred_ext and content_type:
        inferred_ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
    if not path.suffix and (explicit_dir or candidate_name or inferred_ext):
        explicit_dir = True

    if explicit_dir:
        filename = candidate_name or ("generated_image" + (inferred_ext or ".bin"))
        path = path / filename
    return path.resolve()


def _write_atomic(target: Path, data: bytes, *, content_type: str = "") -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    valid, reason = _validate_download_bytes(data, content_type=content_type, expected_suffix=target.suffix)
    if not valid:
        raise ArtifactTransferError(f"artifact_validation_failed:{reason}")
    tmp = target.with_name(target.name + ".download.tmp")
    try:
        if tmp.exists():
            tmp.unlink()
        tmp.write_bytes(data)
        if tmp.stat().st_size <= 0:
            raise ArtifactTransferError("artifact_validation_failed:empty_temp_file")
        tmp.replace(target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return file_evidence(target)


def _try_direct_candidate(page, browser_context, candidate: ArtifactCandidate, output_path: str) -> tuple[Path, dict]:
    url = candidate.href or candidate.src
    if not url:
        raise ArtifactTransferError("candidate_has_no_direct_url")
    data, ctype = _fetch_direct(page, browser_context, url)
    target = _resolve_target(output_path, candidate, data, ctype)
    evidence = _write_atomic(target, data, content_type=ctype)
    evidence.update({"method": "direct_fetch", "content_type": ctype, "candidate": candidate.summary()})
    return target, evidence


def stage_png_candidate(
    page,
    browser_context,
    candidate: ArtifactCandidate,
    *,
    request_id: str,
    artifact_id: str,
    staging_root: str | Path | None = None,
) -> dict:
    """Freeze one request-bound generated PNG while its DOM URL is still live."""
    if candidate.kind != "image":
        raise ArtifactTransferError("png_staging_candidate_is_not_image")
    url = candidate.href or candidate.src
    if not url:
        raise ArtifactTransferError("png_staging_candidate_has_no_direct_url")
    data, content_type = _fetch_direct(page, browser_context, url)
    detected = _detect_image_ext(data)
    if detected != ".png":
        raise ArtifactTransferError(
            f"png_staging_source_is_not_png:detected={detected or 'unknown'}"
        )
    valid, reason = validate_artifact_content(
        data, content_type=content_type, expected_suffix=".png"
    )
    if not valid:
        raise ArtifactTransferError(f"png_staging_validation_failed:{reason}")

    root = Path(staging_root or DEFAULT_ARTIFACT_STAGING_ROOT).expanduser().resolve()
    request_key = hashlib.sha256(
        str(request_id or "").encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    artifact_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(artifact_id or "artifact"))[:96]
    target = (root / request_key / f"{artifact_key}.png").resolve()
    target.relative_to(root)
    evidence = _write_atomic(target, data, content_type="image/png")
    return {
        "staged_png_status": "READY",
        "staged_png_path": str(target),
        "staged_png_root": str(root),
        "staged_png_candidate_id": candidate.identity_signature(),
        "staged_png_size": int(evidence["size"]),
        "staged_png_sha256": str(evidence["sha256"]),
        "staged_png_content_type": "image/png",
        "staged_png_at": time.time(),
    }


def _deliver_staged_png(
    scope: dict,
    output_path: str,
    *,
    expected_filename: str = "",
    manifest: ArtifactManifest | None = None,
    consumed_ledger: ArtifactConsumedLedger | None = None,
) -> dict:
    """Copy a frozen original PNG without consulting the current page DOM."""
    staged_value = str(scope.get("staged_png_path") or "").strip()
    if not staged_value:
        raise ArtifactTransferError("staged_png_path_missing")
    staged = Path(staged_value).expanduser().resolve()
    root = Path(
        str(scope.get("staged_png_root") or DEFAULT_ARTIFACT_STAGING_ROOT)
    ).expanduser().resolve()
    try:
        staged.relative_to(root)
    except ValueError as exc:
        raise ArtifactTransferError("staged_png_path_outside_staging_root") from exc
    if not staged.is_file():
        raise ArtifactTransferError("staged_png_file_missing")
    data = staged.read_bytes()
    valid, reason = validate_artifact_content(
        data, content_type="image/png", expected_suffix=".png"
    )
    if not valid:
        raise ArtifactTransferError(f"staged_png_validation_failed:{reason}")
    expected_size = int(scope.get("staged_png_size") or 0)
    actual_sha = hashlib.sha256(data).hexdigest()
    expected_sha = str(scope.get("staged_png_sha256") or "")
    if expected_size and len(data) != expected_size:
        raise ArtifactTransferError(
            f"staged_png_size_mismatch:expected={expected_size}:actual={len(data)}"
        )
    if expected_sha and actual_sha != expected_sha:
        raise ArtifactTransferError("staged_png_sha256_mismatch")
    scope_artifact_id = str(scope.get("artifact_id") or "")

    raw = os.path.expandvars(os.path.expanduser(str(output_path)))
    output_base = Path(raw).resolve()
    output_is_directory = bool(
        raw.endswith(("/", "\\")) or (output_base.exists() and output_base.is_dir())
    )
    if output_is_directory:
        requested_name = Path(expected_filename).name if expected_filename else "generated_image.png"
        name_path = Path(requested_name)
        if name_path.suffix.lower() != ".png":
            requested_name = (name_path.stem or "generated_image") + ".png"
        destination = (output_base / requested_name).resolve()
    else:
        destination = output_base
        if destination.suffix.lower() != ".png":
            destination = destination.with_suffix(".png")
    evidence = _write_atomic(destination, data, content_type="image/png")
    candidate_id = str(scope.get("staged_png_candidate_id") or "")
    if consumed_ledger and candidate_id:
        consumed_ledger.consume(candidate_id, str(destination))
    return {
        "status": "success",
        "path": str(destination),
        "method": "staged_png_copy",
        "size": int(evidence["size"]),
        "sha256": str(evidence["sha256"]),
        "content_type": "image/png",
        "candidate_id": candidate_id,
        "artifact_id": scope_artifact_id,
        "identity": "request_staged_png",
        "validator": reason,
        "conversion": "",
        "consumed": bool(consumed_ledger),
        "attempts": [{"method": "staged_png_copy", "status": "PASS"}],
    }


def cleanup_staged_png(scope: dict) -> bool:
    """Remove a consumed request-scoped staging file without broad deletion."""
    staged_value = str((scope or {}).get("staged_png_path") or "").strip()
    root_value = str((scope or {}).get("staged_png_root") or "").strip()
    if not staged_value or not root_value:
        return False
    staged = Path(staged_value).expanduser().resolve()
    root = Path(root_value).expanduser().resolve()
    try:
        staged.relative_to(root)
    except ValueError:
        return False
    if staged.is_file():
        staged.unlink()
    try:
        if staged.parent != root:
            staged.parent.rmdir()
    except OSError:
        pass
    return not staged.exists()


def _try_native_download(page, candidate: ArtifactCandidate, output_path: str) -> tuple[Path, dict]:
    if candidate.kind == "image":
        raise ArtifactTransferError("image_element_has_no_native_download_action")
    with page.expect_download(timeout=6000) as info:
        candidate.element.evaluate("el => el.click()")
    download = info.value
    suggested = str(getattr(download, "suggested_filename", "") or candidate.filename or "")
    proxy = ArtifactCandidate(
        element=candidate.element, kind=candidate.kind, score=candidate.score,
        href=candidate.href, src=candidate.src, filename=suggested or candidate.filename,
        text=candidate.text, aria_label=candidate.aria_label, title=candidate.title,
        testid=candidate.testid, root_rank=candidate.root_rank,
    )
    target = _resolve_target(output_path, proxy)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download.tmp")
    try:
        if tmp.exists():
            tmp.unlink()
        download.save_as(str(tmp))
        data = tmp.read_bytes()
        valid, reason = _validate_download_bytes(data, expected_suffix=target.suffix)
        if not valid:
            raise ArtifactTransferError(f"artifact_validation_failed:{reason}")
        tmp.replace(target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    evidence = file_evidence(target)
    evidence.update({"method": "native_download", "candidate": candidate.summary()})
    return target, evidence


def _try_response_interception(page, candidate: ArtifactCandidate, output_path: str) -> tuple[Path, dict]:
    if candidate.kind == "image":
        raise ArtifactTransferError("image_element_response_interception_not_needed")

    def predicate(response):
        try:
            url = str(response.url or "").lower()
            headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
            ctype = headers.get("content-type", "").lower()
            disposition = headers.get("content-disposition", "").lower()
            return bool(
                "/files/" in url or "download" in url or disposition or
                ctype.startswith("image/") or "application/octet-stream" in ctype
            )
        except Exception:
            return False

    with page.expect_response(predicate, timeout=6000) as info:
        candidate.element.evaluate("el => el.click()")
    response = info.value
    data = response.body()
    headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
    ctype = headers.get("content-type", "")
    target = _resolve_target(output_path, candidate, data, ctype)
    evidence = _write_atomic(target, data, content_type=ctype)
    evidence.update({"method": "response_interception", "content_type": ctype, "candidate": candidate.summary()})
    return target, evidence


def _preview_download_candidates(page, expected_name: str = "", *, scope: Optional[dict] = None,
                                 strict_scope: bool = False) -> list[ArtifactCandidate]:
    """Observe a trusted preview opened from an already request-bound candidate."""
    # Preview controls can be portal-mounted outside the assistant turn. This is
    # an explicit second phase entered only after a fresh request-bound opener
    # was clicked; it is not a fallback candidate source for initial discovery.
    return discover_artifact_candidates(
        page, expected_name=expected_name, scope=None, strict_scope=False, trusted_preview=True
    )


def _prioritize_preview_candidates(
    candidates: list[ArtifactCandidate], parent: ArtifactCandidate
) -> list[ArtifactCandidate]:
    """Put the preview's explicit download control before its opener card."""
    parent_identity = parent.identity_signature()

    def priority(item: ArtifactCandidate) -> tuple[int, int]:
        blob = " ".join((item.text, item.aria_label, item.title, item.testid)).casefold()
        explicit_download = int("download" in blob or "下載" in blob)
        different_from_parent = int(item.identity_signature() != parent_identity)
        return (explicit_download * 100 + different_from_parent * 20, item.score)

    return sorted(candidates, key=priority, reverse=True)


def _try_preview_recovery(page, browser_context, candidate: ArtifactCandidate, output_path: str,
                          expected_name: str = "", popup_guard=None) -> tuple[Path, dict]:
    if candidate.kind == "image":
        # Clicking generated images commonly opens a lightbox with a more stable
        # image source and/or an explicit Download button.
        try:
            candidate.element.evaluate("el => el.click()")
        except Exception as exc:
            raise ArtifactTransferError(f"preview_open_failed:{type(exc).__name__}") from exc
    else:
        try:
            candidate.element.evaluate("el => el.click()")
        except Exception as exc:
            raise ArtifactTransferError(f"preview_open_failed:{type(exc).__name__}") from exc
    time.sleep(0.5)
    if popup_guard:
        try:
            popup_guard()
        except Exception:
            pass

    preview = _preview_download_candidates(page, expected_name=expected_name)
    # The file card is often only a preview opener.  Once that exact, fresh,
    # named card has established identity, prefer the anonymous Download
    # control mounted by the preview over clicking the opener again.  The
    # acquired bytes are still validated against the original manifest before
    # commit, so this does not weaken stale-artifact protection.
    parent_identity = candidate.identity_signature()
    preview = _prioritize_preview_candidates(preview, candidate)
    for nested in preview[:10]:
        if nested.identity_signature() == parent_identity:
            continue
        if nested.href or nested.src:
            try:
                target, evidence = _try_direct_candidate(page, browser_context, nested, output_path)
                evidence["method"] = "preview_direct_fetch"
                return target, evidence
            except Exception:
                pass
        if nested.kind != "image":
            try:
                target, evidence = _try_native_download(page, nested, output_path)
                evidence["method"] = "preview_native_download"
                return target, evidence
            except Exception:
                pass
    raise ArtifactTransferError("preview_opened_but_no_downloadable_candidate")


def download_latest_artifact_with_evidence(page, browser_context, output_path: str,
                                           timeout_sec: float = 45.0,
                                           expected_filename: str = "",
                                           *, scope: Optional[dict] = None,
                                           strict_scope: bool = False,
                                           popup_guard=None,
                                           manifest: ArtifactManifest | None = None,
                                           consumed_ledger: ArtifactConsumedLedger | None = None,
                                           diagnostic_callback=None) -> dict:
    """Download the newest ChatGPT artifact and return structured evidence.

    Stage 3.1 defaults generic Web runtime calls to strict request scoping. If no
    artifact from the current request can be proven, this function fails closed
    instead of reusing any older page artifact.
    """
    if manifest is None and expected_filename:
        manifest = ArtifactManifest(
            artifact_id="legacy-" + hashlib.sha256(
                f"{scope or {}}|{expected_filename}".encode("utf-8", errors="replace")
            ).hexdigest()[:20],
            request_id=str((scope or {}).get("request_id", "")),
            run_id=str((scope or {}).get("run_id", "")),
            turn_id=str((scope or {}).get("turn_id", "")),
            logical_filename=expected_filename,
            expected_extension=Path(expected_filename).suffix,
        )
    effective_name = manifest.logical_filename if manifest and manifest.logical_filename else expected_filename
    staged_attempts: list[dict] = []
    if scope and str(scope.get("staged_png_status") or "") == "READY":
        try:
            result = _deliver_staged_png(
                scope,
                output_path,
                expected_filename=effective_name,
                manifest=manifest,
                consumed_ledger=consumed_ledger,
            )
            if diagnostic_callback:
                diagnostic_callback({
                    "phase": "staged_png_delivery",
                    "request_id": str(scope.get("request_id") or ""),
                    "artifact_id": str(scope.get("artifact_id") or ""),
                    "result": "PASS",
                    "method": "staged_png_copy",
                    "size": int(result.get("size") or 0),
                })
            return result
        except Exception as exc:
            staged_attempts.append({
                "method": "staged_png_copy",
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            })
            if diagnostic_callback:
                diagnostic_callback({
                    "phase": "staged_png_delivery",
                    "request_id": str(scope.get("request_id") or ""),
                    "artifact_id": str(scope.get("artifact_id") or ""),
                    "result": "FAIL",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:400],
                })
    raw_output_path = os.path.expandvars(os.path.expanduser(str(output_path)))
    output_base = Path(raw_output_path).resolve()
    output_is_directory = bool(
        raw_output_path.endswith(("/", "\\"))
        or (output_base.exists() and output_base.is_dir())
    )
    requested_suffix = (
        manifest.expected_extension if manifest and manifest.expected_extension
        else ("" if output_is_directory else output_base.suffix)
    )
    # Fetch into a neutral temporary file for generated images.  Source bytes
    # are validated first and may then be converted to the requested suffix.
    acquisition_suffix = "" if requested_suffix.lower() in IMAGE_EXTS else requested_suffix
    if output_is_directory:
        output_base.mkdir(parents=True, exist_ok=True)
        acquisition_path = output_base / (
            f".smartagent.{(manifest.artifact_id if manifest else 'transfer')}.acquire{acquisition_suffix}"
        )
    else:
        acquisition_path = output_base.with_name(
            f".{output_base.name}.{(manifest.artifact_id if manifest else 'transfer')}.acquire{acquisition_suffix}"
        )
    started_at = time.time()
    deadline = started_at + max(5.0, float(timeout_sec))
    attempts: list[dict] = list(staged_attempts)
    seen_candidate_keys: set[str] = set()
    last_diagnostic_signature = ""
    last_discovery_diagnostic: dict = {}
    poll_count = 0

    while time.time() < deadline:
        poll_count += 1
        if popup_guard:
            try:
                popup_guard()
            except Exception:
                pass
        discovery_diagnostic: dict = {
            "phase": "download_discovery_poll",
            "poll": poll_count,
            "elapsed_sec": round(max(0.0, time.time() - started_at), 3),
        }
        candidates = discover_artifact_candidates(
            page, expected_name=effective_name, scope=scope, strict_scope=strict_scope,
            diagnostics=discovery_diagnostic,
        )
        last_discovery_diagnostic = discovery_diagnostic
        diagnostic_signature_payload = {
            key: value for key, value in discovery_diagnostic.items()
            if key not in {"poll", "elapsed_sec"}
        }
        diagnostic_signature = hashlib.sha256(
            json.dumps(diagnostic_signature_payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        if diagnostic_callback and diagnostic_signature != last_diagnostic_signature:
            try:
                diagnostic_callback(discovery_diagnostic)
            except Exception:
                pass
            last_diagnostic_signature = diagnostic_signature
        for candidate in candidates[:18]:
            candidate_id = candidate.identity_signature()
            if consumed_ledger and consumed_ledger.is_consumed(candidate_id):
                attempts.append({
                    "method": "identity_gate", "status": "SKIP",
                    "candidate": candidate.summary(), "error": "candidate_already_consumed",
                })
                continue
            if manifest:
                identity_ok, identity_reason = candidate_matches_manifest(candidate, manifest)
                if not identity_ok:
                    attempts.append({
                        "method": "identity_gate", "status": "FAIL",
                        "candidate": candidate.summary(), "error": identity_reason,
                    })
                    continue
            key = candidate.summary()
            # Retry candidates after the UI changes, but avoid hammering one
            # unchanged candidate continuously within the polling loop.
            if key in seen_candidate_keys:
                continue
            seen_candidate_keys.add(key)

            strategies = []
            if candidate.href or candidate.src:
                strategies.append(("direct_fetch", _try_direct_candidate))
            # A named button without a URL is normally a preview opener, not a
            # native download control.  Open it once and resolve the download
            # action inside the trusted preview before trying event fallbacks
            # that would mutate/toggle the same UI repeatedly.
            preview_first = bool(
                candidate.kind == "button" and candidate.filename
                and not candidate.href and not candidate.src
            )
            if preview_first:
                strategies.append(("preview_recovery", None))
            if candidate.kind != "image" and not preview_first:
                strategies.extend([
                    ("native_download", _try_native_download),
                    ("response_interception", _try_response_interception),
                ])
            if not preview_first:
                strategies.append(("preview_recovery", None))

            for method, func in strategies:
                started = time.time()
                try:
                    if method == "preview_recovery":
                        target, evidence = _try_preview_recovery(
                            page, browser_context, candidate, str(acquisition_path), expected_name=effective_name, popup_guard=popup_guard
                        )
                    elif method == "native_download":
                        target, evidence = func(page, candidate, str(acquisition_path))
                    elif method == "response_interception":
                        target, evidence = func(page, candidate, str(acquisition_path))
                    else:
                        target, evidence = func(page, browser_context, candidate, str(acquisition_path))
                    acquired = Path(target)
                    data = acquired.read_bytes()
                    content_type = str(evidence.get("content_type", "") or "")
                    conversion = ""
                    if requested_suffix.lower() in IMAGE_EXTS:
                        data, converted_content_type, conversion = _convert_image_for_destination(
                            data, requested_suffix
                        )
                        if conversion:
                            acquired.write_bytes(data)
                            content_type = converted_content_type
                    if manifest:
                        valid, validation_reason = validate_artifact_against_manifest(
                            data, manifest, content_type=content_type
                        )
                    else:
                        valid, validation_reason = validate_artifact_content(
                            data, content_type=content_type, expected_suffix=requested_suffix
                        )
                    if not valid:
                        raise ArtifactTransferError(f"format_validation_failed:{validation_reason}")
                    actual_hash = hashlib.sha256(data).hexdigest()
                    if output_is_directory:
                        final_name = effective_name or candidate.filename
                        if not final_name:
                            inferred_ext = _detect_image_ext(data)
                            if not inferred_ext and content_type:
                                inferred_ext = mimetypes.guess_extension(
                                    content_type.split(";", 1)[0].strip()
                                ) or ""
                            final_name = "generated_image" + (inferred_ext or ".bin")
                        final_destination = (output_base / Path(final_name).name).resolve()
                    else:
                        final_destination = output_base
                    final_destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(acquired, final_destination)
                    attempts.append({
                        "method": method, "status": "PASS", "elapsed": round(time.time()-started, 3),
                        "candidate": candidate.summary(),
                    })
                    if consumed_ledger:
                        consumed_ledger.consume(candidate_id, output_path)
                    return {
                        "status": "success",
                        "path": str(final_destination),
                        "method": evidence.get("method", method),
                        "size": len(data),
                        "sha256": actual_hash,
                        "content_type": evidence.get("content_type", ""),
                        "candidate": evidence.get("candidate", candidate.summary()),
                        "candidate_id": candidate_id,
                        "artifact_id": manifest.artifact_id if manifest else "",
                        "identity": "manifest" if manifest else "heuristic",
                        "validator": validation_reason,
                        "conversion": conversion,
                        "consumed": bool(consumed_ledger),
                        "attempts": attempts,
                    }
                except Exception as exc:
                    acquisition_path.unlink(missing_ok=True)
                    attempts.append({
                        "method": method,
                        "status": "FAIL",
                        "elapsed": round(time.time()-started, 3),
                        "candidate": candidate.summary(),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
        time.sleep(0.5)
        # Allow newly mounted image/download UI to be rediscovered.
        seen_candidate_keys.clear()

    compact = attempts[-12:]
    if strict_scope and not attempts:
        if diagnostic_callback:
            try:
                diagnostic_callback({
                    **last_discovery_diagnostic,
                    "phase": "download_no_fresh_artifact",
                    "poll": poll_count,
                    "elapsed_sec": round(max(0.0, time.time() - started_at), 3),
                    "result": "FAIL",
                })
            except Exception:
                pass
        scope_detail = json.dumps(scope or {}, ensure_ascii=False, sort_keys=True)
        raise ArtifactTransferError(
            "[ARTIFACT_DOWNLOAD_FAILED_NO_FRESH_ARTIFACT] no downloadable artifact was proven "
            f"inside the current request scope; stale/page-global fallback is forbidden; scope={scope_detail}"
        )
    raise ArtifactTransferError(
        "[ARTIFACT_DOWNLOAD_FAILED] exhausted download strategies; stale artifact reuse is forbidden; "
        + json.dumps(compact, ensure_ascii=False)
    )


def download_latest_artifact(page, browser_context, output_path: str,
                             timeout_sec: float = 45.0,
                             expected_filename: str = "", *, scope: Optional[dict] = None,
                             strict_scope: bool = False, popup_guard=None,
                             manifest: ArtifactManifest | None = None,
                             consumed_ledger: ArtifactConsumedLedger | None = None,
                             diagnostic_callback=None) -> str:
    """Stage 1-compatible API returning only the downloaded path."""
    result = download_latest_artifact_with_evidence(
        page, browser_context, output_path,
        timeout_sec=timeout_sec,
        expected_filename=expected_filename, scope=scope, strict_scope=strict_scope,
        popup_guard=popup_guard, manifest=manifest, consumed_ledger=consumed_ledger,
        diagnostic_callback=diagnostic_callback,
    )
    return str(result["path"])


def run_artifact_transfer_self_tests() -> dict:
    """Pure deterministic regression tests; no browser/network required."""
    results: dict[str, bool] = {}
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    jpg = b"\xff\xd8\xff" + b"x" * 64 + b"\xff\xd9"
    html = b"<!doctype html><html>login</html>"
    results["png_magic_valid"] = _validate_download_bytes(png, expected_suffix=".png")[0]
    results["jpg_magic_valid"] = _validate_download_bytes(jpg, expected_suffix=".jpg")[0]
    results["html_rejected"] = not _validate_download_bytes(html, content_type="text/html")[0]
    results["empty_rejected"] = not _validate_download_bytes(b"")[0]
    data, ctype = _decode_data_url("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
    results["data_url_decode"] = data == png and ctype == "image/png"
    results["image_ext_detect"] = _detect_image_ext(png) == ".png" and _detect_image_ext(jpg) == ".jpg"
    results["format_mismatch_rejected"] = not _validate_download_bytes(png, expected_suffix=".jpg")[0]
    results["url_filename"] = _filename_from_url("https://x.test/files/demo.png?x=1") == "demo.png"
    # Generated images must score above generic download chrome.
    image_score = _candidate_score(
        kind="image", href="", src="https://x.test/generated/image.png", filename="image.png",
        text="", aria_label="generated image", title="", testid="", expected_name="", root_rank=0,
    )
    generic_score = _candidate_score(
        kind="button", href="", src="", filename="", text="Download", aria_label="Download",
        title="", testid="", expected_name="", root_rank=4,
    )
    results["generated_image_ranked"] = image_score > generic_score
    c1 = ArtifactCandidate(element=None, kind="image", score=1, src="https://x.test/old.png")
    c2 = ArtifactCandidate(element=None, kind="image", score=1, src="https://x.test/new.png")
    results["artifact_identity_changes_with_src"] = c1.identity_signature() != c2.identity_signature()
    results["all_passed"] = all(results.values())
    return results
