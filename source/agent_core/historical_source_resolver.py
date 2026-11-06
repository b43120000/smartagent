"""Cold-start recovery of scoped historical source *locations*, never facts.

Index first; on miss, inspect a bounded same-owner TaskContinuation journal
and optionally explicitly source-labeled Project Ledger events. No filesystem
source probing, parent scan, archived Planner prose, ACL reuse or Task replay.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .source_goal_identity import goal_identity, context_identity, normalized_path
from .source_reference_index import read_references, publish_reference
from .source_candidate_ledger import valid_list_receipt, _LABEL
from .target_path_contract import extract_target_contract

SCHEMA = "SMARTAGENT_HISTORICAL_SOURCE_BOOTSTRAP_V1"
HISTORY_SCHEMA = "SMARTAGENT_SOURCE_HISTORY_V1"


def _matches_contract(goal: object, expected: Mapping) -> bool:
    try:
        old_contract = extract_target_contract(str(goal or ""))
    except (ValueError, TypeError):
        return False
    historical = goal_identity(old_contract)
    return (historical.get("status") == "READY"
            and historical["digest"] == expected.get("digest"))


def _listed_required(receipt: Mapping, expected: Mapping) -> bool:
    entries = (receipt.get("data") or {}).get("entries")
    if not isinstance(entries, list) or len(entries) > 2048:
        return False
    names: dict[str, str] = {}
    for row in entries:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            return False
        name = row["name"].casefold()
        if not name or name in names or name in {".", ".."}:
            return False
        names[name] = str(row.get("kind", ""))
    return all(names.get(item) == ("FILE" if "." in item else "DIRECTORY")
               for item in expected["required_entries"])


def bootstrap_historical_sources(
    *, agent_root: str | Path, workspace: str | Path,
    owner: str, instance: str, target_contract: Mapping | None,
    task_store=None, exclude_task_id: str = "",
    project_events: list[dict] | None = None,
) -> dict:
    identity = goal_identity(target_contract)
    empty = {
        "schema": HISTORY_SCHEMA, "bootstrap_schema": SCHEMA,
        "candidates": [], "index_status": "NOT_QUERIED",
        "bootstrap_state": "NO_INDEXABLE_GOAL",
        "recovered_count": 0, "task_records_examined": 0,
        "project_events_examined": 0,
        "historical_leads_are_live_evidence": False,
        "authorization_granted": False,
        "new_source_filesystem_scan": False,
    }
    if identity["status"] != "READY" or not owner or not instance:
        return {**empty, "bootstrap_state": "CONTEXT_OR_GOAL_UNAVAILABLE"}
    root = Path(agent_root)
    work = str(Path(workspace).expanduser().resolve())
    indexed = read_references(
        root, owner=owner, instance=instance,
        workspace=work, goal_digest=identity["digest"])
    if indexed["candidates"]:
        return {
            **empty, "candidates": indexed["candidates"],
            "index_status": indexed["index_status"],
            "bootstrap_state": "INDEX_HIT",
        }

    recovered: dict[str, dict] = {}
    examined = 0
    def add(path: object, origin: str, ref: str, level: str):
        key = normalized_path(path)
        if (not key or key == identity["target"]
                or key.startswith(identity["target"].rstrip("\\") + "\\")
                or not ref):
            return
        if len(recovered) >= 16 and key not in recovered:
            return
        candidate = {
            "path": str(path).strip().rstrip("\\/"),
            "origin": origin, "provenance_ref": str(ref)[:160],
            "evidence_level": level,
            "current_existence": "UNKNOWN",
            "current_integrity": "UNKNOWN",
            "read_authorization": "NOT_EVALUATED",
        }
        if key not in recovered or (
                level == "E2" and recovered[key]["evidence_level"] == "E1"):
            recovered[key] = candidate

    if task_store is not None:
        try:
            prior = task_store.source_history_records(
                owner=owner, workspace=work, exclude_task_id=exclude_task_id,
                limit=48,
            )
        except (OSError, ValueError, RuntimeError, AttributeError):
            prior = []
        for record in prior[:48]:
            if not isinstance(record, Mapping):
                continue
            examined += 1
            if not _matches_contract(record.get("goal"), identity):
                continue
            tid = str(record.get("task_id", ""))[:120]
            if not tid:
                continue
            for match in list(_LABEL.finditer(str(record.get("goal", ""))))[:8]:
                add(match.group("path"), "HISTORICAL_USER_SOURCE_LABEL",
                    tid + "/ORIGINAL_REQUEST", "E1")
            actions = record.get("actions") or {}
            results = record.get("results") or {}
            for aid, entry in list(actions.items())[-48:]:
                if not isinstance(entry, Mapping):
                    continue
                result_row = results.get(aid) or {}
                if not isinstance(result_row, Mapping):
                    continue
                receipt = valid_list_receipt(
                    result_row, entry,
                    str(record.get("request_id") or ""),
                    str(record.get("task_epoch") or ""),
                )
                if receipt is None or not _listed_required(receipt, identity):
                    continue
                path = (entry.get("action") or {}).get("path")
                add(path, "HISTORICAL_RUNTIME_LIST_RECEIPT",
                    tid + "/" + str(aid)[:100], "E2")

    # Optional Project Ledger input is trusted only when the Runtime supplies
    # events with exact contextual and Goal bindings. Generic event descriptions
    # or path mentions are intentionally excluded.
    ctx = context_identity(owner, instance, work)
    count = 0
    if isinstance(project_events, list):
        for event in project_events[:48]:
            count += 1
            if not isinstance(event, Mapping) or event.get("event_type") != "source_reference_verified":
                continue
            details = event.get("details") or {}
            if not isinstance(details, Mapping):
                continue
            if (details.get("context_digest") != ctx
                    or details.get("goal_digest") != identity["digest"]
                    or not str(event.get("event_id", ""))):
                continue
            add(details.get("source_path"), "PROJECT_LEDGER",
                str(event["event_id"]), "E2")

    for candidate in recovered.values():
        publish_reference(
            root, owner=owner, instance=instance, workspace=work,
            goal_digest=identity["digest"], path=candidate["path"],
            provenance_ref=candidate["provenance_ref"],
            origin=candidate["origin"], evidence_level=candidate["evidence_level"],
        )
    leads = sorted(recovered.values(), key=lambda r: (
        0 if r["evidence_level"] == "E2" else 1,
        r["path"].casefold(),
    ))[:16]
    return {
        **empty, "candidates": leads,
        "index_status": indexed["index_status"],
        "bootstrap_state": (
            "RECOVERED_FROM_HISTORY" if leads else
            "RECOVERY_PARTIAL" if task_store is None else
            "NO_MATCHING_STRUCTURED_SOURCE"
        ),
        "recovered_count": len(leads),
        "task_records_examined": examined,
        "project_events_examined": count,
        "source_history_index_untrusted": True,
    }


__all__ = ["SCHEMA", "bootstrap_historical_sources"]
