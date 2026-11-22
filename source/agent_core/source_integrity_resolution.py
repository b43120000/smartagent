"""Independent, current-epoch source STRUCTURE evidence from list_directory.

Directory listing can prove the top-level names and types at a moment in time.
It cannot prove file hashes, semantic correctness or installation readiness.
This module never opens any candidate, mutates files or grants permission.
"""
from __future__ import annotations

from collections.abc import Mapping

from .goal_gap_ledger import _path_key
from .source_candidate_ledger import valid_list_receipt

SCHEMA = "SMARTAGENT_SOURCE_INTEGRITY_RESOLUTION_V1"


def reconcile_source_integrity(runtime, candidates: Mapping) -> dict:
    contract = getattr(runtime, "_target_path_contract", None) or {}
    required = list(contract.get("required_top_level_entries") or [])[:24] if isinstance(contract, Mapping) else []
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    results = dict(getattr(runtime, "action_result_ledger", {}) or {})
    request = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    verified: dict[str, dict] = {}
    for aid, row in list(results.items())[-64:]:
        if not isinstance(row, Mapping):
            continue
        original = actions.get(aid) or {}
        if not isinstance(original, Mapping):
            continue
        receipt = valid_list_receipt(row, original, request, epoch)
        if receipt is None:
            continue
        path = str((original.get("action") or {}).get("path", ""))
        entries = receipt["data"]["entries"]
        # Fail closed on duplicates, non-plain names, unbounded results and
        # paths where the Runtime result differs from the intended source.
        if not isinstance(entries, list) or len(entries) > 2048:
            continue
        seen: dict[str, str] = {}
        sound = True
        for item in entries:
            if (not isinstance(item, Mapping)
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("kind"), str)
                    or item.get("name") in ("", ".", "..")
                    or any(ch in item["name"] for ch in ("/", "\\"))):
                sound = False
                break
            name = item["name"].casefold()
            if name in seen:
                sound = False
                break
            seen[name] = item["kind"]
        if sound:
            verified[_path_key(path)] = {
                "evidence_ref": str(aid)[:160],
                "entries": seen,
            }
    rows = []
    for candidate in list(candidates.get("candidates") or [])[:16]:
        if not isinstance(candidate, Mapping):
            continue
        found = verified.get(_path_key(candidate.get("path")))
        missing = []
        wrong = []
        if found is not None:
            for item in required:
                actual = found["entries"].get(str(item).casefold())
                expected = "FILE" if "." in str(item) else "DIRECTORY"
                if actual is None:
                    missing.append(str(item))
                elif actual != expected:
                    wrong.append(str(item))
        state = ("NOT_OBSERVED" if found is None else
                 "REQUIREMENTS_NOT_DECLARED" if not required else
                 "STRUCTURE_MISMATCH" if missing or wrong else
                 "STRUCTURE_VERIFIED")
        rows.append({
            "candidate_id": str(candidate.get("candidate_id", ""))[:80],
            "path": str(candidate.get("path", ""))[:1024],
            "structure_state": state,
            "missing_entries": missing[:24],
            "wrong_type_entries": wrong[:24],
            "source_list_evidence_ref": found["evidence_ref"] if found else "",
            "content_integrity": "NOT_VERIFIED",
            "source_ready": False,
            "write_authorization_granted": False,
        })
    return {
        "schema": SCHEMA,
        "sources": rows,
        "required_top_level_entries": required,
        "source_ready_count": 0,
        "verified_structure_count": sum(row["structure_state"] == "STRUCTURE_VERIFIED" for row in rows),
        "content_integrity_inferred": False,
        "existing_source_verified_for_deployment": False,
        "filesystem_access_performed": False,
    }


__all__ = ["SCHEMA", "reconcile_source_integrity"]
