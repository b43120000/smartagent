"""Canonical shared Blackboard projection and strategy-exhaustion gate.

The Blackboard is one request/epoch-bound read model shared by Planner and
Runtime. Existing owner ledgers remain authoritative; this module never
duplicates their write authority, creates Evidence, or grants Scope.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_SHARED_BLACKBOARD_V1"
CAPABILITY_SCHEMA = "SMARTAGENT_RUNTIME_CAPABILITY_MANIFEST_V1"
CANDIDATE_SCHEMA = "SMARTAGENT_STRATEGY_CANDIDATE_REGISTRY_V1"
TERMINAL_SCHEMA = "SMARTAGENT_STRATEGY_TERMINAL_READINESS_V1"
_STRATEGY_ID = re.compile(r"^STRAT-[A-F0-9]{20}$")
_ROUTE_IDS = frozenset({"NATIVE_FILE_CREATION", "EXISTING_CWD_CREATION"})
_BLOCKING_STATES = frozenset({
    "UNTRIED", "NEEDS_ACTION_REPAIR", "RECONCILIATION_REQUIRED",
    "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED",
})
_USER_INPUT_STATES = frozenset({
    "NEEDS_USER_AUTHORIZATION", "NEEDS_REMOTE_ACCESS_EVIDENCE",
    "NEEDS_SOURCE_INFORMATION",
})


def _digest(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _code(value: object, default: str = "UNKNOWN") -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}", text) else default


def _path_key(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 2048:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _join_path(root: str, child: str) -> str:
    if "\\" in root or re.match(r"^[A-Za-z]:", root):
        return ntpath.join(root, child)
    return os.path.join(root, child)


def _within(path: object, root: object) -> bool:
    child, parent = _path_key(path), _path_key(root)
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent.rstrip("\\") + "\\")


def _type_name(value: object) -> str:
    values = value if isinstance(value, tuple) else (value,)
    return "|".join(getattr(item, "__name__", str(item)) for item in values)


def _strategy_id(request_id: str, task_epoch: str,
                 evidence_digest: str, route_id: str) -> str:
    material = "|".join((request_id, task_epoch, evidence_digest, route_id))
    return "STRAT-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20].upper()


def build_runtime_capability_manifest(runtime) -> dict:
    """Publish the real canonical interface/schema surface, never permissions."""
    from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
    from .tool_capabilities import get_allowed_tools, TOOL_CATEGORIES, describe_tools
    from .action_loop_state import (
        DISCOVERY_TOOLS, PLAN_TOOLS, VERIFY_TOOLS, MUTATION_TOOLS, DELIVERY_TOOLS,
    )

    interface = (
        getattr(runtime, "tool_interface", "")
        or getattr(getattr(runtime, "tools", None), "interface", "")
        or "web_direct"
    )
    try:
        allowed = sorted(get_allowed_tools(str(interface)))
    except (ValueError, TypeError):
        interface = "web_direct"
        allowed = sorted(get_allowed_tools("web_direct"))
    purposes = {
        str(row.get("tool")): str(row.get("purpose") or "")
        for row in describe_tools(allowed)
    }
    tools = []
    for name in allowed:
        schema = TOOL_ENVELOPE_SCHEMAS.get(name) or {}
        if name in MUTATION_TOOLS:
            side_effect = "OPERATION_DEPENDENT" if name == "run_command" else "MUTATION"
        elif name in DELIVERY_TOOLS:
            side_effect = "DELIVERY"
        elif name in VERIFY_TOOLS:
            side_effect = "VERIFICATION"
        elif name in DISCOVERY_TOOLS:
            side_effect = "READ_ONLY_OBSERVATION"
        elif name in PLAN_TOOLS:
            side_effect = "PLAN_ONLY"
        else:
            side_effect = "OTHER"
        tools.append({
            "tool": name,
            "category": str(TOOL_CATEGORIES.get(name, "UNCATEGORIZED")),
            "purpose": purposes.get(name, "Canonical Runtime tool."),
            "required_fields": {
                str(key): _type_name(value)
                for key, value in (schema.get("required") or {}).items()
            },
            "optional_fields": {
                str(key): _type_name(value)
                for key, value in (schema.get("optional") or {}).items()
            },
            "side_effect_class": side_effect,
            "normal_tool_admission_required": True,
            "scope_admission_owner": "RUNTIME",
            "authorization_inferred": False,
        })
    registry_digest = _digest([
        {
            "tool": row["tool"], "category": row["category"],
            "required_fields": row["required_fields"],
            "optional_fields": row["optional_fields"],
            "side_effect_class": row["side_effect_class"],
        }
        for row in tools
    ])
    manifest = {
        "schema": CAPABILITY_SCHEMA,
        "interface": str(interface),
        "manifest_id": "CAP-" + registry_digest[:20].upper(),
        "registry_sha256": registry_digest,
        "allowed_tool_count": len(tools),
        "allowed_tools": [row["tool"] for row in tools],
        "tools": tools,
        "run_command_operations": [
            "INSPECT", "MUTATE", "BUILD", "TEST", "VERIFY",
            "GIT_INSPECT", "GIT_MUTATE", "PROCESS", "TRANSFER", "GENERAL",
        ],
        "run_command_cwd_policy": (
            "EXISTING_DIRECTORY_REQUIRED_AND_RUNTIME_SCOPE_PREFLIGHT"
        ),
        "capability_is_not_authorization": True,
        "scope_grants_created": False,
        "network_authorization_granted": False,
        "execution_started": False,
        "authority": "RUNTIME_CANONICAL_TOOL_REGISTRY",
    }
    return manifest


def _preview_scope(runtime, action: Mapping, expected_path: str) -> tuple[str, str]:
    """Use only Runtime's non-executing scope preview."""
    preview_fn = getattr(getattr(runtime, "tools", None), "preview_tool_scope", None)
    if not callable(preview_fn):
        return "UNAVAILABLE", "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
    try:
        preview = preview_fn(dict(action))
    except (OSError, RuntimeError, ValueError, TypeError):
        return "UNAVAILABLE", "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
    if not isinstance(preview, Mapping):
        return "UNAVAILABLE", "RUNTIME_SCOPE_PREFLIGHT_UNAVAILABLE"
    if preview.get("allowed") is not True:
        diagnostic = preview.get("scope_diagnostic")
        reason = (
            diagnostic.get("reason_code")
            if isinstance(diagnostic, Mapping) else ""
        )
        return "DENIED", _code(reason or preview.get("reason_code"),
                               "RUNTIME_SCOPE_PREFLIGHT_REJECTED")
    resolved = list(preview.get("resolved_paths") or [])
    if not resolved or _path_key(resolved[0]) != _path_key(expected_path):
        return "UNVERIFIED", "RUNTIME_SCOPE_PATH_UNCONFIRMED"
    return "PASS", "RUNTIME_SCOPE_PREFLIGHT_PASS"


def _command_mentions_target(action: Mapping, candidate: Mapping) -> bool:
    command = str(action.get("command") or "").replace("/", "\\").casefold()
    target = str(candidate.get("target") or "")
    if not target:
        return True
    target_key = _path_key(target).replace("/", "\\").casefold()
    if target_key and target_key in command:
        return True
    cwd = str(candidate.get("working_directory") or "")
    if cwd:
        try:
            relative = ntpath.relpath(target, cwd)
        except (OSError, ValueError):
            relative = ""
        relative_key = _path_key(relative).replace("/", "\\").casefold()
        if relative_key and relative_key not in {".", ".."} and relative_key in command:
            return True
    return False


def _candidate_action_matches(candidate: Mapping, action: Mapping) -> bool:
    if action.get("strategy_id") == candidate.get("strategy_id"):
        return True
    if candidate.get("route_id") == "GOAL_DIRECTORY_CREATE":
        return (
            action.get("tool") == "create_directory"
            and _path_key(action.get("path")) == _path_key(candidate.get("target"))
        )
    if str(action.get("tool") or "") != str(candidate.get("tool") or ""):
        return False
    operation = str(candidate.get("operation") or "")
    if operation and str(action.get("operation") or "") != operation:
        return False
    working_directory = str(candidate.get("working_directory") or "")
    if working_directory:
        if _path_key(action.get("working_directory")) != _path_key(working_directory):
            return False
        contract = candidate.get("planner_action_contract")
        expected_command = (
            str(contract.get("command") or "")
            if isinstance(contract, Mapping) else ""
        )
        if expected_command:
            return str(action.get("command") or "") == expected_command
        if operation == "GIT_MUTATE":
            if candidate.get("route_id") == "PINNED_REMOTE_ARTIFACT_ACQUISITION":
                source = candidate.get("source_artifact") or {}
                repository = str(source.get("repository") or "").lower()
                return bool(
                    repository
                    and ("github.com/" + repository) in
                        str(action.get("command") or "").lower()
                )
            return bool(
                re.search(r"(?i)\bgit(?:\.exe)?\b", str(action.get("command") or ""))
                and _command_mentions_target(action, candidate)
            )
        return True
    path_root = str(candidate.get("target_path_root") or "")
    if path_root:
        return _within(action.get("path"), path_root)
    return False


def _attempt_state(candidate: Mapping, result: Mapping | None,
                   reason_codes: list[str] | None = None) -> tuple[str, str]:
    reasons = [str(item).lower() for item in (reason_codes or [])]
    if isinstance(result, Mapping):
        reasons.extend([
            str(result.get("result_reason") or "").lower(),
            str(result.get("reason_code") or "").lower(),
        ])
        status = str(result.get("result_status") or "UNKNOWN").upper()
        execution = str(result.get("execution_status") or "UNKNOWN").upper()
        executed = result.get("executed")
        verification = str(
            result.get("effective_verification_status")
            or result.get("verification_status") or "UNKNOWN"
        ).upper()
        if status in {"UNKNOWN", "PARTIAL", "QUEUED", "RUNNING"} or execution in {
            "UNKNOWN", "PARTIAL", "QUEUED", "RUNNING", "OPEN",
        }:
            return "RECONCILIATION_REQUIRED", _code(result.get("result_reason"))
        if status == "SUCCEEDED" and verification == "PASS":
            return "SATISFIED", "VERIFIED_SUCCESS"
        if executed is False and status in {"FAILED", "REJECTED"}:
            if any(any(token in item for token in (
                "scope", "authorization", "permission", "approval",
                "working_directory_rejected",
            )) for item in reasons):
                return "NEEDS_USER_AUTHORIZATION", _code(result.get("result_reason"))
            if any("strategy_candidate_not_selectable" in item for item in reasons):
                return "ATTEMPTED_REJECTED", "STRATEGY_ALREADY_ATTEMPTED"
            if any(any(token in item for token in (
                "schema", "field", "verification_contract", "unexpected_field",
                "wrong_field", "missing_required", "tool_mismatch",
                "operation_mismatch", "working_directory_mismatch",
                "target_outside_root", "target_mismatch", "command_mismatch",
                "strategy_candidate_id_required", "strategy_candidate_reference_unknown",
                "strategy_candidate_reference_invalid",
            )) for item in reasons):
                return "NEEDS_ACTION_REPAIR", _code(result.get("result_reason"))
            return "ATTEMPTED_REJECTED", _code(result.get("result_reason"))
        if status in {"FAILED", "REJECTED"} and executed is True:
            return "ATTEMPTED_FAILED", _code(result.get("result_reason"))
        return "RECONCILIATION_REQUIRED", _code(result.get("result_reason"))
    if any(any(token in item for token in (
        "scope", "authorization", "permission", "approval",
        "working_directory_rejected",
    )) for item in reasons):
        return "NEEDS_USER_AUTHORIZATION", "ADMISSION_SCOPE_BLOCKED"
    if any(any(token in item for token in (
        "schema", "field", "verification_contract", "unexpected_field",
        "wrong_field", "missing_required",
    )) for item in reasons):
        return "NEEDS_ACTION_REPAIR", "ADMISSION_SCHEMA_REPAIR_REQUIRED"
    return "ATTEMPTED_REJECTED", "PREEXECUTION_REJECTION"


def _find_attempts(runtime, candidate: Mapping) -> list[dict]:
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    results = dict(getattr(runtime, "action_result_ledger", {}) or {})
    request_id = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    attempts = []
    for action_id, record in actions.items():
        if not isinstance(record, Mapping):
            continue
        if ((record.get("request_id") and record.get("request_id") != request_id)
                or (record.get("task_epoch") and record.get("task_epoch") != epoch)):
            continue
        action = record.get("action") if isinstance(record.get("action"), Mapping) else record
        if not _candidate_action_matches(candidate, action):
            continue
        result = results.get(action_id)
        if isinstance(result, Mapping) and (
            result.get("request_id") != request_id
            or result.get("task_epoch") != epoch
        ):
            result = None
        status, reason = _attempt_state(candidate, result)
        attempts.append({
            "round": int(record.get("round", 0) or 0),
            "action_id": str(action_id)[:160],
            "status": status,
            "reason_code": reason,
        })
    for trace in list(getattr(runtime, "_rejected_action_resolution_traces", []) or [])[-64:]:
        if not isinstance(trace, Mapping):
            continue
        if trace.get("request_id") != request_id or trace.get("task_epoch") != epoch:
            continue
        action = trace.get("planner_requested") or {}
        if not isinstance(action, Mapping) or not _candidate_action_matches(candidate, action):
            continue
        resolved = trace.get("runtime_resolved") or {}
        reasons = list(resolved.get("reason_codes") or []) if isinstance(resolved, Mapping) else []
        status, reason = _attempt_state(candidate, None, reasons)
        attempts.append({
            "round": int(trace.get("round", 0) or 0),
            "action_id": str(trace.get("action_id", "") or "")[:160],
            "status": status,
            "reason_code": reason,
        })
    return sorted(attempts, key=lambda row: (row["round"], row["action_id"]))


def build_strategy_candidate_registry(
    runtime, *, capability_manifest: Mapping | None = None,
    prerequisite_view: Mapping | None = None,
    prerequisite_transition: Mapping | None = None,
    remote_access_view: Mapping | None = None,
    artifact_view: Mapping | None = None,
    goal_gap_view: Mapping | None = None,
    directory_decision: Mapping | None = None,
) -> dict:
    """Project goal-relevant alternatives from Runtime-attested strategy receipts."""
    from .goal_prerequisite_resolution import source_requirement

    progress = getattr(runtime, "progress_ledger", None)
    goal = str(getattr(progress, "goal", "") or getattr(runtime, "task_request", "") or "")
    need = source_requirement(goal)
    request_id = str(getattr(runtime, "run_id", "") or "")
    task_id = str(getattr(runtime, "task_id", "") or "")
    task_epoch = str(getattr(runtime, "task_epoch", "") or "")
    strategy_state = dict(getattr(progress, "strategy_state", {}) or {})
    history = list(strategy_state.get("history") or [])[-8:]
    manifest = capability_manifest or build_runtime_capability_manifest(runtime)
    allowed_tools = set(manifest.get("allowed_tools") or [])
    artifacts = artifact_view if isinstance(artifact_view, Mapping) else (
        getattr(runtime, "_source_discovery_bootstrap", None) or {}
    )
    artifact_refs = [
        row for row in list(artifacts.get("artifact_references") or [])[:16]
        if isinstance(row, Mapping)
        and row.get("kind") == "REMOTE_ARTIFACT_REFERENCE"
    ]
    remote = remote_access_view if isinstance(remote_access_view, Mapping) else {}
    remote_state = str(remote.get("authorization_state") or "UNKNOWN").upper()
    git_status = str(remote.get("git_executable_status") or "UNKNOWN").upper()
    prerequisite = prerequisite_view if isinstance(prerequisite_view, Mapping) else {}
    transition = prerequisite_transition if isinstance(prerequisite_transition, Mapping) else {}
    gaps = goal_gap_view if isinstance(goal_gap_view, Mapping) else {}
    def confirmed_target_absence(target_path: str) -> str:
        for gap in list(gaps.get("active_gaps") or [])[:32]:
            if (isinstance(gap, Mapping)
                    and _path_key(gap.get("path")) == _path_key(target_path)
                    and gap.get("status") == "UNRESOLVED"
                    and gap.get("observation_status") == "CONFIRMED"
                    and gap.get("observed_value") is False
                    and gap.get("observed_fact_ref")):
                return str(gap.get("observed_fact_ref"))[:160]
        return ""
    candidates = []
    decision = directory_decision if isinstance(directory_decision, Mapping) else {}
    directory_candidate = decision.get("candidate")
    if isinstance(directory_candidate, Mapping) and directory_candidate.get("route_id") == "GOAL_DIRECTORY_CREATE":
        candidate = dict(directory_candidate)
        if candidate.get("tool") in allowed_tools:
            attempts = _find_attempts(runtime, candidate)
            if attempts:
                latest = attempts[-1]
                candidate.update({
                    "status": latest["status"],
                    "attempt_count": len(attempts),
                    "attempted_action_id": latest["action_id"],
                    "attempted_reason_code": latest["reason_code"],
                })
                candidate["blocks_interrupt"] = candidate["status"] in _BLOCKING_STATES
                candidate["requires_user_input"] = candidate["status"] in _USER_INPUT_STATES
            candidates.append(candidate)

    for revision_row in history:
        if not isinstance(revision_row, Mapping):
            continue
        evidence_digest = str(revision_row.get("evidence_digest") or "")
        target = str(revision_row.get("target") or "")
        parent = str(revision_row.get("existing_writable_parent") or "")
        if len(evidence_digest) != 64 or not target:
            continue
        target_absence_ref = confirmed_target_absence(target)
        for route in list(revision_row.get("routes") or [])[:4]:
            if not isinstance(route, Mapping):
                continue
            route_id = str(route.get("route_id") or route.get("id") or "")
            if route_id not in _ROUTE_IDS:
                continue
            tool = ""
            operation = ""
            working_directory = ""
            target_root = ""
            purpose = ""
            scope_status = "NOT_CHECKED"
            scope_reason = "RUNTIME_SCOPE_PREFLIGHT_NOT_RUN"
            status = "BLOCKED_PREREQUISITE"
            required_fields = []
            goal_relevance = ""

            if route_id == "EXISTING_CWD_CREATION":
                if need == "REQUIRED":
                    tool = "run_command"
                    operation = "GIT_MUTATE"
                    goal_relevance = "REMOTE_SOURCE_ACQUISITION"
                    purpose = (
                        "Acquire the registered remote artifact from the exact "
                        "existing writable parent; keep the absent deployment "
                        "destination as the clone/download target, not as CWD."
                    )
                    required_fields = [
                        "command", "operation", "working_directory",
                        "success_criteria", "verify",
                    ]
                elif need == "NOT_REQUIRED":
                    tool = "run_command"
                    operation = "MUTATE"
                    goal_relevance = "TARGET_DIRECTORY_CREATION"
                    purpose = (
                        "Create only the exact missing goal directory from an "
                        "existing parent; do not alter existing files."
                    )
                    required_fields = [
                        "command", "operation", "working_directory",
                        "success_criteria", "verify",
                    ]
                else:
                    continue
                working_directory = parent
                if tool not in allowed_tools:
                    status = "CAPABILITY_UNAVAILABLE"
                elif not parent:
                    status = "BLOCKED_PREREQUISITE"
                else:
                    scope_status, scope_reason = _preview_scope(
                        runtime,
                        {
                            "tool": "run_command", "operation": "INSPECT",
                            "command": "Get-Location",
                            "working_directory": parent,
                        },
                        parent,
                    )
                    if need == "REQUIRED":
                        if not artifact_refs:
                            status = "NEEDS_SOURCE_INFORMATION"
                        elif git_status != "AVAILABLE":
                            status = "CAPABILITY_UNAVAILABLE"
                        elif remote_state in {"DENIED", "NEEDS_USER_APPROVAL"}:
                            status = "NEEDS_USER_AUTHORIZATION"
                        elif remote_state != "AUTHORIZED":
                            status = "NEEDS_REMOTE_ACCESS_EVIDENCE"
                        elif scope_status == "PASS":
                            status = "UNTRIED"
                        elif scope_status == "DENIED":
                            status = "NEEDS_USER_AUTHORIZATION"
                        else:
                            status = "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"
                    elif scope_status == "PASS":
                        status = "UNTRIED"
                    elif scope_status == "DENIED":
                        status = "NEEDS_USER_AUTHORIZATION"
                    else:
                        status = "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"

            elif route_id == "NATIVE_FILE_CREATION":
                if need == "REQUIRED":
                    # A local write cannot replace acquisition of a required
                    # external source whose contents have not been obtained.
                    continue
                if need != "NOT_REQUIRED":
                    continue
                tool = "write_file"
                goal_relevance = "EXACT_GOAL_FILE_CREATION"
                purpose = (
                    "Create an exact user-requested file under the attested "
                    "target root; Runtime still checks the final path and content."
                )
                target_root = target
                required_fields = ["path", "content"]
                probe_path = _join_path(target, "__smartagent_strategy_preflight__.tmp")
                if tool not in allowed_tools:
                    status = "CAPABILITY_UNAVAILABLE"
                else:
                    scope_status, scope_reason = _preview_scope(
                        runtime,
                        {"tool": "write_file", "path": probe_path, "content": ""},
                        probe_path,
                    )
                    if scope_status == "PASS":
                        status = "UNTRIED"
                    elif scope_status == "DENIED":
                        status = "NEEDS_USER_AUTHORIZATION"
                    else:
                        status = "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"

            if status == "UNTRIED" and not target_absence_ref:
                status = "NEEDS_TARGET_OBSERVATION"
            if not tool:
                continue
            strategy_id = _strategy_id(
                request_id, task_epoch, evidence_digest, route_id,
            )
            candidate = {
                "strategy_id": strategy_id,
                "revision": int(revision_row.get("revision", 0) or 0),
                "route_id": route_id,
                "goal_relevance": goal_relevance,
                "tool": tool,
                "operation": operation,
                "working_directory": working_directory,
                "target": target,
                "target_absence_evidence_ref": target_absence_ref,
                "target_path_root": target_root,
                "purpose": purpose,
                "required_action_fields": required_fields,
                "source_artifact_candidate_ids": [
                    str(row.get("candidate_id") or "")[:80]
                    for row in artifact_refs[:8]
                    if row.get("candidate_id")
                ],
                "remote_access_state": remote_state,
                "git_capability_status": git_status,
                "working_directory_scope_preflight": scope_status,
                "working_directory_scope_reason_code": scope_reason,
                "source_integrity_verified": False,
                "target_write_admission_required": True,
                "normal_tool_admission_required": True,
                "planner_must_construct_action": True,
                "authorization_granted": False,
                "execution_authorized": False,
                "executed": False,
                "evidence_digest": evidence_digest,
                "rejected_action_id": str(
                    revision_row.get("rejected_action_id") or ""
                )[:160],
                "rule": str(route.get("rule") or "")[:500],
                "status": status,
            }
            attempts = _find_attempts(runtime, candidate)
            if attempts:
                latest = attempts[-1]
                candidate.update({
                    "status": latest["status"],
                    "attempt_count": len(attempts),
                    "attempted_action_id": latest["action_id"],
                    "attempted_reason_code": latest["reason_code"],
                })
            else:
                candidate["attempt_count"] = 0
                candidate["attempted_action_id"] = ""
                candidate["attempted_reason_code"] = ""
            candidate["blocks_interrupt"] = candidate["status"] in _BLOCKING_STATES
            candidate["requires_user_input"] = candidate["status"] in _USER_INPUT_STATES
            candidate["candidate_is_executable_action"] = False
            candidates.append(candidate)

    # The first successful registered Git HEAD probe is not a CWD failure.
    # Therefore it MUST produce the next source-acquisition planning candidate
    # directly from current Goal Gap + Runtime Evidence. Replan history may
    # legitimately be empty. Without this transition the terminal gate would
    # see zero candidates and permit an unsupported INTERRUPT.
    if (
        need == "REQUIRED"
        and prerequisite.get("next_category") == "RESOLVE_REGISTERED_SOURCE_ARTIFACT"
        and transition.get("stage") == "ACCESS_VERIFIED"
        and remote_state == "AUTHORIZED"
        and git_status == "AVAILABLE"
        and len(artifact_refs) == 1
        and "run_command" in allowed_tools
    ):
        from .action_resolution_trace import registered_remote_git_read
        source = artifact_refs[0]
        repository = str(source.get("repository") or "")
        commit_sha = str(source.get("commit") or "")
        archive_path = str(source.get("artifact_path") or "")
        expected_sha256 = str(source.get("expected_sha256") or "")
        expected_blob_sha = str(source.get("expected_git_blob_sha") or "")
        known_targets = [
            gap for gap in list(gaps.get("active_gaps") or [])[:32]
            if isinstance(gap, Mapping)
            and gap.get("observation_status") == "CONFIRMED"
            and gap.get("status") == "UNRESOLVED"
            and gap.get("observed_value") is False
            and gap.get("observed_fact_ref")
            and gap.get("path")
        ]
        # Never select an arbitrary source, target or missing workspace.
        # A single pinned source and single observed absent Goal target are
        # required for this bounded candidate. This is NOT a write grant.
        if (
            len(known_targets) == 1
            and repository
            and re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha)
            and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
            and re.fullmatch(r"[0-9a-fA-F]{40}", expected_blob_sha)
            and archive_path
            and not ntpath.isabs(archive_path)
            and ":" not in archive_path.split("/")[0]
            and not archive_path.startswith(("/", chr(92)))
            and ".." not in archive_path.replace(chr(92), "/").split("/")
        ):
            actions = dict(getattr(runtime, "action_ledger", {}) or {})
            results = dict(getattr(runtime, "action_result_ledger", {}) or {})
            for action_id in reversed(list(results)):
                evidence = results[action_id]
                recorded = actions.get(action_id) or {}
                action = (
                    recorded.get("action")
                    if isinstance(recorded, Mapping) else None
                )
                if not (
                    isinstance(evidence, Mapping)
                    and evidence.get("request_id") == request_id
                    and evidence.get("task_epoch") == task_epoch
                    and evidence.get("executed") is True
                    and evidence.get("result_status") == "SUCCEEDED"
                    and evidence.get("evidence_status", "ACTIVE") == "ACTIVE"
                    and isinstance(action, Mapping)
                    and registered_remote_git_read(action, artifacts)
                ):
                    continue
                cwd = str(action.get("working_directory") or "")
                if not cwd:
                    # Only an actual bound workspace may be used as a CWD
                    # candidate when the admitted read omitted the field.
                    cwd = str(getattr(runtime, "workspace", "") or "")
                if not cwd:
                    break
                scope_status, scope_reason = _preview_scope(
                    runtime, {
                        "tool": "run_command", "operation": "GIT_INSPECT",
                        "command": str(action.get("command") or ""),
                        "working_directory": cwd,
                    }, cwd,
                )
                status = (
                    "UNTRIED" if scope_status == "PASS" else
                    "NEEDS_USER_AUTHORIZATION" if scope_status == "DENIED" else
                    "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"
                )
                target = str(known_targets[0]["path"])
                evidence_ref = str(known_targets[0]["observed_fact_ref"])[:160]
                seed = _digest({
                    "source_candidate_id": str(source.get("candidate_id") or ""),
                    "repository": repository, "commit_sha": commit_sha,
                    "archive_path": archive_path,
                    "expected_sha256": expected_sha256,
                    "expected_blob_sha": expected_blob_sha,
                    "target": _path_key(target),
                    "git_read_action_id": str(action_id),
                    "target_absence_ref": evidence_ref,
                })
                candidate = {
                    "strategy_id": _strategy_id(
                        request_id, task_epoch, seed,
                        "PINNED_REMOTE_ARTIFACT_ACQUISITION",
                    ),
                    "revision": int(strategy_state.get("revision", 1) or 1),
                    "route_id": "PINNED_REMOTE_ARTIFACT_ACQUISITION",
                    "goal_relevance": "ACQUIRE_AND_VERIFY_PINNED_SOURCE_BEFORE_DEPLOYMENT",
                    "tool": "run_command",
                    "operation": "GIT_MUTATE",
                    "working_directory": cwd,
                    "target": target,
                    "target_path_root": "",
                    "target_absence_evidence_ref": evidence_ref,
                    "source_artifact": {
                        "candidate_id": str(source.get("candidate_id") or "")[:80],
                        "repository": repository,
                        "commit": commit_sha,
                        "artifact_path": archive_path,
                        "expected_sha256": expected_sha256,
                        "expected_git_blob_sha": expected_blob_sha,
                        "archive_root": str(source.get("archive_root") or ""),
                        "expected_hashes_are_catalog_claims": True,
                    },
                    "source_git_read_evidence_ref": str(action_id)[:160],
                    "purpose": (
                        "Acquire the exact registered pinned remote archive "
                        "under a separately admitted write destination and "
                        "independently verify the commit, archive hash and "
                        "contents before changing the missing Goal target."
                    ),
                    "required_action_fields": [
                        "command", "operation", "working_directory",
                        "success_criteria", "verify",
                    ],
                    "working_directory_scope_preflight": scope_status,
                    "working_directory_scope_reason_code": scope_reason,
                    "scope_preview_is_read_only_git_probe": True,
                    "mutation_scope_not_yet_verified": True,
                    "source_integrity_verified": False,
                    "target_write_admission_required": True,
                    "normal_tool_admission_required": True,
                    "planner_must_construct_action": True,
                    "authorization_granted": False,
                    "execution_authorized": False,
                    "executed": False,
                    "status": status,
                    "evidence_digest": seed,
                }
                attempts = _find_attempts(runtime, candidate)
                if attempts:
                    latest = attempts[-1]
                    candidate.update({
                        "status": latest["status"],
                        "attempt_count": len(attempts),
                        "attempted_action_id": latest["action_id"],
                        "attempted_reason_code": latest["reason_code"],
                    })
                else:
                    candidate["attempt_count"] = 0
                    candidate["attempted_action_id"] = ""
                    candidate["attempted_reason_code"] = ""
                candidate["blocks_interrupt"] = candidate["status"] in _BLOCKING_STATES
                candidate["requires_user_input"] = candidate["status"] in _USER_INPUT_STATES
                candidate["candidate_is_executable_action"] = False
                candidates.append(candidate)
                break

    # Include the already-supported exact Git readiness probe in the same
    # candidate vocabulary; the prerequisite transition gate remains owner.
    probe = dict(transition.get("probe_action") or {})
    if transition.get("stage") == "READY_FOR_TOOL_ADMISSION" and probe:
        repository = str(transition.get("repository") or "")
        candidate_seed = str(transition.get("candidate_id") or repository)
        digest = _digest({
            "request_id": request_id, "task_epoch": task_epoch,
            "candidate_seed": candidate_seed, "route": "REGISTERED_GIT_HEAD_READ",
        })
        strategy_id = "STRAT-" + digest[:20].upper()
        attempts = list(transition.get("actual_probe_attempts") or [])
        probe_status = "ATTEMPTED_REJECTED" if attempts else "UNTRIED"
        candidates.append({
            "strategy_id": strategy_id,
            "revision": int(strategy_state.get("revision", 1) or 1),
            "route_id": "REGISTERED_GIT_HEAD_READ",
            "goal_relevance": "REMOTE_SOURCE_READINESS",
            "tool": str(probe.get("tool") or "run_command"),
            "operation": str(probe.get("operation") or "GIT_INSPECT"),
            "working_directory": str(probe.get("working_directory") or ""),
            "target": "",
            "target_path_root": "",
            "purpose": "Perform the exact registered Git HEAD read already scoped by Runtime.",
            "required_action_fields": ["command", "operation", "working_directory"],
            "planner_action_contract": dict(probe),
            "working_directory_scope_preflight": "PASS",
            "remote_access_state": remote_state,
            "source_integrity_verified": False,
            "normal_tool_admission_required": True,
            "authorization_granted": False,
            "execution_authorized": False,
            "executed": False,
            "attempt_count": len(attempts),
            "status": probe_status,
            "blocks_interrupt": probe_status == "UNTRIED",
            "requires_user_input": False,
            "candidate_is_executable_action": False,
        })

    # Stable de-duplication protects against multiple revision records that
    # carry the same receipt/route pair.
    unique = {}
    for item in candidates:
        semantic_key = _digest({
            "route_id": item.get("route_id"),
            "tool": item.get("tool"),
            "operation": item.get("operation"),
            "working_directory": _path_key(item.get("working_directory")),
            "target": _path_key(item.get("target")),
            "target_path_root": _path_key(item.get("target_path_root")),
        })
        previous = unique.get(semantic_key)
        if previous is None or int(item.get("revision", 0) or 0) >= int(
            previous.get("revision", 0) or 0
        ):
            unique[semantic_key] = item
    candidates = list(unique.values())[-16:]
    counts = {}
    for item in candidates:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema": CANDIDATE_SCHEMA,
        "request_id": request_id,
        "task_id": task_id,
        "task_epoch": task_epoch,
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        "strategy_revision": int(strategy_state.get("revision", 1) or 1),
        "source_requirement": need,
        "candidate_count": len(candidates),
        "status_counts": counts,
        "untried_candidate_count": counts.get("UNTRIED", 0),
        "blocking_candidate_count": sum(
            1 for item in candidates if item.get("blocks_interrupt")
        ),
        "candidates": candidates,
        "strategy_authority": "PLANNER",
        "candidate_derivation_authority": "RUNTIME_EVIDENCE_AND_CAPABILITY",
        "admission_authority": "RUNTIME",
        "candidate_is_permission": False,
        "authorization_granted": False,
        "action_dispatched": False,
        "execution_started": False,
    }


def evaluate_terminal_readiness(
    registry: Mapping | None, *, request_id: str = "", task_epoch: str = "",
) -> dict:
    """Allow INTERRUPT only after runnable candidates and uncertain effects clear."""
    view = registry if isinstance(registry, Mapping) else {}
    if not view:
        return {
            "schema": TERMINAL_SCHEMA, "decision": "ALLOW_INTERRUPT",
            "reason_code": "NO_SHARED_STRATEGY_REGISTRY",
            "interrupt_allowed": True, "strategy_space_state": "NO_GROUNDED_CANDIDATES",
            "candidate_count": 0, "untried_candidate_count": 0,
            "blocking_candidate_ids": [], "authorization_granted": False,
        }
    if view.get("schema") != CANDIDATE_SCHEMA:
        return {
            "schema": TERMINAL_SCHEMA, "decision": "HOLD_FOR_RECONCILIATION",
            "reason_code": "STRATEGY_REGISTRY_SCHEMA_INVALID",
            "interrupt_allowed": False, "strategy_space_state": "UNKNOWN",
            "candidate_count": 0, "untried_candidate_count": 0,
            "blocking_candidate_ids": [], "authorization_granted": False,
        }
    if ((request_id and view.get("request_id") != request_id)
            or (task_epoch and view.get("task_epoch") != task_epoch)):
        return {
            "schema": TERMINAL_SCHEMA, "decision": "HOLD_FOR_RECONCILIATION",
            "reason_code": "STRATEGY_REGISTRY_IDENTITY_MISMATCH",
            "interrupt_allowed": False, "strategy_space_state": "STALE_SNAPSHOT",
            "candidate_count": int(view.get("candidate_count", 0) or 0),
            "untried_candidate_count": 0,
            "blocking_candidate_ids": [], "authorization_granted": False,
        }
    candidates = [
        row for row in list(view.get("candidates") or [])[:16]
        if isinstance(row, Mapping)
    ]
    blocking = [row for row in candidates if row.get("blocks_interrupt") is True]
    states = {str(row.get("status") or "UNKNOWN") for row in blocking}
    if "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED" in states:
        decision, reason, state = (
            "HOLD_FOR_RECONCILIATION",
            "STRATEGY_SCOPE_PREFLIGHT_UNRESOLVED",
            "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED",
        )
    elif "RECONCILIATION_REQUIRED" in states:
        decision, reason, state = (
            "HOLD_FOR_RECONCILIATION",
            "UNRESOLVED_ACTION_OUTCOME_PREVENTS_TERMINAL_INTERRUPT",
            "RECONCILIATION_REQUIRED",
        )
    elif "NEEDS_ACTION_REPAIR" in states:
        decision, reason, state = (
            "REPAIR_REQUIRED",
            "UNTRIED_STRATEGY_ACTION_SCHEMA_REPAIR_REMAINS",
            "ACTION_REPAIR_REQUIRED",
        )
    elif "UNTRIED" in states:
        decision, reason, state = (
            "REPLAN_REQUIRED",
            "GOAL_RELEVANT_STRATEGY_CANDIDATE_NOT_ATTEMPTED",
            "UNTRIED_CANDIDATES_AVAILABLE",
        )
    else:
        user_input = any(
            str(row.get("status") or "") in _USER_INPUT_STATES
            for row in candidates
        )
        attempted = any(
            str(row.get("status") or "").startswith("ATTEMPTED")
            or str(row.get("status") or "") == "SATISFIED"
            for row in candidates
        )
        decision, reason, state = (
            "ALLOW_INTERRUPT",
            "REMAINING_STRATEGIES_REQUIRE_USER_INPUT" if user_input else
            "GROUNDED_STRATEGIES_EXHAUSTED" if attempted else
            "NO_GROUNDED_STRATEGY_CANDIDATES",
            "USER_INPUT_REQUIRED" if user_input else
            "EXHAUSTED" if attempted else "NO_GROUNDED_CANDIDATES",
        )
    return {
        "schema": TERMINAL_SCHEMA,
        "request_id": str(view.get("request_id") or ""),
        "task_epoch": str(view.get("task_epoch") or ""),
        "decision": decision,
        "reason_code": reason,
        "interrupt_allowed": decision == "ALLOW_INTERRUPT",
        "strategy_space_state": state,
        "candidate_count": len(candidates),
        "untried_candidate_count": sum(
            1 for row in candidates if row.get("status") == "UNTRIED"
        ),
        "blocking_candidate_ids": [
            str(row.get("strategy_id") or "") for row in blocking[:8]
        ],
        "remaining_user_input_candidate_ids": [
            str(row.get("strategy_id") or "")
            for row in candidates if row.get("status") in _USER_INPUT_STATES
        ][:8],
        "authorization_granted": False,
        "tool_executed": False,
        "terminal_success_inferred": False,
        "authority": "RUNTIME_TERMINAL_CONSTRAINT",
    }


def interrupt_terminal_diagnostic(readiness: Mapping | None) -> dict | None:
    view = readiness if isinstance(readiness, Mapping) else {}
    if view.get("interrupt_allowed") is True:
        return None
    decision = _code(view.get("decision"), "HOLD_FOR_RECONCILIATION")
    reason = _code(view.get("reason_code"), "STRATEGY_TERMINAL_READINESS_UNKNOWN")
    return {
        "marker": "[SMARTAGENT_STRATEGY_EXHAUSTION_GATE]",
        "reason": "goal_relevant_strategy_not_exhausted",
        "decision": decision,
        "reason_code": reason,
        "detail": json.dumps({
            "strategy_space_state": view.get("strategy_space_state", "UNKNOWN"),
            "candidate_count": view.get("candidate_count", 0),
            "untried_candidate_count": view.get("untried_candidate_count", 0),
            "blocking_candidate_ids": list(view.get("blocking_candidate_ids") or [])[:8],
        }, ensure_ascii=False, separators=(",", ":")),
        "suggestion": (
            "原始 Goal 與既有授權邊界保持不變。若狀態為 UNTRIED_CANDIDATES_AVAILABLE，"
            "請使用 CONTINUE，選擇 Runtime strategy_candidates 中對應的 strategy_id，"
            "提出新的 canonical Action；若為 ACTION_REPAIR_REQUIRED，修正該候選 Action；"
            "若為 RECONCILIATION_REQUIRED，先查明既有 Action 的實際結果。"
            "候選策略不是授權或可執行命令；每個新 Action 仍須通過完整 Tool/Scope/"
            "Network/Approval Admission 與獨立 Evidence Verification。"
        ),
        "authorization_granted": False,
        "tool_executed": False,
    }


def validate_strategy_candidate_bindings(
    actions: list[dict], registry: Mapping | None, *,
    request_id: str = "", task_epoch: str = "",
) -> dict | None:
    """Validate explicit Planner strategy IDs as correlation, not permission."""
    operational = [
        row for row in list(actions or [])
        if isinstance(row, Mapping)
        and row.get("tool") not in {"report_progress", "final_response", "turn_commit"}
    ]
    selected = [row for row in operational if row.get("strategy_id")]
    if not operational:
        return None
    view = registry if isinstance(registry, Mapping) else {}
    if view.get("schema") != CANDIDATE_SCHEMA:
        if selected:
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_registry_missing_or_stale",
                "suggestion": "只使用本 Request/Task Epoch 最新 Planner Context 中由 Runtime 提供的 strategy_id。",
                "authorization_granted": False, "executed": False,
            }
        return None
    if ((request_id and view.get("request_id") != request_id)
            or (task_epoch and view.get("task_epoch") != task_epoch)):
        return {
            "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
            "reason": "strategy_candidate_registry_missing_or_stale",
            "suggestion": "只使用本 Request/Task Epoch 最新 Planner Context 中由 Runtime 提供的 strategy_id。",
            "authorization_granted": False, "executed": False,
        }
    by_id = {
        str(row.get("strategy_id")): row
        for row in list(view.get("candidates") or [])
        if isinstance(row, Mapping)
    }
    for action in operational:
        if not action.get("strategy_id"):
            matching = [
                row for row in by_id.values()
                if row.get("status") in {"UNTRIED", "NEEDS_ACTION_REPAIR"}
                and _candidate_action_matches(row, action)
            ]
            if matching:
                return {
                    "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                    "reason": "strategy_candidate_id_required",
                    "detail": json.dumps({
                        "candidate_ids": [
                            str(row.get("strategy_id") or "") for row in matching[:4]
                        ],
                    }, separators=(",", ":")),
                    "suggestion": "若此 Action 採用 Runtime strategy candidate，必須帶上該候選精確的 strategy_id；此欄位只做因果關聯，不授權執行。",
                    "authorization_granted": False, "executed": False,
                }
            continue
        strategy_id = str(action.get("strategy_id") or "")
        candidate = by_id.get(strategy_id)
        if not _STRATEGY_ID.fullmatch(strategy_id) or candidate is None:
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_reference_unknown",
                "detail": "strategy_id is not present in the current Runtime candidate registry",
                "suggestion": "從本輪 shared_blackboard.strategy.candidates 選取精確 strategy_id；不得自行產生 ID。",
                "authorization_granted": False, "executed": False,
            }
        if candidate.get("status") not in {"UNTRIED", "NEEDS_ACTION_REPAIR"}:
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_not_selectable",
                "detail": json.dumps({
                    "strategy_id": strategy_id,
                    "status": candidate.get("status"),
                }, separators=(",", ":")),
                "suggestion": "已嘗試、已阻擋或需要使用者輸入的候選不可重複當成新策略；先依目前 Evidence 重新評估。",
                "authorization_granted": False, "executed": False,
            }
        if str(action.get("tool") or "") != str(candidate.get("tool") or ""):
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_tool_mismatch",
                "detail": "strategy_id must be bound to the Runtime-declared candidate tool",
                "suggestion": "保留候選 strategy_id，並使用其宣告的 canonical tool。",
                "authorization_granted": False, "executed": False,
            }
        operation = str(candidate.get("operation") or "")
        if operation and str(action.get("operation") or "") != operation:
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_operation_mismatch",
                "detail": "strategy_id must preserve the candidate operation class",
                "suggestion": "使用候選宣告的 operation；不得藉由更換 operation 規避 Admission。",
                "authorization_granted": False, "executed": False,
            }
        cwd = str(candidate.get("working_directory") or "")
        if cwd and _path_key(action.get("working_directory")) != _path_key(cwd):
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_working_directory_mismatch",
                "detail": "strategy_id must preserve the Runtime-attested CWD candidate",
                "suggestion": "使用候選所列的既有 working_directory，仍須通過正常 Runtime Scope Admission。",
                "authorization_granted": False, "executed": False,
            }
        if (str(candidate.get("tool") or "") == "run_command"
                and operation == "GIT_MUTATE"
                and candidate.get("route_id") != "PINNED_REMOTE_ARTIFACT_ACQUISITION"
                and not _command_mentions_target(action, candidate)):
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_target_mismatch",
                "detail": "the candidate deployment target is not present in the proposed command",
                "suggestion": "保留 Runtime candidate target；不得以同一 strategy_id 改寫或擴大部署目標。",
                "authorization_granted": False, "executed": False,
            }
        if candidate.get("route_id") == "PINNED_REMOTE_ARTIFACT_ACQUISITION":
            source = candidate.get("source_artifact") or {}
            repository = str(source.get("repository") or "").lower()
            command_text = str(action.get("command") or "").lower()
            if not repository or ("github.com/" + repository) not in command_text:
                return {
                    "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                    "reason": "strategy_candidate_source_repository_mismatch",
                    "detail": "source acquisition must use the exactly registered repository",
                    "suggestion": "使用 Runtime source_artifact.repository 所指定的固定來源；任何新路徑仍須獨立授權。",
                    "authorization_granted": False, "executed": False,
                }
        contract = candidate.get("planner_action_contract")
        expected_command = (
            str(contract.get("command") or "")
            if isinstance(contract, Mapping) else ""
        )
        if expected_command and str(action.get("command") or "") != expected_command:
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_command_mismatch",
                "detail": "this candidate is bound to the exact Runtime-registered probe command",
                "suggestion": "保留 Runtime 提供的精確 probe command；若要改用其他操作，請提出新的策略並通過正常 Admission。",
                "authorization_granted": False, "executed": False,
            }
        if (candidate.get("route_id") == "GOAL_DIRECTORY_CREATE"
                and _path_key(action.get("path")) != _path_key(candidate.get("target"))):
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_directory_target_mismatch",
                "detail": "DIR-001 requires creation of the exact Runtime-confirmed missing directory",
                "suggestion": "使用 directory_decision_rule.candidate.target 指定的完整原始路徑，不得建立上層或其他子路徑。",
                "authorization_granted": False, "executed": False,
            }
        root = str(candidate.get("target_path_root") or "")
        if root and not _within(action.get("path"), root):
            return {
                "marker": "[SMARTAGENT_STRATEGY_REFERENCE_REJECTED]",
                "reason": "strategy_candidate_target_outside_root",
                "detail": "strategy_id cannot be used to widen the original target root",
                "suggestion": "將 write_file.path 限制在候選 target_path_root 內。",
                "authorization_granted": False, "executed": False,
            }
    return None


def _section(owner: str, source: str, authority: str,
             value: object, evidence_refs: list[str] | None = None) -> dict:
    row = {
        "owner": owner,
        "source_of_truth": source,
        "authority": authority,
        "value": value,
        "evidence_refs": list(dict.fromkeys(
            str(item)[:160] for item in (evidence_refs or []) if item
        ))[:32],
        "derived_not_independently_authoritative": True,
    }
    row["section_sha256"] = _digest(row)
    return row


def _event_projection(runtime, payload: Mapping) -> tuple[list[dict], bool]:
    request_id = str(getattr(runtime, "run_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    actions = dict(getattr(runtime, "action_ledger", {}) or {})
    results = dict(getattr(runtime, "action_result_ledger", {}) or {})
    events = []
    for action_id, record in actions.items():
        if action_id in results or not isinstance(record, Mapping):
            continue
        if ((record.get("request_id") and record.get("request_id") != request_id)
                or (record.get("task_epoch") and record.get("task_epoch") != epoch)):
            continue
        action = record.get("action") if isinstance(record.get("action"), Mapping) else {}
        event = {
            "event_type": "ACTION_DISPATCHED_OUTCOME_PENDING",
            "source_owner": "ACTION_LEDGER",
            "action_id": str(action_id)[:160],
            "strategy_id": str(action.get("strategy_id") or "")[:40],
            "tool": _code(action.get("tool")),
            "round": int(record.get("round", 0) or 0),
            "status": _code(record.get("execution_status")),
            "execution_status": _code(record.get("execution_status")),
            "executed": record.get("executed") is True,
            "verification_status": "NOT_RECORDED",
            "reason_code": "",
        }
        event["event_id"] = "BBE-" + _digest(event)[:20].upper()
        events.append(event)
    for action_id, evidence in results.items():
        if (not isinstance(evidence, Mapping)
                or evidence.get("request_id") != request_id
                or evidence.get("task_epoch") != epoch):
            continue
        record = actions.get(action_id) or {}
        if not isinstance(record, Mapping):
            record = {}
        action = record.get("action") if isinstance(record.get("action"), Mapping) else {}
        event = {
            "event_type": "ACTION_RESULT_RECORDED",
            "source_owner": "ACTION_RESULT_LEDGER",
            "action_id": str(action_id)[:160],
            "strategy_id": str(action.get("strategy_id") or "")[:40],
            "tool": _code(action.get("tool")),
            "round": int(evidence.get("produced_round", record.get("round", 0)) or 0),
            "status": _code(evidence.get("result_status")),
            "execution_status": _code(evidence.get("execution_status")),
            "executed": evidence.get("executed"),
            "verification_status": _code(
                evidence.get("effective_verification_status")
                or evidence.get("verification_status")
            ),
            "reason_code": _code(evidence.get("result_reason")),
        }
        event["event_id"] = "BBE-" + _digest(event)[:20].upper()
        events.append(event)
    for trace in list(getattr(runtime, "_rejected_action_resolution_traces", []) or [])[-64:]:
        if (not isinstance(trace, Mapping)
                or trace.get("request_id") != request_id
                or trace.get("task_epoch") != epoch):
            continue
        action = trace.get("planner_requested") or {}
        resolved = trace.get("runtime_resolved") or {}
        if not isinstance(action, Mapping) or not isinstance(resolved, Mapping):
            continue
        event = {
            "event_type": "ACTION_ADMISSION_REJECTED",
            "source_owner": "ACTION_RESOLUTION_TRACE",
            "action_id": str(trace.get("action_id") or "")[:160],
            "strategy_id": str(action.get("strategy_id") or "")[:40],
            "tool": _code(action.get("tool")),
            "round": int(trace.get("round", 0) or 0),
            "status": _code(resolved.get("disposition")),
            "reason_codes": [
                _code(item) for item in list(resolved.get("reason_codes") or [])[:6]
            ],
            "executed": False,
            "authorization_granted": False,
        }
        event["event_id"] = "BBE-" + _digest(event)[:20].upper()
        events.append(event)
    progress = getattr(runtime, "progress_ledger", None)
    strategy_state = dict(getattr(progress, "strategy_state", {}) or {})
    for row in list(strategy_state.get("history") or [])[-16:]:
        if not isinstance(row, Mapping):
            continue
        event = {
            "event_type": "STRATEGY_REVISION_RECORDED",
            "source_owner": "TASK_PROGRESS_STRATEGY_STATE",
            "round": int(row.get("round_id", 0) or 0),
            "revision": int(row.get("revision", 0) or 0),
            "trigger": _code(row.get("trigger")),
            "evidence_digest": str(row.get("evidence_digest") or "")[:64],
            "rejected_action_id": str(row.get("rejected_action_id") or "")[:160],
            "route_ids": [
                _code(item.get("route_id") or item.get("id"))
                for item in list(row.get("routes") or [])[:4]
                if isinstance(item, Mapping)
            ],
            "authorization_granted": False,
        }
        event["event_id"] = "BBE-" + _digest(event)[:20].upper()
        events.append(event)
    events.sort(key=lambda item: (int(item.get("round", 0) or 0), item["event_id"]))
    truncated = len(events) > 64
    return events[-64:], truncated


def build_shared_blackboard(runtime, payload: Mapping) -> dict:
    """Build the one bounded canonical read model consumed by Planner/Runtime."""
    progress = getattr(runtime, "progress_ledger", None)
    goal = str(getattr(progress, "goal", "") or getattr(runtime, "task_request", "") or "")
    request_id = str(getattr(runtime, "run_id", "") or "")
    task_id = str(getattr(runtime, "task_id", "") or "")
    epoch = str(getattr(runtime, "task_epoch", "") or "")
    strategy_state = dict(getattr(progress, "strategy_state", {}) or {})
    identity = {
        "request_id": request_id,
        "task_id": task_id,
        "task_epoch": epoch,
        "round": int(getattr(runtime, "turn_id", 0) or 0),
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        "strategy_revision": int(strategy_state.get("revision", 1) or 1),
    }
    goal_data = {
        "goal_text": goal[:32768],
        "goal_sha256": identity["goal_sha256"],
        "goal_status": dict(payload.get("goal") or {}),
        "goal_transition_readiness": dict(payload.get("goal_transition_readiness") or {}),
        "persistent_goal_gaps": dict(payload.get("persistent_goal_gaps") or {}),
    }
    environment_data = {
        "observed_runtime_facts": list(payload.get("observed_runtime_facts") or [])[:32],
        "environment_facts": list(payload.get("environment_facts") or [])[:32],
        "runtime_software_inventory": dict(payload.get("runtime_software_inventory") or {}),
    }
    capability_data = {
        "manifest": dict(payload.get("runtime_capability_manifest") or {}),
        "semantic_capability_state": dict(
            payload.get("runtime_capability_state") or {}
        ),
    }
    authorization_data = {
        "scope": dict(payload.get("scope") or {}),
        "remote_artifact_access_readiness": dict(
            payload.get("remote_artifact_access_readiness") or {}
        ),
        "deployment_path_prerequisites": dict(
            payload.get("deployment_path_prerequisites") or {}
        ),
        "authorization_granted_by_candidate": False,
    }
    action_data = {
        "recent_action_evidence": list(payload.get("recent_action_evidence") or [])[:24],
        "recent_action_provenance": list(payload.get("recent_action_provenance") or [])[:24],
        "action_resolution_trace": list(payload.get("action_resolution_trace") or [])[:24],
        "action_loop_blockers": list(payload.get("action_loop_blockers") or [])[:24],
    }
    evidence_data = {
        "failure_causal_receipts": list(payload.get("failure_causal_receipts") or [])[:16],
        "condition_evidence": list(payload.get("condition_evidence") or [])[:24],
        "evidence_total": int(payload.get("evidence_total", 0) or 0),
        "raw_tool_results_included": False,
    }
    strategy_data = {
        "directory_decision_rule": dict(payload.get("directory_decision_rule") or {}),
        "candidate_registry": dict(payload.get("strategy_candidates") or {}),
        "prerequisite_transition_gate": dict(
            payload.get("prerequisite_transition_gate") or {}
        ),
        "goal_prerequisite_resolution": dict(
            payload.get("goal_prerequisite_resolution") or {}
        ),
        "progress_strategy_state": strategy_state,
    }
    progress_data = {
        "dynamic_progress_contract": dict(payload.get("dynamic_progress_contract") or {}),
        "goal": dict(payload.get("goal") or {}),
        "accepted_progress": {
            "decision": _code(getattr(progress, "decision", "")),
            "outcome": _code(getattr(progress, "outcome", "")),
            "current_step": getattr(progress, "current_step", 0),
            "total_steps": getattr(progress, "total_steps", 0),
            "current_focus": str(getattr(progress, "current_focus", "") or "")[:1000],
            "next_action": str(getattr(progress, "next_action", "") or "")[:1000],
            "selected_action": str(getattr(progress, "selected_action", "") or "")[:160],
            "context_request": dict(getattr(progress, "context_request", {}) or {}),
            "evidence_refs": list(getattr(progress, "evidence_refs", []) or [])[:32],
            "matched_condition_id": str(
                getattr(progress, "matched_condition_id", "") or ""
            )[:160],
        },
        "action_loop_state": dict(getattr(runtime, "action_loop_state", {}) or {}),
        "runtime_transition": str(payload.get("runtime_transition") or "UNKNOWN"),
    }
    terminal_data = dict(payload.get("terminal_readiness") or {})
    sections = {
        "goal": _section(
            "TASK_PROGRESS_AND_GOAL_GAP_LEDGER", "TaskProgressLedger + GoalGapLedger",
            "PERSISTED_GOAL_AND_RUNTIME_CLASSIFIED_GAPS", goal_data,
            [str(row.get("observed_fact_ref") or "")
             for row in list((payload.get("persistent_goal_gaps") or {}).get("active_gaps") or [])
             if isinstance(row, Mapping)],
        ),
        "environment": _section(
            "RUNTIME_FACT_LEDGER", "ActionProvenance + EnvironmentFactLedger",
            "RUNTIME_OBSERVATION_ONLY", environment_data,
            [str(row.get("evidence_ref") or "")
             for row in environment_data["observed_runtime_facts"]
             if isinstance(row, Mapping)],
        ),
        "capabilities": _section(
            "RUNTIME_CAPABILITY_REGISTRY", "ToolEnvelopeSchemas + ToolCapabilityPolicy",
            "RUNTIME_DECLARED_CAPABILITY", capability_data,
        ),
        "authorization": _section(
            "SECURITY_CONTEXT_AND_REMOTE_ACCESS", "SecurityContext + RuntimeAccessReceipt",
            "RUNTIME_AUTHORIZATION_DECISION", authorization_data,
        ),
        "actions": _section(
            "ACTION_LEDGER_AND_ACTION_RESOLUTION", "ActionLedger + ActionResolutionTrace",
            "RUNTIME_ACTION_LIFECYCLE", action_data,
            [str(row.get("action_id") or "")
             for row in action_data["recent_action_evidence"]
             if isinstance(row, Mapping)],
        ),
        "evidence": _section(
            "ACTION_RESULT_AND_CONDITION_LEDGER", "ActionResultLedger + ConditionResultLedger",
            "RUNTIME_EVIDENCE", evidence_data,
            [str(row.get("action_id") or "")
             for row in evidence_data["failure_causal_receipts"]
             if isinstance(row, Mapping)],
        ),
        "strategy": _section(
            "STRATEGY_CANDIDATE_ENGINE", "RuntimeAttestedStrategyHistory + CurrentAdmissionPreview",
            "RUNTIME_DERIVED_CANDIDATES_PLANNER_SELECTS", strategy_data,
            [str(row.get("evidence_digest") or "")
             for row in list(strategy_state.get("history") or [])
             if isinstance(row, Mapping)],
        ),
        "progress": _section(
            "TASK_PROGRESS_LEDGER", "TaskProgressLedger + ActionLoopState",
            "PLANNER_PROPOSAL_RUNTIME_ACCEPTED_STATE", progress_data,
        ),
        "terminal": _section(
            "RUNTIME_TERMINAL_READINESS_GATE", "StrategyCandidateRegistry + UnresolvedActionState",
            "RUNTIME_TERMINAL_CONSTRAINT", terminal_data,
        ),
    }
    events, events_truncated = _event_projection(runtime, payload)
    board = {
        "schema": SCHEMA,
        "version": 1,
        "identity": identity,
        "sections": sections,
        "event_projection": events,
        "event_projection_truncated": events_truncated,
        "event_count": len(events),
        "ownership": {
            "planner_may_propose": [
                "STRATEGY", "ACTION", "PROGRESS", "CONTEXT_REQUEST",
            ],
            "runtime_owns": [
                "CAPABILITY_MANIFEST", "OBSERVATION", "ADMISSION", "EXECUTION",
                "TOOL_RESULT", "VERIFICATION", "TERMINAL_ACCEPTANCE",
            ],
            "security_owner": ["AUTHORIZATION", "SCOPE_DECISION"],
            "evidence_owner": ["EVIDENCE_RECORD", "EVIDENCE_IDENTITY"],
            "cross_owner_write_allowed": False,
        },
        "invariants": {
            "single_request_epoch_snapshot": True,
            "sections_are_owner_projected": True,
            "owner_ledgers_remain_authoritative": True,
            "derived_sections_are_not_independent_authority": True,
            "unknown_is_not_false": True,
            "candidate_is_not_authorization": True,
            "planner_proposal_is_not_runtime_evidence": True,
            "scope_expansion_requires_runtime_admission": True,
            "terminal_success_requires_independent_evidence": True,
        },
        "raw_tool_results_included": False,
        "raw_commands_included": False,
    }
    board_digest = _digest(board)
    board["snapshot_id"] = "BB-" + board_digest[:20].upper()
    board["snapshot_sha256"] = board_digest
    return board


__all__ = [
    "CAPABILITY_SCHEMA", "CANDIDATE_SCHEMA", "SCHEMA", "TERMINAL_SCHEMA",
    "build_runtime_capability_manifest", "build_strategy_candidate_registry",
    "build_shared_blackboard", "evaluate_terminal_readiness",
    "interrupt_terminal_diagnostic", "validate_strategy_candidate_bindings",
]
