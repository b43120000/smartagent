"""Goal-condition reconciliation from an existing Project Sync snapshot.

Read-only and deterministic: no scan, dependency probe, write, authorization,
Planner decisions or Action admission. INDEX_ONLY owns the current snapshot.
Environment Index entries are historical hints, never live verification.
"""
from __future__ import annotations

import re
import ntpath
import posixpath
from collections.abc import Mapping
from pathlib import PurePosixPath

from .project_sync import is_ignored_project_path

SCHEMA = "SMARTAGENT_GOAL_ENVIRONMENT_RECONCILIATION_V1"
_HEX = re.compile(r"^[a-fA-F0-9]{64}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ALLOWED = {"file_exists", "file_sha256", "directory_exists", "git_head", "dependency_candidate"}


def _relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("goal_condition_path_invalid")
    path = raw.replace("\\", "/")
    if (path.startswith("/") or ":" in path or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("goal_condition_path_outside_project")
    return PurePosixPath(path).as_posix()


def _windows_root(root: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[/\\]", root) or root.startswith("\\\\"))


def _canonical_target(workspace_root: object, path: str) -> str:
    """Lexical project-relative identity only; no filesystem calls."""
    if not isinstance(workspace_root, str) or not workspace_root:
        return ""
    if _windows_root(workspace_root):
        return ntpath.normcase(ntpath.normpath(
            ntpath.join(workspace_root, path.replace("/", "\\"))))
    if workspace_root.startswith("/") and not workspace_root.startswith("//"):
        return posixpath.normpath(posixpath.join(workspace_root, path))
    return ""


def _canonical_observed(workspace_root: object, path: object) -> str:
    if not isinstance(workspace_root, str) or not isinstance(path, str) or not path:
        return ""
    if _windows_root(workspace_root):
        return ntpath.normcase(ntpath.normpath(path)) if ntpath.isabs(path) else ""
    if workspace_root.startswith("/") and not workspace_root.startswith("//"):
        return posixpath.normpath(path) if posixpath.isabs(path) else ""
    return ""

def _runtime_directory_facts(action_ledger: Mapping | None,
                             action_results: Mapping | None,
                             request_id: str, task_epoch: str) -> list[dict]:
    """Recheck bounded, committed Runtime receipts; ignore model statements."""
    if not isinstance(action_ledger, Mapping) or not isinstance(action_results, Mapping):
        return []
    from .goal_strategy_transition import classify_runtime_evidence
    facts = []
    for aid in list(action_results)[-24:]:
        row = action_results.get(aid)
        original = action_ledger.get(aid)
        if not isinstance(row, Mapping) or not isinstance(original, Mapping):
            continue
        if (row.get("evidence_status", "ACTIVE") != "ACTIVE"
                or not request_id or row.get("request_id") != request_id
                or not task_epoch or row.get("task_epoch") != task_epoch):
            continue
        fact = classify_runtime_evidence(row, original)
        if fact.get("environment_observation_kind") != "directory.exists":
            continue
        facts.append({
            "evidence_ref": str(aid)[:160],
            "path": fact["environment_observed_path"],
            "exists": fact["environment_observed_exists"],
        })
    return facts

def reconcile_goal_conditions(
    snapshot: Mapping, conditions: list,
    *, environment_index: Mapping | None = None,
    action_ledger: Mapping | None = None,
    action_results: Mapping | None = None,
    request_id: str = "", task_epoch: str = "",
) -> dict:
    """Compare explicit predicates with an existing Project Sync snapshot.

    VERIFIED applies only to the specified predicate, not overall Task Goal.
    Dependency candidates remain UNKNOWN pending a trusted live probe.
    """
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 32:
        raise ValueError("goal_conditions_count_invalid")
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != "PROJECT_SNAPSHOT_V1":
        raise ValueError("goal_conditions_snapshot_required")
    snapshot_id = str(snapshot.get("snapshot_id", "") or "")
    if not _HEX.fullmatch(snapshot_id):
        raise ValueError("goal_conditions_snapshot_identity_invalid")
    rows = snapshot.get("files")
    if not isinstance(rows, list):
        raise ValueError("goal_conditions_snapshot_inventory_invalid")
    inventory = {row.get("path"): row for row in rows if isinstance(row, dict)
                 and isinstance(row.get("path"), str)}
    recorded = (environment_index or {}).get("dependencies", {})
    if not isinstance(recorded, Mapping):
        recorded = {}
    live_directories = _runtime_directory_facts(
        action_ledger, action_results, request_id, task_epoch)
    seen = set()
    evaluations = []
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise ValueError("goal_condition_object_required")
        cid, kind = condition.get("id"), condition.get("kind")
        if not isinstance(cid, str) or not _ID.fullmatch(cid) or cid in seen:
            raise ValueError("goal_condition_identity_invalid_or_duplicate")
        if kind not in _ALLOWED:
            raise ValueError("goal_condition_kind_invalid")
        seen.add(cid)
        answer = {"id": cid, "kind": kind, "status": "UNKNOWN",
                  "reason": "NOT_YET_OBSERVED", "snapshot_id": snapshot_id}
        if kind == "directory_exists":
            path = _relative_path(condition.get("path"))
            answer["path"] = path
            if is_ignored_project_path(path):
                answer.update(reason="OUTSIDE_PROJECT_SYNC_INVENTORY")
            else:
                target = _canonical_target(snapshot.get("workspace_root"), path)
                live = next((
                    fact for fact in reversed(live_directories)
                    if target and _canonical_observed(
                        snapshot.get("workspace_root"), fact["path"]) == target
                ), None)
                has_children = any(item.startswith(path + "/") for item in inventory)
                if path in inventory:
                    answer.update(status="UNSATISFIED", reason="PATH_IS_FILE_IN_SNAPSHOT")
                elif live and has_children and live["exists"] is False:
                    answer.update(status="UNKNOWN", reason="SNAPSHOT_AND_LIVE_FACT_CONFLICT")
                elif has_children:
                    answer.update(status="VERIFIED", reason="DIRECTORY_CONTAINS_SNAPSHOT_FILES")
                elif live:
                    answer.update(
                        status="VERIFIED" if live["exists"] else "MISSING",
                        reason="DIRECTORY_PRESENT_BY_RUNTIME" if live["exists"]
                               else "DIRECTORY_ABSENT_BY_RUNTIME",
                        evidence_ref=live["evidence_ref"],
                        observation_source="COMMITTED_ACTION_RECEIPT",
                    )
                else:
                    answer.update(reason="DIRECTORY_REQUIRES_LIVE_OBSERVATION")
        elif kind in {"file_exists", "file_sha256"}:
            path = _relative_path(condition.get("path"))
            answer["path"] = path
            expected = None
            if kind == "file_sha256":
                expected = condition.get("expected_sha256")
                if not isinstance(expected, str) or not _HEX.fullmatch(expected):
                    raise ValueError("goal_condition_expected_sha256_invalid")
                expected = expected.lower()
            if is_ignored_project_path(path):
                answer.update(status="UNKNOWN", reason="OUTSIDE_PROJECT_SYNC_INVENTORY")
            else:
                observed = inventory.get(path)
                if not observed:
                    # Snapshot absence does not grant write authorization.
                    answer.update(status="MISSING", reason="FILE_ABSENT_FROM_SNAPSHOT")
                elif kind == "file_exists":
                    answer.update(status="VERIFIED", reason="FILE_IN_SNAPSHOT")
                else:
                    actual = observed.get("sha256")
                    if not isinstance(actual, str) or not _HEX.fullmatch(actual):
                        answer.update(status="UNKNOWN", reason="FILE_DIGEST_NOT_AVAILABLE")
                    elif actual.lower() == expected:
                        answer.update(status="VERIFIED", reason="SNAPSHOT_SHA256_MATCH")
                    else:
                        answer.update(status="UNSATISFIED", reason="SNAPSHOT_SHA256_MISMATCH")
        elif kind == "git_head":
            expected = condition.get("expected_sha")
            if not isinstance(expected, str) or not re.fullmatch(
                    r"[a-fA-F0-9]{40}", expected):
                raise ValueError("goal_condition_expected_git_sha_invalid")
            actual = str(snapshot.get("git_head", "") or "")
            if not re.fullmatch(r"[a-fA-F0-9]{40}", actual):
                answer.update(status="UNKNOWN", reason="GIT_HEAD_NOT_AVAILABLE")
            else:
                match = actual.lower() == expected.lower()
                answer.update(status="VERIFIED" if match else "UNSATISFIED",
                              reason="GIT_HEAD_MATCH" if match else "GIT_HEAD_MISMATCH")
        else:
            name = condition.get("distribution")
            if not isinstance(name, str) or not _DISTRIBUTION.fullmatch(name):
                raise ValueError("goal_condition_distribution_invalid")
            candidate = recorded.get(name.lower().replace("_", "-"))
            answer.update(status="UNKNOWN",
                          reason="CANDIDATE_REQUIRES_LIVE_PROBE" if isinstance(candidate, Mapping)
                          else "DEPENDENCY_NOT_OBSERVED",
                          candidate_recorded=isinstance(candidate, Mapping))
        evaluations.append(answer)
    counts = {state: sum(1 for row in evaluations if row["status"] == state)
              for state in ("VERIFIED", "MISSING", "UNSATISFIED", "UNKNOWN", "BLOCKED")}
    return {
        "schema": SCHEMA, "snapshot_id": snapshot_id,
        "status": "ASSESSED", "conditions": evaluations, "counts": counts,
        "task_completion_inferred": False, "authorization_granted": False,
        "dependency_live_verified": False, "replan_executed": False,
        "runtime_directory_fact_count": len(live_directories),
        "new_scan_performed": False,
    }
