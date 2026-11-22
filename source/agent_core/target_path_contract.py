"""Request-anchored deployment target contract (read-only, fail closed).

This gate checks *where* work was delivered, independently of generic Action PASS.
It never moves, overwrites, creates, or grants access to files.
"""
from __future__ import annotations

import hashlib
import ntpath
import json
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from .terminal_evidence_reconciliation import (
    capture_existing_tree, reconcile_existing_tree,
)

SCHEMA = "SMARTAGENT_TARGET_PATH_CONTRACT_V1"
_LABEL = re.compile(
    r"(?:預期部署根目錄|指定部署根目錄|目標部署根目錄|部署根目錄|指定部署路徑|"
    r"目標部署路徑|預期部署路徑|expected deployment root|deployment target root|"
    r"deployment root|target deployment path)\s*[:：]\s*(?:\*\*)?\s*[\x60\x22\x27\u201c]?"
    r"(?P<path>[A-Za-z]:[\\/][^\s\`\"'<>|?*，,。;；)\]]+|/[^\s\`\"'<>|?*，,。;；)\]]+)",
    re.IGNORECASE,
)
_EXPLICIT_VERB_TARGET = re.compile(
    r"(?:部署|解壓|安裝|deploy|extract|install).{0,16}?"
    r"(?:到|至|於|into|to)\s*[:：]?\s*[`\"']?"
    r"(?P<path>[A-Za-z]:[\\/][^\s`\"'<>|?*，,。;；)\]]+|/[^\s`\"'<>|?*，,。;；)\]]+)",
    re.IGNORECASE,
)
_EXPLICIT_IN_TARGET = re.compile(
    r"(?:在|於|under|within)\s*[`\"']?"
    r"(?P<path>[A-Za-z]:[\\/][^\s`\"'<>|?*，,。;；)\]]+|/[^\s`\"'<>|?*，,。;；)\]]+)"
    r"\s*(?:下|中|內|底下|之下)?\s*(?:進行|執行|完成)?\s*"
    r"(?:部署|解壓|安裝|deploy|extract|install)",
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
    # Extend the *existing* target binding with an exact directory-only
    # contract. The same SCR translator owns verbs and object classes;
    # this function owns only the original-request literal target.
    from .semantic_capability_resolution import translate_goal_action
    translated = translate_goal_action(text)
    if translated["semantic_intent"] == "CREATE_DIRECTORY":
        # A quoted path may include spaces. An unquoted path is bounded by
        # whitespace/punctuation; never invent a destination or fall back
        # to an existing parent/workspace.
        quoted = re.findall(
            r'["\x60]((?:[A-Za-z]:[\\/]|/)[^"\x60\r\n<>|?*]+)["\x60]',
            text,
        )
        unquoted = re.findall(
            r'(?<![A-Za-z0-9])((?:[A-Za-z]:[\\/]|/)[^\s\x60"<>|?*,，。；;\r\n\)\]]+)',
            text,
        )
        candidates = quoted or unquoted
        # A quoted literal is authoritative over substring lexical matches.
        normalized = {
            ntpath.normcase(ntpath.normpath(str(value).strip()))
            for value in candidates
        }
        exact = str(candidates[0]).strip() if candidates and len(normalized) == 1 else ""
        if exact and not (
            re.match(r'^[A-Za-z]:[\\/][^\\/]+', exact)
            or (exact.startswith("/") and not exact.startswith("//") and len(exact) > 1)
        ):
            exact = ""
        return {
            "schema": SCHEMA,
            "requested_target": exact,
            "binding_status": "BOUND" if exact else "AMBIGUOUS",
            "target_kind": "DIRECTORY_ONLY",
            "goal_intent": translated["semantic_intent"],
            "canonical_tool": translated["canonical_tool"],
            "semantic_translation_schema": translated["schema"],
            "relocation_permitted_if_collision": False,
            "required_top_level_entries": [],
            "source": "ORIGINAL_REQUEST_DIRECTORY_INTENT",
            "revalidation_requested": False,
            "request_anchor_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "authorization_granted": False,
        }
    # The target label alone is NOT an imperative to deploy. In particular,
    # observation-only requests must not be forced through a mutation gate.
    intent = _LABEL.sub(" ", text)
    if not re.search(r"部署|deploy|extract|解壓|安裝", intent, re.IGNORECASE):
        return None
    readonly = re.search(
        r"不(?:要|需|進行)?部署|禁止部署|只(?:需|要)?(?:檢查|驗證|確認)|"
        r"唯讀(?:檢查|驗證|觀測)", intent, re.IGNORECASE)
    followed_by_mutation = re.search(
        r"(?:然後|再|之後|接著|then).{0,80}(?:部署|安裝|解壓|deploy|install|extract)",
        intent, re.IGNORECASE | re.DOTALL)
    if readonly and not followed_by_mutation:
        return None
    def clean_match(value: str) -> str:
        # Preserve Windows drive roots, e.g. E:\\, rather than turning
        # them into ambiguous drive-relative E:.
        return value if re.fullmatch(r"[A-Za-z]:[\\/]", value) else value.rstrip("\\/")
    matches = [clean_match(m.group("path")) for m in _LABEL.finditer(text)]
    matches.extend(clean_match(m.group("path")) for expression in
                   (_EXPLICIT_VERB_TARGET, _EXPLICIT_IN_TARGET)
                   for m in expression.finditer(text))
    # A deployment label is a declared destination contract even when the
    # path is malformed or wrapped unexpectedly. Never silently disable the
    # terminal gate after failing to extract the requested target.
    if not matches and not re.search(
        r"(?:預期部署根目錄|指定部署根目錄|目標部署根目錄|部署根目錄|"
        r"指定部署路徑|目標部署路徑|預期部署路徑|"
        r"expected deployment root|deployment target root|deployment root|"
        r"target deployment path)\s*[:：]",
        text, re.IGNORECASE,
    ):
        return None
    def normalize_target(value: str) -> str:
        return (ntpath.normcase(ntpath.normpath(value))
                if re.match(r"^[A-Za-z]:[\\/]", value)
                else os.path.normcase(os.path.normpath(value)))
    normalized = {normalize_target(p) for p in matches}
    path = matches[0] if matches and len(normalized) == 1 else ""
    items = []
    for section in _ENTRIES.findall(text):
        for token in re.split(r"[,，、;；\s]+", section):
            token = token.strip("`\"'()[]{}。．，；！？」")
            if _NAME.fullmatch(token) and token not in items:
                items.append(token)
    return {
        "schema": SCHEMA,
        "requested_target": path,
        "binding_status": "BOUND" if path else "AMBIGUOUS",
        "relocation_permitted_if_collision": bool(_RELOCATION.search(text)),
        "required_top_level_entries": items[:24],
        "source": "ORIGINAL_REQUEST_EXPLICIT_LABEL",
        "revalidation_requested": bool(re.search(
            r"重新驗證|再次驗證|重新確認|復核|revalidat|re-check|verify existing",
            text, re.IGNORECASE)),
        "request_anchor_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "authorization_granted": False,
    }


def observe_target(path: str, security_context, *, max_entries: int = _MAX_ENTRIES,
                   capture_preexisting: bool = False) -> dict:
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
        observed = {"state": "OCCUPIED" if entries else "EMPTY",
                    "reason": "DIRECTORY_OBSERVED", "resolved_path": str(resolved),
                    "entries": entries}
        if capture_preexisting and entries:
            # This must run at request entry, BEFORE any Tool Action. The
            # bounded baseline is owned by Runtime, never model-provided.
            observed["preservation_baseline"] = capture_existing_tree(
                resolved, security_context)
        return observed
    except Exception as exc:
        # Missing is authoritative only for FileNotFoundError from a scoped
        # stat. Authorization and I/O failures can NEVER prove absence.
        return {"state": "UNKNOWN", "reason": type(exc).__name__, "entries": {}}


def _at_root(post: Mapping, required: list[str]) -> bool:
    entries = post.get("entries") or {}
    if required:
        return all(name in entries for name in required)
    # Runtime/cache bookkeeping does not constitute a deployed payload.
    payload = {name: value for name, value in entries.items()
               if name not in {".agents", ".git", ".cache", "__pycache__", "tmp"}}
    # Without a declared manifest, one wrapper directory is ambiguous:
    # it might be the actual destination instead of the requested root.
    return bool(payload) and (
        len(payload) > 1
        or next(iter(payload.values())).get("kind") == "FILE"
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
                             evidence_pass: bool = False,
                             reuse_verification_ref: str = "") -> dict:
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
    old = pre.get("entries") or {}
    if old:
        # Metadata (especially directory mtime/size) is NOT content evidence.
        # Reconcile every preexisting path against a bounded, pre-mutation
        # file-content hash + file identity snapshot. Missing or changed
        # original content stays blocked; new siblings/children are permitted.
        preserved = reconcile_existing_tree(
            Path(requested), security_context, pre.get("preservation_baseline"))
        result["preservation_status"] = preserved["status"]
        result["preservation_reason"] = preserved["reason"]
        result["preservation_verified_nodes"] = preserved["verified_nodes"]
        result["preservation_verified_bytes"] = preserved["verified_bytes"]
        result["metadata_only_directory_changes"] = sum(
            original.get("kind") == "DIRECTORY"
            and name in post["entries"]
            and post["entries"][name].get("kind") == "DIRECTORY"
            and original != post["entries"][name]
            for name, original in old.items())
        if preserved["status"] != "VERIFIED":
            if preserved["reason"].startswith("PREEXISTING_"):
                result["reason"] = preserved["reason"]
            else:
                result["reason"] = "PREEXISTING_CONTENT_EVIDENCE_UNVERIFIED"
            return result
    else:
        result["preservation_status"] = "NOT_REQUIRED"
        result["preservation_reason"] = "NO_PREEXISTING_ENTRIES"
        result["preservation_verified_nodes"] = 0
        result["preservation_verified_bytes"] = 0
        result["metadata_only_directory_changes"] = 0
    if contract.get("target_kind") == "DIRECTORY_ONLY":
        # A folder-only Goal must not require payload files. Verification
        # and preexisting-data preservation above still apply.
        result.update(
            status="PASS", reason=(
                "DIRECTORY_ROOT_CREATED_AND_VERIFIED"
                if pre["state"] == "ABSENT"
                else "EXISTING_DIRECTORY_VERIFIED"
            ),
            actual_target=requested,
            mutation_applied=pre["state"] == "ABSENT",
        )
        return result
    if _at_root(post, required) and post["state"] == "OCCUPIED":
        new_entries = set(post["entries"]) - set(old)
        if (pre["state"] == "ABSENT"
                or (required and any(name in new_entries for name in required))
                or (not required and bool(new_entries))):
            result.update(status="PASS", reason="REQUESTED_ROOT_VERIFIED",
                          actual_target=requested)
            return result
        if (required and contract.get("revalidation_requested") is True
                and isinstance(reuse_verification_ref, str)
                and reuse_verification_ref):
            # Existing destination has all required direct-root entries,
            # prior bytes/identities still match, and the *current* request
            # supplied a referenced VERIFY PASS bound to this exact cwd.
            # This accepts reuse; it does not infer a new copy/mutation.
            result.update(status="PASS", reason="EXISTING_ROOT_REVALIDATED",
                          actual_target=requested,
                          reuse_verification_ref=reuse_verification_ref,
                          mutation_applied=False)
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
