"""Request-anchored deployment target contract (read-only, fail closed).

This gate checks *where* work was delivered, independently of generic Action PASS.
It never moves, overwrites, creates, or grants access to files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Mapping

SCHEMA = "SMARTAGENT_TARGET_PATH_CONTRACT_V1"
_LABEL = re.compile(
    r"(?:預期部署根目錄|指定部署根目錄|目標部署根目錄|部署根目錄|指定部署路徑|"
    r"目標部署路徑|預期部署路徑|expected deployment root|deployment target root|"
    r"deployment root|target deployment path)\s*[:：]\s*"
    r"(?P<path>[A-Za-z]:[\\/][^\s\`\"'<>|?*，,。;；)\]]+|/[^\s\`\"'<>|?*，,。;；)\]]+)",
    re.IGNORECASE,
)
_RELOCATION = re.compile(
    r"(?:如果|若|if)\s*.{0,100}(?:覆蓋|overwrit|衝突|conflict)"
    r".{0,110}(?:新.*(?:目錄|資料夾)|獨立.*(?:目錄|資料夾)|alternative|separate directory)",
    re.IGNORECASE | re.DOTALL,
)
_ENTRIES = re.compile(
    r"(?:預期包含|部署後包含|應包含|必須包含|目錄下應有|目錄下需有|"
    r"expected top.level entries|expected root entries)\s*[:：]\s*([^\r\n]{1,220})",
    re.IGNORECASE,
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_MAX_ENTRIES = 96


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def extract_target_contract(request: str) -> dict | None:
    """Only explicit labeled target paths; never infer a target from incidental paths."""
    text = str(request or "")
    if not re.search(r"部署|deploy|extract|解壓|安裝", text, re.IGNORECASE):
        return None
    matches = [m.group("path").rstrip("\\/") for m in _LABEL.finditer(text)]
    if not matches:
        return None
    normalized = {os.path.normcase(os.path.normpath(p)) for p in matches}
    path = matches[0] if len(normalized) == 1 else ""
    items = []
    for section in _ENTRIES.findall(text):
        for token in re.split(r"[,，、;；\s]+", section):
            token = token.strip("\`\"'()[]{}")
            if _NAME.fullmatch(token) and token not in items:
                items.append(token)
    return {
        "schema": SCHEMA,
        "requested_target": path,
        "binding_status": "BOUND" if path else "AMBIGUOUS",
        "relocation_permitted_if_collision": bool(_RELOCATION.search(text)),
        "required_top_level_entries": items[:24],
        "source": "ORIGINAL_REQUEST_EXPLICIT_LABEL",
        "request_anchor_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "authorization_granted": False,
    }


def observe_target(path: str, security_context, *, max_entries: int = _MAX_ENTRIES) -> dict:
    """Small, nonrecursive, authorized stat/list, including missing-vs-denied."""
    try:
        if not isinstance(path, str) or not path:
            return {"state": "UNKNOWN", "reason": "AMBIGUOUS_TARGET"}
        resolved = security_context.require_read(path, require_absolute=True)
        try:
            info = resolved.lstat()
        except FileNotFoundError:
            return {"state": "ABSENT", "reason": "TARGET_NOT_FOUND",
                    "resolved_path": str(resolved), "entries": {}}
        if not stat.S_ISDIR(info.st_mode):
            return {"state": "CONFLICT", "reason": "TARGET_IS_NOT_DIRECTORY",
                    "resolved_path": str(resolved), "entries": {}}
        entries = {}
        with os.scandir(resolved) as iterator:
            for index, entry in enumerate(iterator):
                if index >= max_entries:
                    return {"state": "UNKNOWN", "reason": "DIRECTORY_ENTRY_LIMIT",
                            "resolved_path": str(resolved), "entries": {}}
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or bool(
                    getattr(metadata, "st_file_attributes", 0) & 0x400
                ):
                    return {"state": "UNKNOWN", "reason": "REPARSE_COMPONENT_PRESENT",
                            "resolved_path": str(resolved), "entries": {}}
                kind = ("DIRECTORY" if stat.S_ISDIR(metadata.st_mode) else
                        "FILE" if stat.S_ISREG(metadata.st_mode) else "OTHER")
                entries[entry.name] = {
                    "kind": kind, "bytes": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                }
        return {"state": "OCCUPIED" if entries else "EMPTY",
                "reason": "DIRECTORY_OBSERVED", "resolved_path": str(resolved),
                "entries": entries}
    except Exception as exc:
        # Missing is authoritative only for FileNotFoundError from a scoped
        # stat. Authorization and I/O failures can NEVER prove absence.
        return {"state": "UNKNOWN", "reason": type(exc).__name__, "entries": {}}


def _at_root(post: Mapping, required: list[str]) -> bool:
    entries = post.get("entries") or {}
    if required:
        return all(name in entries for name in required)
    # Without a declared manifest, one wrapper directory is ambiguous:
    # it might be the actual destination instead of the requested root.
    return bool(entries) and (
        len(entries) > 1
        or next(iter(entries.values())).get("kind") == "FILE"
    )


def _candidate_alt(root: Path, pre: Mapping, post: Mapping, security_context,
                   required: list[str]) -> str:
    """Only a newly created independent direct child, never an old folder."""
    new_names = set(post.get("entries") or {}) - set(pre.get("entries") or {})
    matches = []
    for name in sorted(new_names):
        if (post["entries"][name]["kind"] != "DIRECTORY"
                or not _NAME.fullmatch(name)):
            continue
        candidate = root / name
        observed = observe_target(str(candidate), security_context)
        if observed["state"] not in {"EMPTY", "OCCUPIED"}:
            continue
        if _at_root(observed, required) and observed["state"] == "OCCUPIED":
            matches.append(str(candidate))
            continue
        # Permit one additional package wrapper only if explicit required
        # root entries verify inside it; otherwise its layout is unproven.
        if required and len(observed["entries"]) == 1:
            wrapper = next(iter(observed["entries"]))
            if observed["entries"][wrapper]["kind"] == "DIRECTORY":
                inner = candidate / wrapper
                check = observe_target(str(inner), security_context)
                if check["state"] == "OCCUPIED" and _at_root(check, required):
                    matches.append(str(inner))
    return matches[0] if len(matches) == 1 else ""


def verify_target_completion(contract: Mapping | None, baseline: Mapping | None,
                             security_context, *, final_text: str = "",
                             evidence_pass: bool = False) -> dict:
    """Admission-only path compliance, not proof of source/checksum correctness.

    Generic verification remains an independent, necessary prerequisite.
    """
    if contract is None:
        return {"status": "NOT_APPLICABLE", "reason": "NO_EXPLICIT_TARGET_CONTRACT"}
    requested = str(contract.get("requested_target") or "")
    result = {
        "schema": SCHEMA, "status": "UNVERIFIED",
        "reason": "", "requested_target": requested, "actual_target": "",
        "pre_mutation_state": (baseline or {}).get("state", "UNKNOWN"),
        "post_observation_state": "UNKNOWN",
        "relocation_justification": "NONE",
        "pre_mutation_evidence": bool(baseline and baseline.get("reason") in
            {"TARGET_NOT_FOUND", "DIRECTORY_OBSERVED"}),
        "authorization_granted": False,
        "mutation_applied": False,
    }
    if not requested or contract.get("binding_status") != "BOUND":
        result["reason"] = "REQUEST_TARGET_AMBIGUOUS"
        return result
    if not evidence_pass:
        result["reason"] = "INDEPENDENT_ACTION_VERIFICATION_REQUIRED"
        return result
    pre = dict(baseline or {})
    post = observe_target(requested, security_context)
    result["post_observation_state"] = post["state"]
    if pre.get("state") not in {"ABSENT", "EMPTY", "OCCUPIED"}:
        result["reason"] = "PRE_MUTATION_EVIDENCE_UNVERIFIED"
        return result
    if post["state"] not in {"EMPTY", "OCCUPIED"}:
        result["reason"] = "REQUESTED_TARGET_POSTCONDITION_MISSING"
        return result
    if (pre.get("resolved_path") != post.get("resolved_path")):
        result["reason"] = "TARGET_IDENTITY_CHANGED"
        return result
    required = list(contract.get("required_top_level_entries") or [])
    if _at_root(post, required) and post["state"] == "OCCUPIED":
        old = pre.get("entries") or {}
        changed_old = [name for name in old if
                       name in post["entries"]
                       and old[name] != post["entries"][name]]
        if changed_old:
            # An occupied root is not permission to overwrite its contents.
            # Metadata change is conservative: it requires independent
            # no-clobber reconciliation before an accepted terminal claim.
            result["reason"] = "PREEXISTING_ENTRY_CHANGED_REQUIRES_RECONCILIATION"
            return result
        new_entries = set(post["entries"]) - set(old)
        if (pre["state"] == "ABSENT"
                or (required and any(name in new_entries for name in required))
                or (not required and bool(new_entries))):
            result.update(status="PASS", reason="REQUESTED_ROOT_VERIFIED",
                          actual_target=requested)
            return result
        result["reason"] = "UNCHANGED_EXISTING_CONTENT_REQUIRES_INDEPENDENT_REUSE_PROOF"
        return result
    # An exception is permissible only with *pre-mutation* collision
    # evidence; directory existence alone does not prove overwrite risk.
    if not contract.get("relocation_permitted_if_collision"):
        result["reason"] = "DEPLOYMENT_LAYOUT_NOT_AT_REQUESTED_ROOT"
        return result
    if pre.get("state") != "OCCUPIED" or not required:
        result["reason"] = "OVERWRITE_RISK_NOT_ESTABLISHED"
        return result
    collisions = set(pre.get("entries") or {}) & set(required)
    if not collisions:
        result["reason"] = "OVERWRITE_RISK_NOT_ESTABLISHED"
        return result
    target = _candidate_alt(Path(requested), pre, post, security_context, required)
    if not target:
        result["reason"] = "RELOCATION_TARGET_UNVERIFIED"
        return result
    if os.path.normcase(os.path.normpath(target)) not in os.path.normcase(
            os.path.normpath(final_text.replace("/", "\\"))) and target not in final_text:
        result["reason"] = "RELOCATION_NOT_DISCLOSED_TO_USER"
        return result
    result.update(status="PASS", reason="AUTHORIZED_RELOCATION_VERIFIED",
                  actual_target=target, relocation_justification="PREEXISTING_ENTRY_COLLISION")
    return result
