"""Bounded, provenance-linked source candidates. Never permission.

Only literal user source labels, current-epoch committed Runtime directory
listing receipts and explicitly injected Runtime-owned historic metadata.
A listed directory is NOT proof of deployment source integrity.
"""
from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Mapping

from .tool_result_contract import decode_result
from .goal_gap_ledger import _path_key

SCHEMA = "SMARTAGENT_SOURCE_CANDIDATE_LEDGER_V1"
_LABEL = re.compile(
    r"(?:先前|既有|原始|已知)?(?:部署)?來源(?:資料夾|目錄|路徑)?"
    r"\s*[:：]\s*[\"']?(?P<path>[A-Za-z]:[\\/][^\r\n，；。\"']+)",
    re.I,
)
_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,180}$")


def _absolute(path: object) -> bool:
    raw = str(path or "").strip()
    return (len(raw) <= 1024
            and bool(re.match(r"^[A-Za-z]:[\\/]", raw))
            and "\x00" not in raw
            and not any(part in (".", "..") for part in re.split(r"[\\/]", raw[3:])))


def _scope(path: str, declared: list[str]) -> str:
    key = _path_key(path)
    for raw in declared[:32]:
        root = _path_key(raw)
        if key and key == root:
            return "EXACT_DECLARED_PATH"
        if key and root and key.startswith(root.rstrip("\\") + "\\"):
            return "WITHIN_DECLARED_PATH"
    return "OUTSIDE_DECLARED_PATHS"


def valid_list_receipt(row: Mapping, original: Mapping, request_id: str,
                       epoch: str) -> dict | None:
    if (row.get("request_id") != request_id or row.get("task_epoch") != epoch
            or row.get("evidence_status", "ACTIVE") != "ACTIVE"
            or row.get("executed") is not True
            or row.get("result_status") != "SUCCEEDED"
            or row.get("tool") != "list_directory"):
        return None
    action = original.get("action") or {}
    receipt = decode_result(original.get("result", ""))
    if (action.get("tool") != "list_directory"
            or not isinstance(receipt, Mapping)
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("executed") is not True
            or receipt.get("reason") != "DIRECTORY_LISTED"
            or receipt.get("tool") != "list_directory"
            or _path_key(receipt.get("requested_path")) != _path_key(action.get("path"))
            or not isinstance((receipt.get("data") or {}).get("entries"), list)):
        return None
    return receipt


def build_source_candidate_ledger(runtime, gap_view: Mapping) -> dict:
    """Reconstruct without disk reads, cross-epoch facts, or scope expansion."""
    progress = getattr(runtime, "progress_ledger", None)
    goal = str(getattr(progress, "goal", "") or getattr(runtime, "task_request", "") or "")
    req = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    declared = list(getattr(runtime, "authorized_paths", []) or [])
    targets = {_path_key(row.get("path")) for row in
               list(gap_view.get("known_absences") or []) if isinstance(row, Mapping)}
    contract = getattr(runtime, "_target_path_contract", None) or {}
    if isinstance(contract, Mapping):
        targets.add(_path_key(contract.get("requested_target")))
    targets.discard("")
    records: dict[str, dict] = {}

    def add(path: object, origin: str, ref: str, *, rank: int,
            existence: str = "UNKNOWN", parent_ref: str = "",
            evidence_level: str = "E0"):
        raw = str(path or "").strip().rstrip("\\/")
        if not _absolute(raw):
            return
        key = _path_key(raw)
        if key in targets or any(key.startswith(t + "\\") for t in targets):
            return
        row = {
            "candidate_id": "SRC-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16].upper(),
            "path": raw,
            "origin": origin,
            "evidence_level": evidence_level,
            "provenance_ref": str(ref)[:160],
            "parent_listing_ref": str(parent_ref)[:160],
            "existence": existence,
            "integrity": "UNKNOWN",
            "scope_status": _scope(raw, declared),
            "read_authorized": False,
            "write_authorized": False,
            "needs_current_epoch_verification": True,
            "priority": rank,
        }
        if key not in records or rank < records[key]["priority"]:
            records[key] = row
        elif existence == "OBSERVED_PRESENT" and records[key]["existence"] == "UNKNOWN":
            records[key]["existence"] = existence

    for m in list(_LABEL.finditer(goal))[:8]:
        add(m.group("path"), "ORIGINAL_REQUEST_SOURCE_LABEL", "ORIGINAL_REQUEST", rank=0)

    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    results = dict(getattr(runtime, "action_result_ledger", {}) or {})
    for aid, row in list(results.items())[-64:]:
        if not isinstance(row, Mapping):
            continue
        original = actions.get(aid) or {}
        if not isinstance(original, Mapping):
            continue
        receipt = valid_list_receipt(row, original, req, epoch)
        if receipt is None:
            continue
        parent = str((original.get("action") or {}).get("path", ""))
        for item in list(receipt["data"]["entries"])[:128]:
            if (not isinstance(item, Mapping)
                    or item.get("kind") != "DIRECTORY"
                    or not isinstance(item.get("name"), str)
                    or not _NAME.fullmatch(item["name"])
                    or item["name"] in (".", "..")):
                continue
            add(ntpath.join(parent, item["name"]),
                "COMMITTED_PARENT_DIRECTORY_LISTING", str(aid),
                rank=2, existence="OBSERVED_PRESENT", parent_ref=str(aid),
                evidence_level="E3")

    # This optional field is only populated by trusted Runtime code.
    # Historical pointers cannot verify current existence or authorize reads.
    history = getattr(runtime, "_source_candidate_history", None)
    if isinstance(history, Mapping) and history.get("schema") == "SMARTAGENT_SOURCE_HISTORY_V1":
        for hint in list(history.get("candidates") or [])[:16]:
            if (isinstance(hint, Mapping) and hint.get("origin") in
                    {"PROJECT_LEDGER", "PREVIOUS_TASK_RECEIPT",
                     "HISTORICAL_USER_SOURCE_LABEL", "HISTORICAL_RUNTIME_LIST_RECEIPT"}
                    and hint.get("evidence_level", "E2") in {"E1", "E2"}
                    and isinstance(hint.get("provenance_ref"), str)
                    and bool(hint["provenance_ref"])):
                add(hint.get("path"), str(hint["origin"]),
                    hint["provenance_ref"], rank=1,
                    evidence_level=str(hint.get("evidence_level", "E2")))

    output = sorted(records.values(),
                    key=lambda r: (r["priority"], r["path"].casefold()))[:16]
    for row in output:
        row.pop("priority", None)
    return {
        "schema": SCHEMA,
        "request_id": req,
        "task_epoch": epoch,
        "candidates": output,
        "candidate_count": len(output),
        "historical_index_available": (
            isinstance(history, Mapping) and history.get("schema") == "SMARTAGENT_SOURCE_HISTORY_V1"
        ),
        "historical_index_status": str((history or {}).get("index_status", "UNKNOWN"))[:80]
            if isinstance(history, Mapping) else "UNKNOWN",
        "historical_bootstrap_state": str((history or {}).get("bootstrap_state", "UNKNOWN"))[:80]
            if isinstance(history, Mapping) else "UNKNOWN",
        "historical_recovered_count": int((history or {}).get("recovered_count", 0) or 0)
            if isinstance(history, Mapping) else 0,
        "historical_task_records_examined": int((history or {}).get("task_records_examined", 0) or 0)
            if isinstance(history, Mapping) else 0,
        "historical_leads_are_live_evidence": False,
        "candidate_is_authorization": False,
        "new_disk_scan_performed": False,
    }


__all__ = ["SCHEMA", "build_source_candidate_ledger", "valid_list_receipt"]
