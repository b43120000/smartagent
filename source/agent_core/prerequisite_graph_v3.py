"""PR-A: request-scoped prerequisite DAG and durable, non-authoritative ledger.

This records requirements and Runtime evidence; it does NOT authorize scopes,
execute prerequisites, infer successful tool results, or alter V9 admission.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_PREREQ_V3_GRAPH_LEDGER_V1"
STATES = frozenset({"PENDING", "SATISFIED", "FAILED", "UNKNOWN"})
MAX_NODES = 512
MAX_EVENTS = 8192
_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_LABEL = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


class PrerequisiteGraphError(ValueError):
    """A rejected or corrupt PR-A graph never creates implicit evidence."""


def _require_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PrerequisiteGraphError("pr_a_invalid_" + field)
    return value


def _labels(items: object, field: str) -> list[str]:
    if not isinstance(items, (tuple, list)) or len(items) > 64:
        raise PrerequisiteGraphError("pr_a_invalid_" + field)
    if any(not isinstance(item, str) or not _LABEL.fullmatch(item) for item in items):
        raise PrerequisiteGraphError("pr_a_invalid_" + field)
    return sorted(set(items))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: dict) -> str:
    return _hash(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _node(identifier: str, kind: str, deps: Sequence[str] = (),
          *, action_id: str = "", scope_id: str = "", label: str = "") -> dict:
    return {"id": identifier, "kind": kind, "depends_on": sorted(set(deps)),
            "action_id": action_id, "scope_id": scope_id, "label": label,
            "state": "PENDING", "evidence_ref_digest": ""}


def _validate(graph: dict) -> None:
    if not isinstance(graph, dict) or graph.get("schema") != SCHEMA:
        raise PrerequisiteGraphError("pr_a_schema_mismatch")
    identity = graph.get("identity")
    if not isinstance(identity, dict) or set(identity) != {
        "task_id", "request_id", "task_epoch", "intent_sha256"
    }:
        raise PrerequisiteGraphError("pr_a_identity_invalid")
    for key in ("task_id", "request_id", "task_epoch"):
        _require_id(identity[key], key)
    if not isinstance(identity["intent_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", identity["intent_sha256"]):
        raise PrerequisiteGraphError("pr_a_intent_invalid")
    nodes = graph.get("nodes")
    events = graph.get("events")
    if (not isinstance(nodes, dict) or not 1 <= len(nodes) <= MAX_NODES
            or not isinstance(events, list) or len(events) > MAX_EVENTS
            or type(graph.get("revision")) is not int or graph["revision"] < 1):
        raise PrerequisiteGraphError("pr_a_limits_or_shape_invalid")
    for name, node in nodes.items():
        if not isinstance(node, dict) or node.get("id") != name or not isinstance(name, str):
            raise PrerequisiteGraphError("pr_a_node_identity_invalid")
        if not re.fullmatch(r"[a-z0-9:._-]{1,190}", name):
            raise PrerequisiteGraphError("pr_a_node_name_invalid")
        if node.get("kind") not in {"TASK", "SOURCE", "CAPABILITY", "OPERATION", "SCOPE", "ACTION"}:
            raise PrerequisiteGraphError("pr_a_node_kind_invalid")
        if node.get("state") not in STATES:
            raise PrerequisiteGraphError("pr_a_state_invalid")
        if not isinstance(node.get("evidence_ref_digest"), str) or (
            node["evidence_ref_digest"] and not re.fullmatch(r"[0-9a-f]{64}", node["evidence_ref_digest"])
        ):
            raise PrerequisiteGraphError("pr_a_evidence_invalid")
        deps = node.get("depends_on")
        if not isinstance(deps, list) or len(deps) > 96 or deps != sorted(set(deps)):
            raise PrerequisiteGraphError("pr_a_dependencies_invalid")
        if name in deps or any(dep not in nodes for dep in deps):
            raise PrerequisiteGraphError("pr_a_dependency_missing_or_self")
        if node["kind"] == "ACTION" and not _ID.fullmatch(str(node.get("action_id", ""))):
            raise PrerequisiteGraphError("pr_a_action_id_invalid")
    # Strict topological validation: no cycles, even if the file was edited.
    marks: dict[str, int] = {}
    def visit(key: str) -> None:
        if marks.get(key) == 1:
            raise PrerequisiteGraphError("pr_a_cycle")
        if marks.get(key) == 2:
            return
        marks[key] = 1
        for dep in nodes[key]["depends_on"]:
            visit(dep)
        marks[key] = 2
    for key in nodes:
        visit(key)
    for index, event in enumerate(events, 1):
        if not isinstance(event, dict) or event.get("seq") != index:
            raise PrerequisiteGraphError("pr_a_event_sequence_invalid")
        if event.get("node") not in nodes:
            raise PrerequisiteGraphError("pr_a_event_node_unknown")
        if event.get("type") not in {"CREATED", "ACTION_BOUND", "EVIDENCE"}:
            raise PrerequisiteGraphError("pr_a_event_type_invalid")
    if graph["revision"] != len(events):
        raise PrerequisiteGraphError("pr_a_revision_mismatch")
    stored = graph.get("digest")
    if stored != _digest({key: value for key, value in graph.items() if key != "digest"}):
        raise PrerequisiteGraphError("pr_a_integrity_mismatch")


def _seal(graph: dict) -> dict:
    graph["revision"] = len(graph["events"])
    graph["digest"] = _digest({key: value for key, value in graph.items() if key != "digest"})
    _validate(graph)
    return graph


def _scope_id(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PrerequisiteGraphError("pr_a_scope_invalid")
    # Scope hashes are tags, NOT authorization tokens. Never persist raw paths.
    return "scope:" + _hash(path.replace("\\", "/").casefold())[:32]


def build_requirement_graph(*, task_id: str, request_id: str, task_epoch: str,
                            assessment: Mapping[str, object],
                            authorized_paths: Sequence[str] = ()) -> dict:
    """Freeze PR-0 classification into an unordered-evidence prerequisite DAG."""
    for field, value in (("task_id", task_id), ("request_id", request_id),
                         ("task_epoch", task_epoch)):
        _require_id(value, field)
    if not isinstance(assessment, Mapping) or assessment.get("schema") != "SMARTAGENT_TASK_REQUIREMENT_V1":
        raise PrerequisiteGraphError("pr_a_pr0_assessment_required")
    intent = assessment.get("intent_sha256")
    if not isinstance(intent, str) or not re.fullmatch(r"[0-9a-f]{64}", intent):
        raise PrerequisiteGraphError("pr_a_intent_invalid")
    sources = _labels(assessment.get("resource_sources", []), "sources")
    capabilities = _labels(assessment.get("required_capabilities", []), "capabilities")
    operations = _labels(assessment.get("required_operations", []), "operations")
    scopes = sorted({_scope_id(p) for p in authorized_paths})
    if len(scopes) > 64:
        raise PrerequisiteGraphError("pr_a_scope_count_exceeded")
    nodes = {"task:intent": _node("task:intent", "TASK", label="INTENT")}
    for name in sources:
        identifier = "source:" + name.lower()
        nodes[identifier] = _node(identifier, "SOURCE", ["task:intent"], label=name)
    for name in capabilities:
        identifier = "capability:" + name.lower()
        nodes[identifier] = _node(identifier, "CAPABILITY", ["task:intent"], label=name)
    for scope in scopes:
        nodes[scope] = _node(scope, "SCOPE", ["task:intent"], scope_id=scope)
    for operation in operations:
        deps = ["task:intent"] + [
            name for name in nodes if name.startswith(("source:", "capability:"))
        ] + (scopes if "LOCAL" in sources else [])
        identifier = "operation:" + operation.lower()
        nodes[identifier] = _node(identifier, "OPERATION", deps, label=operation)
    nodes["task:goal"] = _node(
        "task:goal", "TASK",
        [x for x in nodes if x.startswith("operation:")] or ["task:intent"],
        label="GOAL",
    )
    events = [{"seq": index, "type": "CREATED", "node": key}
              for index, key in enumerate(sorted(nodes), 1)]
    return _seal({"schema": SCHEMA, "identity": {
        "task_id": task_id, "request_id": request_id, "task_epoch": task_epoch,
        "intent_sha256": intent,
    }, "nodes": nodes, "events": events, "revision": 0})


class RequirementLedger:
    """Cross-process serialized, atomic per-Task snapshots with audited events."""

    def __init__(self, install_root: str | Path):
        base = Path(install_root).expanduser().resolve()
        self.directory = base / "localdata" / "runtime" / "prerequisite_v3"

    def _path(self, task_id: str) -> Path:
        return self.directory / (_require_id(task_id, "task_id") + ".json")

    def _read_unlocked(self, task_id: str) -> dict | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        graph = read_json_retry(path)
        _validate(graph)
        if graph["identity"]["task_id"] != task_id:
            raise PrerequisiteGraphError("pr_a_task_mismatch")
        return graph

    def read(self, task_id: str) -> dict | None:
        return self._read_unlocked(task_id)

    def _locked(self, task_id: str):
        path = self._path(task_id)
        return exclusive_process_lock(
            path.with_suffix(".lock"), timeout_sec=10,
            label="PR-A requirement ledger", legacy_kind="smartagent-pr-a-lock-v1",
        )

    @staticmethod
    def _identity(graph: dict, request_id: str, task_epoch: str) -> None:
        if (graph["identity"]["request_id"] != _require_id(request_id, "request_id")
                or graph["identity"]["task_epoch"] != _require_id(task_epoch, "task_epoch")):
            raise PrerequisiteGraphError("pr_a_request_epoch_mismatch")

    def initialize(self, graph: dict) -> dict:
        _validate(graph)
        task_id = graph["identity"]["task_id"]
        with self._locked(task_id):
            existing = self._read_unlocked(task_id)
            if existing is not None:
                if (existing["identity"] != graph["identity"]
                        or any(existing["nodes"].get(k) != v for k, v in graph["nodes"].items())):
                    # An existing mutable/advanced graph is valid if its initial
                    # node identities/edges remain; do not reset accumulated evidence.
                    for key, baseline in graph["nodes"].items():
                        current = existing["nodes"].get(key)
                        if current is None or any(current.get(f) != baseline[f]
                            for f in ("id", "kind", "depends_on", "scope_id", "label")):
                            raise PrerequisiteGraphError("pr_a_initial_graph_conflict")
                    if existing["identity"] != graph["identity"]:
                        raise PrerequisiteGraphError("pr_a_initial_identity_conflict")
                return existing
            write_json_atomic(self._path(task_id), graph)
            return graph

    def bind_action(self, *, task_id: str, request_id: str, task_epoch: str,
                    action_id: str, depends_on: Sequence[str] = (),
                    scope_paths: Sequence[str] = (), tool: str = "") -> dict:
        """Record action edges only. Does not approve the selected tool or scope."""
        _require_id(action_id, "action_id")
        if not isinstance(depends_on, (list, tuple)) or len(depends_on) > 32:
            raise PrerequisiteGraphError("pr_a_action_dependencies_invalid")
        for dep in depends_on:
            _require_id(dep, "dependency_action_id")
        if len(set(depends_on)) != len(depends_on) or action_id in depends_on:
            raise PrerequisiteGraphError("pr_a_action_dependencies_invalid")
        if not isinstance(tool, str) or len(tool) > 100:
            raise PrerequisiteGraphError("pr_a_tool_invalid")
        paths = [_scope_id(p) for p in scope_paths]
        if len(paths) > 32:
            raise PrerequisiteGraphError("pr_a_action_scope_exceeded")
        with self._locked(task_id):
            graph = self._read_unlocked(task_id)
            if graph is None:
                raise PrerequisiteGraphError("pr_a_task_not_initialized")
            self._identity(graph, request_id, task_epoch)
            nodes = graph["nodes"]
            dep_keys = ["task:intent"] + [
                "action:" + dep.lower() for dep in depends_on
            ]
            if any(dep not in nodes for dep in dep_keys):
                raise PrerequisiteGraphError("pr_a_dependency_not_bound")
            # A new scope is an unresolved annotation, never scope widening.
            for key in sorted(set(paths)):
                if key not in nodes:
                    nodes[key] = _node(key, "SCOPE", ["task:intent"], scope_id=key)
                    graph["events"].append({"seq": len(graph["events"]) + 1,
                                             "type": "CREATED", "node": key})
                dep_keys.append(key)
            key = "action:" + action_id.lower()
            candidate = _node(key, "ACTION", dep_keys, action_id=action_id,
                              label=tool.upper() if _LABEL.fullmatch(tool.upper()) else "TOOL")
            if key in nodes:
                prior = nodes[key]
                if any(prior.get(field) != candidate[field] for field in
                       ("id", "kind", "depends_on", "action_id", "label")):
                    raise PrerequisiteGraphError("pr_a_action_rebind_conflict")
            else:
                nodes[key] = candidate
                graph["events"].append({"seq": len(graph["events"]) + 1,
                                         "type": "ACTION_BOUND", "node": key})
            _seal(graph)
            write_json_atomic(self._path(task_id), graph)
            return graph

    def record_action_evidence(self, *, task_id: str, request_id: str, task_epoch: str,
                               action_id: str, status: str,
                               evidence_ref: str = "") -> dict:
        """Only the Runtime caller can attest evidence; no model interpretation."""
        _require_id(action_id, "action_id")
        if status not in STATES:
            raise PrerequisiteGraphError("pr_a_evidence_state_invalid")
        if not isinstance(evidence_ref, str) or len(evidence_ref) > 256:
            raise PrerequisiteGraphError("pr_a_evidence_ref_invalid")
        if status == "SATISFIED" and not evidence_ref:
            raise PrerequisiteGraphError("pr_a_success_requires_evidence")
        with self._locked(task_id):
            graph = self._read_unlocked(task_id)
            if graph is None:
                raise PrerequisiteGraphError("pr_a_task_not_initialized")
            self._identity(graph, request_id, task_epoch)
            node = graph["nodes"].get("action:" + action_id.lower())
            if not node or node["action_id"] != action_id:
                raise PrerequisiteGraphError("pr_a_action_unknown")
            digest = _hash(evidence_ref) if evidence_ref else ""
            if node["state"] == status and node["evidence_ref_digest"] == digest:
                return graph
            if node["state"] != "PENDING":
                raise PrerequisiteGraphError("pr_a_evidence_conflict")
            node["state"], node["evidence_ref_digest"] = status, digest
            graph["events"].append({"seq": len(graph["events"]) + 1,
                                    "type": "EVIDENCE", "node": node["id"]})
            _seal(graph)
            write_json_atomic(self._path(task_id), graph)
            return graph


def graph_summary(graph: dict) -> dict:
    _validate(graph)
    states = {state: 0 for state in sorted(STATES)}
    for node in graph["nodes"].values():
        states[node["state"]] += 1
    return {"schema": SCHEMA, "task_id": graph["identity"]["task_id"],
            "revision": graph["revision"], "node_count": len(graph["nodes"]),
            "states": states, "digest": graph["digest"],
            "authorizes_execution": False, "verification_complete": False}
