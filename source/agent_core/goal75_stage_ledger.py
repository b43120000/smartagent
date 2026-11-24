"""Git75 stage-local, immutable-after-PASS execution ledger.

Stages are semantic Planner proposals, NOT authority to grant tools/scope.
The Runtime can commit a stage only against frozen typed predicates and
independently observed tool receipts. All state transitions are append-audited.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
from collections.abc import Mapping

from .goal75_blackboard import digest, emit_transition
from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_GOAL75_STAGE_LEDGER_V1"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_FIELD = re.compile(r"^(?:status|executed|data\.[A-Za-z_][A-Za-z0-9_.]{0,119})$")
_OPERATORS = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})
_MAX_STAGES = 64
_MAX_CHECKS = 16


def _where(root: str | Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(task_id)):
        raise ValueError("goal75_task_id_invalid")
    base = Path(root).resolve()
    if base.name.casefold() == "source" and (base.parent / "collect.bat").exists():
        base = base.parent
    return base / "localdata" / "runtime" / "goal75_stages" / (task_id + ".json")


def _plain(value: object) -> bool:
    return type(value) in {str, int, float, bool, type(None)} and (
        not isinstance(value, str) or len(value) <= 512
    )


def _stage_plan(stages: object) -> list[dict]:
    if not isinstance(stages, list) or not 1 <= len(stages) <= _MAX_STAGES:
        raise ValueError("goal75_stage_count_invalid")
    output: list[dict] = []
    identifiers = set()
    for number, row in enumerate(stages):
        if not isinstance(row, Mapping) or set(row) != {
            "stage_id", "description", "depends_on", "acceptance",
        }:
            raise ValueError("goal75_stage_schema_invalid")
        sid = row["stage_id"]
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in identifiers:
            raise ValueError("goal75_stage_id_invalid_or_duplicate")
        text = row["description"]
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 800:
            raise ValueError("goal75_description_invalid")
        deps = row["depends_on"]
        if (not isinstance(deps, list) or len(deps) != len(set(deps))
                or not all(isinstance(x, str) and x in identifiers for x in deps)):
            raise ValueError("goal75_dependency_not_preceding_stage")
        checks = row["acceptance"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= _MAX_CHECKS:
            raise ValueError("goal75_acceptance_required")
        normalized_checks = []
        for check in checks:
            if not isinstance(check, Mapping) or set(check) not in (
                    {"field", "operator", "expected"},
                    {"field", "operator", "expected", "binding"}):
                raise ValueError("goal75_check_schema_invalid")
            if (not isinstance(check["field"], str) or not _FIELD.fullmatch(check["field"])
                    or check["operator"] not in _OPERATORS or not _plain(check["expected"])):
                raise ValueError("goal75_check_invalid")
            if check["operator"] not in {"eq", "ne"} and (
                type(check["expected"]) not in {int, float}
            ):
                raise ValueError("goal75_numeric_check_invalid")
            if "binding" in check:
                binding = check["binding"]
                if (not isinstance(binding, Mapping)
                        or set(binding) != {"kind", "subject", "before_mutation"}
                        or binding["kind"] != "file.exists"
                        or check["field"] != "data.observations.0.value"
                        or check["operator"] != "eq"
                        or type(check["expected"]) is not bool
                        or type(binding["subject"]) is not str
                        or not os.path.isabs(binding["subject"])
                        or len(binding["subject"]) > 1024
                        or any(c in binding["subject"] for c in ("*", "?", "\x00"))
                        or type(binding["before_mutation"]) is not bool):
                    raise ValueError("goal76_file_exists_binding_invalid")
                normalized_checks.append({**check, "binding": dict(binding)})
            else:
                normalized_checks.append(dict(check))
        identifiers.add(sid)
        output.append({
            "stage_id": sid,
            "description": text.strip(),
            "depends_on": list(deps),
            "acceptance": normalized_checks,
        })
    return output


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    row = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(row, dict) or row.get("schema") != SCHEMA:
        raise ValueError("goal75_stage_record_corrupt")
    basis = [
        {field: stage[field] for field in ("stage_id", "description", "depends_on", "acceptance")}
        for stage in row["stages"]
    ]
    if digest(basis) != row["plan_sha256"]:
        raise ValueError("goal75_frozen_plan_tampered")
    for stage in row["stages"]:
        receipt = stage.get("commit")
        if stage.get("status") == "VERIFIED_COMMITTED" and not isinstance(receipt, dict):
            raise ValueError("goal75_stage_commit_missing")
        if receipt is not None:
            if not isinstance(receipt, dict):
                raise ValueError("goal75_stage_commit_invalid")
            body = {key: value for key, value in receipt.items()
                    if key != "commit_sha256"}
            if digest(body) != receipt.get("commit_sha256"):
                raise ValueError("goal75_stage_commit_tampered")
            if (receipt.get("stage_id") != stage.get("stage_id")
                    or receipt.get("plan_sha256") != row["plan_sha256"]):
                raise ValueError("goal75_stage_commit_plan_binding_invalid")
            observed = {item.get("evidence_ref"): item
                        for item in stage.get("observations", [])}
            refs = list(receipt.get("evidence_refs") or [])
            if (not refs or len(refs) != len(set(refs))
                    or any(ref not in observed for ref in refs)):
                raise ValueError("goal75_stage_commit_observation_missing")
            coverage = {i for ref in refs
                        for i in observed[ref].get("acceptance_matched", [])}
            if coverage != set(range(len(stage["acceptance"]))):
                raise ValueError("goal75_stage_commit_coverage_tampered")
            if any("binding" in check for check in stage["acceptance"]):
                proofs = [observed[ref].get("binding_proofs", []) for ref in refs]
                if (digest(proofs) != receipt.get("binding_proofs_sha256")
                        or {p["check_index"] for group in proofs for p in group}
                        != {i for i, c in enumerate(stage["acceptance"]) if "binding" in c}):
                    raise ValueError("goal76_binding_commit_tampered")
    return row


def _atomic(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".goal75-stage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _actual(receipt: Mapping, field: str) -> tuple[bool, object]:
    value: object = receipt
    for segment in field.split("."):
        if isinstance(value, Mapping):
            if segment not in value:
                return False, None
            value = value[segment]
        elif isinstance(value, list):
            # JSON array indexes must be canonical, non-negative decimals.
            # Missing paths never become a false Boolean observation.
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                return False, None
            index = int(segment)
            if index >= len(value):
                return False, None
            value = value[index]
        else:
            return False, None
    return True, value


def _matches(check: Mapping, receipt: Mapping) -> bool:
    present, value = _actual(receipt, check["field"])
    if not present:
        return False
    expected = check["expected"]
    op = check["operator"]
    if op in {"eq", "ne"}:
        matched = type(value) is type(expected) and value == expected
        return matched if op == "eq" else (not matched and type(value) is type(expected))
    if type(value) not in {int, float}:
        return False
    return {
        "gt": lambda: value > expected,
        "ge": lambda: value >= expected,
        "lt": lambda: value < expected,
        "le": lambda: value <= expected,
    }[op]()


# A frozen semantic binding is not an arbitrary field alias.
def _bound_fact(check: Mapping, receipt: Mapping, action_id: str) -> dict | None:
    binding = check["binding"]
    data = receipt.get("data")
    if (receipt.get("tool") != "observe_fact"
            or receipt.get("status") != "SUCCEEDED"
            or receipt.get("executed") is not True
            or receipt.get("action_id") != action_id
            or not isinstance(data, Mapping)
            or data.get("execution_finality") != "COMPLETED"
            or data.get("observation_status") != "CONFIRMED"
            or data.get("terminal_eligible") is not True
            or data.get("verification_status") not in {"PASS", "UNVERIFIED"}):
        return None
    facts = data.get("observations")
    if not isinstance(facts, list) or len(facts) != 1:
        return None
    fact = facts[0]
    subject = binding["subject"]
    scope = fact.get("scope") if isinstance(fact, Mapping) else None
    resolved = receipt.get("resolved_path")
    if (not isinstance(fact, Mapping)
            or fact.get("schema") != "SMARTAGENT_EVIDENCE_V1"
            or fact.get("field") != "file.exists"
            or fact.get("source") != "filesystem.stat"
            or fact.get("raw_evidence_ref") != action_id
            or fact.get("observation_status") != "CONFIRMED"
            or fact.get("freshness") != "CURRENT"
            or not isinstance(scope, str)
            or not isinstance(resolved, str)
            or not os.path.isabs(scope)
            or not os.path.isabs(resolved)
            or os.path.normcase(os.path.normpath(scope)) != os.path.normcase(os.path.normpath(subject))
            or os.path.normcase(os.path.normpath(resolved)) != os.path.normcase(os.path.normpath(subject))
            or type(fact.get("value")) is not bool
            or fact["value"] is not check["expected"]):
        return None
    return {"field": "data.observations.0.value",
            "kind": "file.exists", "subject_sha256": digest(subject),
            "expected_sha256": digest(check["expected"]),
            "before_mutation": binding["before_mutation"],
            "schema": fact["schema"]}


class GoalStageLedger:
    def __init__(self, root: str | Path, *, task_id: str, request_id: str,
                 task_epoch: str, goal: str):
        self.root = Path(root)
        self.path = _where(root, task_id)
        self.task_id = str(task_id)
        self.request_id = str(request_id)
        self.task_epoch = str(task_epoch)
        self.goal_sha256 = digest(str(goal))

    def read(self) -> dict | None:
        row = _read(self.path)
        if row and any((
            row["task_id"] != self.task_id,
            row["request_id"] != self.request_id,
            row["task_epoch"] != self.task_epoch,
            row["goal_sha256"] != self.goal_sha256,
        )):
            raise ValueError("goal75_stage_identity_mismatch")
        return row

    def _save(self, before: dict | None, after: dict, event: str, sid: str) -> dict:
        after["revision"] = int((before or {}).get("revision", 0)) + 1
        after["updated_at"] = time.time()
        _atomic(self.path, after)
        emit_transition(
            self.root, task_id=self.task_id, request_id=self.request_id,
            task_epoch=self.task_epoch, module="GOAL75_STAGE_LEDGER",
            event=event, stage_id=sid, before=before, after=after,
        )
        return after

    def create(self, stages: list[dict]) -> dict:
        frozen = _stage_plan(stages)
        with exclusive_process_lock(self.path.with_suffix(".lock"), label="goal75-stage"):
            existing = self.read()
            if existing:
                if existing["plan_sha256"] != digest(frozen):
                    raise ValueError("goal75_accepted_stage_plan_immutable")
                return existing
            payload = {
                "schema": SCHEMA,
                "task_id": self.task_id, "request_id": self.request_id,
                "task_epoch": self.task_epoch,
                "goal_sha256": self.goal_sha256,
                "plan_sha256": digest(frozen),
                "status": "IN_PROGRESS",
                "stages": [
                    {**entry, "status": "PENDING", "strategy_revision": 0,
                     "observations": [], "commit": None, "failures": []}
                    for entry in frozen
                ],
            }
            return self._save(None, payload, "PLAN_FROZEN", "")

    def current(self) -> dict | None:
        row = self.read()
        if not row:
            return None
        done = {s["stage_id"] for s in row["stages"] if s["status"] == "VERIFIED_COMMITTED"}
        for stage in row["stages"]:
            if stage["status"] == "VERIFIED_COMMITTED":
                continue
            if all(dep in done for dep in stage["depends_on"]):
                return stage
        return None

    def _change(self, sid: str, event: str, modify) -> dict:
        with exclusive_process_lock(self.path.with_suffix(".lock"), label="goal75-stage"):
            before = self.read()
            if not before:
                raise ValueError("goal75_stage_plan_missing")
            after = json.loads(json.dumps(before))
            stage = next((s for s in after["stages"] if s["stage_id"] == sid), None)
            if stage is None:
                raise ValueError("goal75_stage_unknown")
            if stage["status"] == "VERIFIED_COMMITTED":
                raise ValueError("goal75_stage_frozen_no_mutation")
            current = next((
                s for s in after["stages"] if s["status"] != "VERIFIED_COMMITTED"
            ), None)
            if current is not stage and current is not None:
                raise ValueError("goal75_stage_dependency_not_committed")
            modify(stage)
            return self._save(before, after, event, sid)

    def committed_mutation_fingerprints(self) -> set[str]:
        row = self.read()
        return {
            str(item["mutation_fingerprint"])
            for stage in (row or {}).get("stages", [])
            if stage["status"] == "VERIFIED_COMMITTED"
            for item in stage.get("observations", [])
            if item.get("mutating") is True and item.get("mutation_fingerprint")
        }

    def completed_bound_mutation_fingerprints(self) -> set[str]:
        stage = self.current()
        if not stage or not any(c.get("binding", {}).get("before_mutation") is True
                                for c in stage["acceptance"]):
            return set()
        return {str(item["mutation_fingerprint"]) for item in stage["observations"]
                if item.get("mutating") is True and item.get("status") == "SUCCEEDED"
                and item.get("mutation_fingerprint")}

    def _read_bound_receipt(self, item: Mapping) -> dict | None:
        if (item.get("evidence_store_verified") is not True
                or item.get("expected_matched") is not True
                or item.get("mutating") is not False):
            return None
        from .runtime_evidence_store import read_runtime_action_result
        from .tool_result_contract import decode_result
        try:
            raw = read_runtime_action_result(
                self.root, task_id=self.task_id, request_id=self.request_id,
                task_epoch=self.task_epoch, action_id=item["action_id"],
                evidence_ref=item["evidence_ref"],
            )
            receipt = decode_result(raw)
            if not isinstance(receipt, dict) or digest(receipt) != item["receipt_sha256"]:
                return None
            return receipt
        except (ValueError, OSError, TypeError, KeyError):
            return None

    def _reconcile_bindings(self, stage: dict) -> None:
        # Re-evaluate only frozen semantic predicates, in original action order.
        # No historical action or immutable producer receipt is rewritten.
        bound = {i: c for i, c in enumerate(stage["acceptance"]) if "binding" in c}
        if not bound:
            return
        mutation_seen = False
        boundary = stage.get("mutation_boundary")
        for ordinal, item in enumerate(stage["observations"]):
            item["acceptance_matched"] = sorted(
                set(item["acceptance_matched"]) - set(bound))
            item["binding_proofs"] = []
            if item.get("mutating") is True:
                mutation_seen = True
                continue
            receipt = self._read_bound_receipt(item)
            if receipt is None:
                continue
            for index, check in bound.items():
                if check["binding"]["before_mutation"] and (
                        mutation_seen or (boundary is not None and ordinal >= boundary)):
                    continue
                proof = _bound_fact(check, receipt, item["action_id"])
                if proof is not None:
                    item["acceptance_matched"].append(index)
                    item["binding_proofs"].append({
                        **proof, "check_index": index,
                        "evidence_ref": item["evidence_ref"],
                        "receipt_sha256": item["receipt_sha256"],
                    })
            item["acceptance_matched"].sort()

    def note_mutation_boundary(self, sid: str) -> None:
        stage = self.current()
        if not stage or stage["stage_id"] != sid:
            raise ValueError("goal76_stage_not_current")
        if not any(c.get("binding", {}).get("before_mutation") is True
                   for c in stage["acceptance"]):
            return
        def apply(active):
            self._reconcile_bindings(active)
            covered = {i for row in active["observations"] for i in row["acceptance_matched"]}
            if any(i not in covered and c.get("binding", {}).get("before_mutation") is True
                   for i, c in enumerate(active["acceptance"])):
                raise ValueError("goal76_pre_mutation_evidence_missing")
            active.setdefault("mutation_boundary", len(active["observations"]))
        self._change(sid, "STAGE_MUTATION_BOUNDARY", apply)

    def missing_pre_mutation_bindings(self) -> list[int]:
        stage = self.current()
        if not stage:
            return []
        self._reconcile_bindings(stage)
        covered = {index for row in stage["observations"]
                   for index in row["acceptance_matched"]}
        return [i for i, check in enumerate(stage["acceptance"])
                if check.get("binding", {}).get("before_mutation") is True
                and i not in covered]

    def record_action(self, sid: str, *, action_id: str, evidence_ref: str,
                      receipt: Mapping, mutation_fingerprint: str = "",
                      mutating: bool = False,
                      expected_match: bool | None = None,
                      evidence_store_verified: bool = False) -> dict:
        """Bind only completed, actually executed Runtime receipts to this stage."""
        if (not isinstance(receipt, Mapping)
                or receipt.get("status") not in {"SUCCEEDED", "FAILED"}
                or receipt.get("executed") is not True):
            raise ValueError("goal75_action_finality_not_verified")
        if (not isinstance(action_id, str) or not action_id
                or not isinstance(evidence_ref, str) or not evidence_ref):
            raise ValueError("goal75_evidence_identity_invalid")
        def apply(stage):
            if any(item["action_id"] == action_id for item in stage["observations"]):
                raise ValueError("goal75_action_duplicate")
            # Git75 Live RCA: a tool may return status=SUCCEEDED, exit_code=0
            # while the predeclared Expected-vs-Observed verification is FAIL.
            # That receipt is *not* acceptance evidence, regardless of what
            # planner-authored predicates say. Negative tests which match
            # their frozen Expected have verification_status=PASS.
            verified = expected_match is not False and not (
                isinstance(receipt.get("data"), Mapping)
                and receipt["data"].get("verification_status")
                    in {"FAIL", "MISMATCH", "UNVERIFIED"}
            )
            matches = [
                index for index, check in enumerate(stage["acceptance"])
                if "binding" not in check and verified and _matches(check, receipt)
            ]
            unobserved = [
                index for index, check in enumerate(stage["acceptance"])
                if not _actual(receipt, check["field"])[0]
            ]
            stage["observations"].append({
                "action_id": action_id, "evidence_ref": evidence_ref,
                "receipt_sha256": digest(receipt),
                "status": receipt["status"],
                "acceptance_matched": matches,
                "acceptance_unobserved": unobserved,
                "acceptance_receipt_eligible": verified,
                "expected_matched": expected_match,
                "evidence_store_verified": evidence_store_verified is True,
                "mutating": bool(mutating),
                "mutation_fingerprint": str(mutation_fingerprint)[:64]
                if mutating else "",
            })
            item = stage["observations"][-1]
            # A confirmed, complete Git identity is an observation, not a
            # successful mutation or a retroactive override of FAIL. Permit
            # only the typed identity fields with a matching frozen Expected
            # and a persisted producer readback.
            if not verified:
                readback = self._read_bound_receipt(item)
                if readback is not None:
                    d = readback.get("data") or {}
                    if (readback.get("tool") == "run_command"
                            and readback.get("status") == "SUCCEEDED"
                            and d.get("operation") == "GIT_INSPECT"
                            and d.get("verification_status") == "UNVERIFIED"
                            and d.get("execution_status") == "SUCCEEDED"
                            and d.get("exit_code") == 0
                            and d.get("git_observation_confirmed") is True
                            and d.get("output_complete") is True):
                        allowed = {"data.git_local_head_sha", "data.git_remote_head_sha",
                                   "data.git_remote_ref", "data.git_remote_repository"}
                        matched = [i for i, check in enumerate(stage["acceptance"])
                                   if "binding" not in check and check["field"] in allowed
                                   and _matches(check, readback)]
                        item["acceptance_matched"] = matched
                        item["git_observation_matched"] = matched
            self._reconcile_bindings(stage)
            stage["status"] = ("VERIFYING" if stage["observations"][-1]["acceptance_matched"]
                               else "RECOVERING")
        return self._change(sid, "STAGE_ACTION_OBSERVED", apply)

    def commit_if_independently_verified(self, sid: str) -> dict | None:
        """Skip a redundant Planner StageCommit when Runtime has all proof.

        This is an optimization of the *existing* commit() verifier, not a
        Planner-completion claim or a new acceptance predicate. Every selected
        receipt must be a persisted/read-back MIN-EV with its predeclared
        Expected matching actual Observed. Unverified, absent-Expected and
        failed-verification actions cannot be auto-committed.
        """
        stage = self.current()
        if stage is None or stage["stage_id"] != sid:
            return None
        self._reconcile_bindings(stage)
        needed = set(range(len(stage["acceptance"])))
        selected = []
        covered = set()
        for item in stage["observations"]:
            if (item.get("expected_matched") is not True
                    or item.get("evidence_store_verified") is not True):
                continue
            newly_covered = set(item.get("acceptance_matched") or []) & needed
            if not newly_covered - covered:
                continue
            selected.append(item["evidence_ref"])
            covered.update(newly_covered)
            if covered == needed:
                break
        if not selected or covered != needed:
            return None
        # commit() supplies immutable plan binding, observation hash,
        # accepted_check_ids, and the same Stage-frozen Event/Hash semantics.
        return self.commit(sid, evidence_refs=selected)

    def recover(self, sid: str, *, reason_code: str, strategy_id: str) -> dict:
        def apply(stage):
            stage["strategy_revision"] += 1
            stage["status"] = "RECOVERING"
            stage["failures"].append({
                "reason_code": str(reason_code)[:120],
                "strategy_id": str(strategy_id)[:120],
                "revision": stage["strategy_revision"],
            })
        return self._change(sid, "STAGE_LOCAL_REPLAN", apply)

    def commit(self, sid: str, *, evidence_refs: list[str]) -> dict:
        def apply(stage):
            self._reconcile_bindings(stage)
            observed = {row["evidence_ref"]: row for row in stage["observations"]}
            selected = [observed[ref] for ref in evidence_refs if ref in observed]
            if not selected or len(selected) != len(set(evidence_refs)):
                raise ValueError("goal75_stage_commit_evidence_unbound")
            if len(selected) != len(evidence_refs):
                raise ValueError("goal75_stage_commit_evidence_missing")
            covered = {index for row in selected for index in row["acceptance_matched"]}
            if covered != set(range(len(stage["acceptance"]))):
                raise ValueError("goal75_stage_acceptance_unverified")
            stage["status"] = "VERIFIED_COMMITTED"
            stage["commit"] = {
                "stage_id": sid,
                "plan_sha256": self.read()["plan_sha256"],
                "evidence_refs": list(evidence_refs),
                "observed_receipt_sha256": [r["receipt_sha256"] for r in selected],
                "accepted_check_ids": sorted(covered),
            }
            if any("binding" in check for check in stage["acceptance"]):
                stage["commit"]["binding_proofs_sha256"] = digest([
                    r.get("binding_proofs", []) for r in selected
                ])
            stage["commit"]["commit_sha256"] = digest(stage["commit"])
        row = self._change(sid, "STAGE_VERIFIED_COMMITTED", apply)
        if all(s["status"] == "VERIFIED_COMMITTED" for s in row["stages"]):
            with exclusive_process_lock(self.path.with_suffix(".lock"), label="goal75-stage"):
                before = self.read()
                after = json.loads(json.dumps(before))
                after["status"] = "STAGES_VERIFIED_GOAL_FINAL_CHECK_REQUIRED"
                return self._save(before, after, "ALL_STAGES_COMMITTED", sid)
        return row

    def readiness(self) -> dict:
        row = self.read()
        if not row:
            return {"status": "NO_STAGE_PLAN"}
        stage = self.current()
        return {
            "status": row["status"],
            "plan_sha256": row["plan_sha256"],
            "stage_count": len(row["stages"]),
            "committed_count": sum(
                s["status"] == "VERIFIED_COMMITTED" for s in row["stages"]
            ),
            "current_stage_id": stage["stage_id"] if stage else "",
            "current_description": stage["description"] if stage else "",
            "current_acceptance": stage["acceptance"] if stage else [],
            "current_unproven_acceptance": [
                {
                    "check_index": index,
                    "field": check["field"],
                    "unobserved_receipt_count": sum(
                        index in item.get("acceptance_unobserved", [])
                        for item in stage["observations"]
                    ),
                    "receipt_count": len(stage["observations"]),
                }
                for index, check in enumerate(stage["acceptance"])
                if not any(index in item.get("acceptance_matched", [])
                           for item in stage["observations"])
            ] if stage else [],
            "current_pre_mutation_binding_missing": [
                i for i, check in enumerate(stage["acceptance"])
                if check.get("binding", {}).get("before_mutation") is True
                and not any(i in item.get("acceptance_matched", [])
                            for item in stage["observations"])
            ] if stage else [],
            "current_evidence_refs": [
                x["evidence_ref"] for x in stage["observations"]
            ] if stage else [],
            "frozen_stage_ids": [
                x["stage_id"] for x in row["stages"]
                if x["status"] == "VERIFIED_COMMITTED"
            ],
        }
