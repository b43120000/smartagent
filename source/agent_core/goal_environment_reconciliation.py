"""Goal-condition reconciliation from an existing Project Sync snapshot.

Read-only and deterministic: no scan, dependency probe, write, authorization,
Planner decisions or Action admission. INDEX_ONLY owns the current snapshot.
Environment Index entries are historical hints, never live verification.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from .project_sync import is_ignored_project_path

SCHEMA = "SMARTAGENT_GOAL_ENVIRONMENT_RECONCILIATION_V1"
_HEX = re.compile(r"^[a-fA-F0-9]{64}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ALLOWED = {"file_exists", "file_sha256", "git_head", "dependency_candidate"}


def _relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("goal_condition_path_invalid")
    path = raw.replace("\\", "/")
    if (path.startswith("/") or ":" in path or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("goal_condition_path_outside_project")
    return PurePosixPath(path).as_posix()


def reconcile_goal_conditions(
    snapshot: Mapping, conditions: list,
    *, environment_index: Mapping | None = None,
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
        if kind in {"file_exists", "file_sha256"}:
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
        "new_scan_performed": False,
    }
