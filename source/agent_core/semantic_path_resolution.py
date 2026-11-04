"""SPR V1 — bounded request-source path candidate resolution.

This is a read-only preflight *input* resolver, not a permission check or a
Planner-issued authorization. Candidates must be literal prefixes of path
spans in the user's original request. Filesystem existence alone never grants
write authority. Scope/ACL/Tool admission remain independent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Callable

SCHEMA = "SMARTAGENT_SEMANTIC_PATH_RESOLUTION_V1"
_DRIVE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/])")
_SENTENCE = re.compile(r"[\r\n，；。;]|[,](?=\s|$)")
_HAN = re.compile(r"[\u3400-\u9fff]")
_QUOTES = {'"': '"', "'": "'", "\u300c": "\u300d", "\u300e": "\u300f", "\x60": "\x60"}
_MAX_SPANS = 24
_MAX_CANDIDATES = 64


class SemanticPathError(ValueError):
    pass


# Positive effect intentions constrain *path narrowing*. Negative phrases do
# not confer authority and must not be mistaken for positive instructions.
_EFFECT_IMPERATIVE = re.compile(
    r"(?i)(?<![A-Za-z0-9_\\])(?:請|幫我|麻煩|再|然後|接著|務必)?"
    r"\s*(?:建立|新增|刪除|修改|覆寫|移動|搬移|安裝|寫入|"
    r"create\b|delete\b|modify\b|overwrite\b|install\b|move\b)"
)


def readonly_narrowing_allowed(request: str, lexical_paths: list[str]) -> bool:
    """Use only the instruction prefix before the first literal path.

    A filename such as Camera Test Data is not an EXECUTE instruction.
    Following negative clauses such as '禁止任何寫入或安裝' must not turn
    an otherwise read-only directory observation into a mutation request.
    This preflight never establishes write permission, even when allowed.
    """
    text = str(request or "")
    positions = [text.find(value) for value in lexical_paths if value and text.find(value) >= 0]
    prefix = text[:min(positions)] if positions else text
    return not bool(_EFFECT_IMPERATIVE.search(prefix))


def _path_exists(path: str, probe: Callable[[str], bool]) -> bool:
    try:
        return bool(probe(path))
    except (OSError, RuntimeError, ValueError):
        return False


def _raw_span(request: str, offset: int) -> tuple[str, int, bool]:
    """Discover a bounded original-user substring, preserving spaces."""
    quote = request[offset - 1] if offset else ""
    closer = _QUOTES.get(quote)
    if closer:
        end = request.find(closer, offset)
        if end >= 0:
            return request[offset:end], end, True
    stop = len(request)
    sentence = _SENTENCE.search(request, offset)
    if sentence:
        stop = min(stop, sentence.start())
    next_drive = _DRIVE.search(request, offset + 3)
    if next_drive:
        stop = min(stop, next_drive.start())
    return request[offset:stop].rstrip(), stop, False


def _cutpoints(raw: str):
    """Possible lexical cuts, not a vocabulary of Chinese instruction words.

    An unquoted Chinese continuation can start immediately after a directory
    name or after a space. Never truncate inside a downstream path separator.
    English/Latin tokens after a space stay ambiguous unless explicitly quoted.
    """
    for i in range(3, len(raw)):
        suffix = raw[i:]
        if not suffix or (suffix[0] not in " \t" and not _HAN.match(suffix[0])):
            continue
        suffix = suffix.lstrip(" \t")
        if not suffix or not _HAN.match(suffix[0]):
            continue
        if "\\" in suffix or "/" in suffix or ":" in suffix:
            continue
        prefix = raw[:i].rstrip(" \t")
        if (len(prefix) < 4 or prefix.endswith(("\\", "/"))
                or prefix == raw or not _DRIVE.match(prefix)):
            continue
        yield i, prefix


def analyze_request_paths(
    request: str,
    lexical_paths: list[str],
    *,
    allow_readonly_narrowing: bool,
    probe: Callable[[str], bool] | None = None,
) -> dict:
    """Resolve one literal path span at a time, with conservative ambiguity.

    Lexical paths are produced by the existing V9 parser, preserving JSON,
    quotes, saved-plan isolation and ADS guards. Unresolvable path literals
    are kept verbatim, never replaced by a parent or workspace fallback.
    """
    source = str(request or "")
    probe = probe or (lambda value: Path(value).exists())
    resolved: list[str] = []
    records: list[dict] = []
    scan_from = 0
    for lexical in lexical_paths[:_MAX_SPANS]:
        value = str(lexical)
        offset = source.find(value, scan_from)
        # Quoted JSON uses escaped backslashes; the existing parser owns it.
        if offset < 0:
            records.append({"original": value, "selected": value,
                            "status": "LEXICAL_EXACT", "candidates": []})
            resolved.append(value)
            continue
        scan_from = offset + len(value)
        raw, end, quoted = _raw_span(source, offset)
        # The existing tokenizer is authoritative on already terminated spans.
        # Never reinterpret an ADS suffix as natural language and shorten it.
        if (quoted or not raw.startswith(value)
                or ":" in raw[2:]
                or not allow_readonly_narrowing):
            records.append({"original": value, "selected": value,
                            "status": "LEXICAL_EXACT", "candidates": []})
            resolved.append(value)
            continue
        paths = [raw]
        if value != raw:
            paths.append(value)
        paths.extend(prefix for _, prefix in _cutpoints(raw))
        paths = list(dict.fromkeys(paths))[:_MAX_CANDIDATES]
        candidates = [{
            "value": path, "span_start": offset, "span_end": offset + len(path),
            "existence": "EXISTS" if _path_exists(path, probe) else "ABSENT",
        } for path in paths]
        present = [item for item in candidates if item["existence"] == "EXISTS"]
        # If a longer existing path also consumes Chinese words, the natural
        # language boundary is inherently ambiguous: do not guess the shorter.
        if len(present) > 1:
            status, selected = "AMBIGUOUS", None
        elif len(present) == 1:
            status, selected = "VERIFIED_UNIQUE", present[0]["value"]
        else:
            status, selected = "LEXICAL_UNVERIFIED", value
        records.append({"original": value, "selected": selected,
                        "status": status, "candidates": candidates})
        if selected is not None:
            resolved.append(selected)
    if len(lexical_paths) > _MAX_SPANS:
        raise SemanticPathError("semantic_path_candidate_limit_exceeded")
    status = ("AMBIGUOUS" if any(row["status"] == "AMBIGUOUS" for row in records)
              else "VERIFIED_UNIQUE" if any(row["status"] == "VERIFIED_UNIQUE" for row in records)
              else "LEXICAL_EXACT")
    return {"schema": SCHEMA, "status": status, "paths": list(dict.fromkeys(resolved)),
            "records": records, "original_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "grants_authorization": False}


def require_resolved_paths(document: dict) -> list[str]:
    if document.get("status") == "AMBIGUOUS":
        raise SemanticPathError("semantic_path_resolution_ambiguous")
    return list(document["paths"])



def validate_planner_path_confirmation(proposal: object, document: dict | None) -> dict:
    """Validate a Planner's semantic choice against original-request proof.

    This is never a source of new path or write authorization. The only
    accepted selection is the one already established by read-only preflight.
    A different (even existing) parent, child or sibling fails closed.
    """
    if (not isinstance(proposal, dict)
            or set(proposal) != {"schema", "selected_paths"}
            or proposal.get("schema") != SCHEMA):
        raise SemanticPathError("semantic_path_planner_schema_invalid")
    values = proposal.get("selected_paths")
    if (not isinstance(values, list) or len(values) > _MAX_SPANS
            or any(type(x) is not str or not x for x in values)
            or len(set(values)) != len(values)):
        raise SemanticPathError("semantic_path_planner_selection_invalid")
    if not isinstance(document, dict) or document.get("status") == "AMBIGUOUS":
        raise SemanticPathError("semantic_path_planner_preflight_unavailable")
    if values != list(document.get("paths") or []):
        raise SemanticPathError("semantic_path_planner_selection_mismatch")
    return {"schema": SCHEMA, "selected_paths": list(values)}



def persist_request_path_resolution(
    base: str | Path, *, request_id: str, task_epoch: str,
    original_request: str, document: dict,
) -> Path:
    """Durable, request/epoch and original-digest-bound preflight ledger."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(request_id)) or not str(task_epoch):
        raise SemanticPathError("semantic_path_identity_invalid")
    if hashlib.sha256(original_request.encode("utf-8")).hexdigest() != document.get("original_sha256"):
        raise SemanticPathError("semantic_path_original_digest_mismatch")
    directory = Path(base) / "localdata" / "runtime" / "path_resolution"
    path = directory / (request_id + ".json")
    payload = {**document, "request_id": request_id, "task_epoch": task_epoch}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SemanticPathError("semantic_path_ledger_unreadable") from exc
        if (old.get("request_id") != request_id or old.get("task_epoch") != task_epoch
                or old.get("original_sha256") != document["original_sha256"]):
            raise SemanticPathError("semantic_path_ledger_identity_mismatch")
        if old.get("paths") != document["paths"] or old.get("status") != document["status"]:
            raise SemanticPathError("semantic_path_ledger_resolution_drift")
        return path
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def planner_path_context(document: dict) -> str:
    """Candidate data only. The Planner cannot authorize a different target."""
    bounded = {
        "schema": SCHEMA, "status": document["status"], "paths": document["paths"][:24],
        "candidates": [
            {"original": row["original"], "selected": row["selected"],
             "status": row["status"], "candidates": row["candidates"][:16]}
            for row in document["records"][:24]
        ],
        "source": "VERIFIED_ORIGINAL_REQUEST_SPANS",
        "authorization": "NONE; existing ACL and Tool Scope still apply",
    }
    return ("[SMARTAGENT_SEMANTIC_PATH_RESOLUTION_V1]\n"
            + json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
            + "\n[/SMARTAGENT_SEMANTIC_PATH_RESOLUTION_V1]\n"
            "This is Runtime preflight data, not an action/effect or permission. "
            "Planner may optionally confirm the semantic boundary in "
            "report_progress.path_resolution with exactly this schema and "
            "selected_paths: " + SCHEMA + " and the paths listed above. "
            "Runtime verifies exact matches before Tool admission; this "
            "model proposal cannot authorize a new path. "
            "Use only the selected paths in canonical read actions. Do not "
            "retry ABSENT candidates, invent new paths, or expand to parents. "
            "If AMBIGUOUS, request a quoted exact path; do not issue file actions.")
