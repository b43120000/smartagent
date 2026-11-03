"""PR-B/PR-C/PR-D: bounded request-scoped probes and verified recovery ledger.

Only canonical Runtime Actions can mutate. This module never executes a
remediation, grants a path, imports a package, or creates a success receipt.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
from pathlib import Path
from collections.abc import Mapping, Sequence

from .json_state_io import read_json_retry, write_json_atomic
from .paths import runtime_root
from .process_file_lock import exclusive_process_lock
from .prerequisite_graph_v3 import _require_id
from .runtime_environment import runtime_tool_paths
from .dependency_adapters import parse_requirements, probe_dependency, tool_snapshot_status, classify_remediation

SCHEMA = "SMARTAGENT_PREREQ_V3_BCD_LEDGER_V1"
PHASES = frozenset({
    "NOT_REQUIRED", "PROBED", "MISSING", "REMEDIATING",
    "REMEDIATED_PENDING_VERIFY", "VERIFIED", "RESUMED", "BLOCKED", "UNKNOWN",
})
VERSION = re.compile(r"^(?:[0-9]+)(?:\.[0-9]+){0,3}$")
PACKAGE = re.compile(
    r"(?:\b(?:package|dependency|distribution)\s+|(?:套件|相依套件)\s*)"
    r"([a-zA-Z][a-zA-Z0-9_.-]{0,65})\s*(==|>=|<=)\s*([0-9]+(?:\.[0-9]+){0,3})",
    re.I,
)
# Users frequently specify PEP-508-style version constraints without
# the English prefix "package" (e.g. 確認 smartagent-pr-bcd-live>=1.0 是否缺失).
# The bare constraint is a probe candidate only, never an install grant.
BARE_PACKAGE = re.compile(
    r"(?<![A-Za-z0-9_.-])([a-zA-Z][a-zA-Z0-9_.-]{0,65})\s*(==|>=|<=)\s*([0-9]+(?:\.[0-9]+){0,3})(?![A-Za-z0-9_.-])",
    re.I,
)
TOOL_VERSION = re.compile(r"\b(python|git)\s*(==|>=|<=)\s*([0-9]+(?:\.[0-9]+){0,3})", re.I)
WRITE_INTENT = re.compile(
    r"\b(?:install|setup|repair|remediate|update|modify|write|create)\b|"
    r"安裝|補救|修復|新增|寫入|建立|修改", re.I
)
NEGATED_REMEDIATION = re.compile(
    r"(?:do not|don't|never|must not|without|禁止|不要|請勿|不得|不准|無須|不需要)"
    r"\s*(?:(?:any|to|the|a|任何|自行|執行)\s*)?"
    r"(?:install|setup|repair|remediate|update|modify|write|create|安裝|補救|修復|寫入|建立|修改)",
    re.I,
)


def explicit_remediation_intent(request: str) -> bool:
    """Interpret the *goal's* requested operation, not paths or documentation.

    Paths labelled as installation targets and mentions of the word "write"
    must not become remediation intent. Negative task-wide instructions win
    over positive mentions; narrower prohibitions on other targets retain an
    explicit local install request. Authorization still remains Runtime-owned.
    """
    text = str(request or "")
    scoped = re.compile(
        r"禁止其他專案路徑寫入|"
        r"(?:禁止|不得|不要|請勿)修改\s*SmartAgent\s*共用\s*Python\s*環境",
        re.I,
    )
    # Remove only restrictions on distinctly *other* targets.
    relevant = scoped.sub("", text)
    deny = re.compile(
        r"(?:禁止|不得|不要|請勿|不准|不允許)\s*"
        r"(?:(?:任何|一切|實際|執行|進行|自行|所有)\s*){0,4}"
        r"(?:安裝|補救|修復|寫入|修改|MUTATE|install|repair|remediate|write)|"
        r"不(?:安裝|補救|修復|寫入|修改)|"
        r"\b(?:do\s+not|don't|never|without|must\s+not|no)\s+"
        r"(?:(?:any|to|the)\s+)?(?:install|repair|remediate|write|modify)\b",
        re.I,
    )
    if deny.search(relevant):
        return False
    positive = re.compile(
        r"(?:請|幫我|允許|授權|同意|現在|直接|執行|進行|需要)\s*"
        r"(?:(?:先|立即|進行|執行|使用|離線)\s*){0,3}"
        r"(?:安裝|修復|補救|更新)|"
        r"\b(?:please\s+)?(?:install|repair|remediate|set\s+up)\b"
        r"(?!\s+(?:target|path|directory|location|command|example)\b)",
        re.I,
    )
    return bool(positive.search(relevant))


class PrerequisiteRecoveryError(ValueError):
    pass


def _digest(obj: Mapping[str, object]) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(v) for v in value.split("."))


def _satisfies(found: str, operator: str, required: str) -> bool:
    a, b = _version_tuple(found), _version_tuple(required)
    size = max(len(a), len(b))
    aa = a + (0,) * (size-len(a))
    bb = b + (0,) * (size-len(b))
    return {"==":aa == bb, ">=":aa >= bb, "<=":aa <= bb}[operator]


def _version_in_text(raw: str) -> str:
    m = re.search(r"(?<!\d)(\d+(?:\.\d+){0,3})(?!\d)", raw)
    return m.group(1) if m else ""


def _probe_package(package: str, python_binary: Path) -> tuple[str, str]:
    """No import of the requested module, no pip and no arbitrary command."""
    if not python_binary.is_file():
        return "UNKNOWN", ""
    script = (
        "import importlib.metadata,sys;"
        "name=sys.argv[1];"
        "print(importlib.metadata.version(name))"
    )
    try:
        result = subprocess.run(
            [str(python_binary), "-I", "-c", script, package],
            cwd=str(python_binary.parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN", ""
    if result.returncode == 0:
        version = _version_in_text((result.stdout or "").strip())
        return ("AVAILABLE", version) if version else ("UNKNOWN", "")
    if result.returncode == 1 and "PackageNotFoundError" in (result.stderr or ""):
        return "MISSING", ""
    return "UNKNOWN", ""


def _probe_project_package(package: str, python_binary: Path, target: str) -> tuple[str, str]:
    """Revalidate a cached project-local distribution with isolated Bundled Python."""
    if not python_binary.is_file() or not Path(target).is_dir():
        return "UNKNOWN", ""
    script = (
        "import importlib.metadata,sys;"
        "target=sys.argv[1];name=sys.argv[2];"
        "found=next((d.version for d in importlib.metadata.distributions(path=[target]) "
        "if (d.metadata.get('Name','').lower().replace('_','-')==name.replace('_','-'))),None);"
        "print(found) if found else sys.exit(3)"
    )
    try:
        result = subprocess.run(
            [str(python_binary), "-I", "-B", "-c", script, str(target), package],
            cwd=str(python_binary.parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN", ""
    if result.returncode == 0:
        value = _version_in_text((result.stdout or "").strip())
        return ("AVAILABLE", value) if value else ("UNKNOWN", "")
    return "UNKNOWN", ""


def probe_requirements(*, request: str, authorized_paths: Sequence[str],
                       environment_snapshot: Mapping[str, object],
                       install_root: str | Path,
                       project_root: str | Path | None = None) -> dict:
    """Only trusted request text, scoped paths and Runtime-owned probes.

    Never probe an arbitrary model-provided target; filesystem inspection is
    constrained to exact authorized path identities, not directory traversal.
    """
    text = str(request or "")
    paths = tuple(str(p) for p in authorized_paths if isinstance(p, (str, Path)))
    if len(paths) > 64:
        raise PrerequisiteRecoveryError("pr_b_scope_limit")
    tools = environment_snapshot.get("tools", {})
    if not isinstance(tools, Mapping):
        tools = {}
    requirements = parse_requirements(text)
    requested_tools = tuple(row.name for row in requirements if row.kind == "TOOL"
                            and row.name not in {"python", "git"})
    facts = []
    for name in ("python", "git", *requested_tools):
        entry = tools.get(name, {})
        entry = entry if isinstance(entry, Mapping) else {}
        status = str(entry.get("status", "UNKNOWN")).upper()
        if status not in {"AVAILABLE", "MISSING", "INVALID"}:
            status = "UNKNOWN"
        facts.append({"id":"tool:"+name, "kind":"ENVIRONMENT",
                      "state":"SATISFIED" if status=="AVAILABLE" else
                               "MISSING" if status=="MISSING" else "UNKNOWN",
                      "status":status,
                      "version":_version_in_text(str(entry.get("version", ""))),
                      "source":"RUNTIME_PROBE"})
    for idx, raw in enumerate(paths):
        normalized = Path(raw).expanduser().resolve(strict=False)
        # Report *only* the explicit authorized target; no enumeration.
        exists = normalized.exists()
        facts.append({"id":"path:"+str(idx), "kind":"PATH",
                      "path_sha256":_digest({"path":str(normalized).casefold()}),
                      "state":"SATISFIED" if exists else "MISSING",
                      "status":"EXISTS" if exists else "MISSING",
                      "source":"AUTHORIZED_SCOPE_PROBE"})
    for req in requirements:
        if req.kind != "TOOL":
            continue
        state, observed = tool_snapshot_status(
            req, tools, version_satisfies=_satisfies,
            version_extract=_version_in_text,
        )
        facts.append({"id":req.fact_id, "kind":"VERSION", "state":state,
                      "ecosystem":"tool", "name":req.name,
                      "observed_version":observed,
                      "constraint":req.operator + req.required_version,
                      "source":"RUNTIME_VERSION_PROBE"})
    python_binary = runtime_tool_paths(install_root)["python"]
    from .project_environment import verified_candidate
    for req in requirements:
        if req.kind != "PACKAGE":
            continue
        status, observed, origin = probe_dependency(
            req, python_probe=lambda package: _probe_package(package, python_binary),
        )
        # A bundled distribution can exist yet fail the requested version.
        # Probe a recorded project-local candidate unless the bundled version
        # already satisfies this specific request. A cache entry alone is never
        # proof: a new Runtime metadata probe must confirm the candidate.
        bundled_satisfies = (
            status == "AVAILABLE" and bool(observed)
            and _satisfies(observed, req.operator, req.required_version)
        )
        if req.ecosystem == "python" and not bundled_satisfies and project_root is not None:
            candidate = verified_candidate(project_root, req.name)
            if candidate:
                checked, project_version = _probe_project_package(
                    req.name, python_binary, str(candidate["location"])
                )
                if checked == "AVAILABLE" and project_version:
                    status, observed = checked, project_version
                    origin = "PROJECT_ENVIRONMENT_REPROBED"
        state = ("MISSING" if status == "MISSING" or
                 (status == "AVAILABLE" and not _satisfies(
                     observed, req.operator, req.required_version))
                 else "SATISFIED" if status == "AVAILABLE" else "UNKNOWN")
        facts.append({"id":req.fact_id, "kind":"PACKAGE", "state":state,
                      "ecosystem":req.ecosystem, "name":req.name,
                      "observed_version":observed,
                      "constraint":req.operator + req.required_version,
                      "source":origin, "probe_status":status})
    # An install request referring only to "the wheel" is underspecified.
    # Do not mark it PROBED/ready or infer a distribution from task history.
    # Runtime/Planner must inspect the explicitly authorized wheel first.
    if (not any(row["kind"] == "PACKAGE" for row in facts)
            and re.search(r"(?i)\bwheel\b|輪檔|輪子檔", text)
            and (bool(WRITE_INTENT.search(text))
                 or bool(re.search(r"(?i)\bMUTATE\b|使用.{0,100}\bWheel\b", text)))):
        facts.append({"id":"package:unspecified","kind":"PACKAGE",
                      "state":"UNKNOWN","source":"REQUEST_DISTRIBUTION_UNSPECIFIED",
                      "probe_status":"NOT_PROBED"})
    missing = [row["id"] for row in facts if row["state"]=="MISSING"]
    unknown = [row["id"] for row in facts if row["state"]=="UNKNOWN"]
    # Only a positive, explicit remediation request may enter PR-C.
    # A missing *output* file is not automatically an environment
    # prerequisite. PR-C targets missing runtime/dependency capabilities;
    # ordinary output writes keep their existing V9 action/receipt path.
    dependency_missing = any(
        item.startswith(("tool:","version:","package:","dependency:")) for item in missing
    )
    # Intent survives UNKNOWN probes; eligibility is checked separately by PR-C.
    # Intent belongs to the requested goal, not incidental path descriptions.
    positive_intent = explicit_remediation_intent(text)
    # Ordinary output writes are not PR-C dependency remediation.
    has_dependency_requirement = any(row.get("kind") in {"PACKAGE", "VERSION"} for row in facts)
    remediation_requested = bool(positive_intent and paths and has_dependency_requirement)
    return {"schema":"SMARTAGENT_PREREQ_V3_PROBE_V1",
            "facts":facts, "missing":missing,"unknown":unknown,
            "remediation_requested":remediation_requested,
            "remediation_eligible":bool(remediation_requested and dependency_missing),
            "project_environment_required":bool(
                remediation_requested and project_root is not None and
                any(row.get("ecosystem") == "python" and row.get("kind") == "PACKAGE"
                    and row.get("state") == "MISSING" for row in facts)
            ),
            "authorization_granted":False,"verification_complete":False,
            "executed":False}


def _validate(doc: dict) -> None:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise PrerequisiteRecoveryError("pr_v3_schema_invalid")
    identity = doc.get("identity", {})
    if not isinstance(identity, dict):
        raise PrerequisiteRecoveryError("pr_v3_identity_invalid")
    for key in ("task_id","request_id","task_epoch"):
        _require_id(identity.get(key), key)
    if not isinstance(identity.get("intent_sha256"), str) or not re.fullmatch(
        r"[a-f0-9]{64}", identity["intent_sha256"]
    ):
        raise PrerequisiteRecoveryError("pr_v3_intent_invalid")
    if doc.get("phase") not in PHASES:
        raise PrerequisiteRecoveryError("pr_v3_phase_invalid")
    events = doc.get("events")
    if not isinstance(events, list) or len(events)>2048:
        raise PrerequisiteRecoveryError("pr_v3_events_invalid")
    if len(events) != doc.get("revision") or any(
        not isinstance(e,dict) or e.get("seq")!=i for i,e in enumerate(events,1)
    ):
        raise PrerequisiteRecoveryError("pr_v3_revision_invalid")
    if doc.get("digest") != _digest({k:v for k,v in doc.items() if k!="digest"}):
        raise PrerequisiteRecoveryError("pr_v3_digest_invalid")


def _seal(doc: dict) -> dict:
    doc["revision"] = len(doc["events"])
    doc["digest"] = _digest({k:v for k,v in doc.items() if k!="digest"})
    _validate(doc)
    return doc


class RecoveryLedger:
    def __init__(self, root: str | Path):
        self.path_root = runtime_root(root) / "prerequisite_v3_recovery"

    def _path(self, task_id: str) -> Path:
        return self.path_root / (_require_id(task_id,"task_id")+".json")

    def read(self, task_id: str) -> dict | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        doc = read_json_retry(path)
        _validate(doc)
        if doc["identity"]["task_id"] != task_id:
            raise PrerequisiteRecoveryError("pr_v3_cross_task")
        return doc

    def initialize(self, *, task_id: str, request_id: str, task_epoch: str,
                   intent_sha256: str, probe: Mapping[str, object],
                   graph_digest: str) -> dict:
        identity = {"task_id":_require_id(task_id,"task_id"),
                    "request_id":_require_id(request_id,"request_id"),
                    "task_epoch":_require_id(task_epoch,"task_epoch"),
                    "intent_sha256":intent_sha256}
        if not isinstance(probe,Mapping) or probe.get("schema")!="SMARTAGENT_PREREQ_V3_PROBE_V1":
            raise PrerequisiteRecoveryError("pr_b_probe_required")
        facts = list(probe["facts"])
        if len(facts)>80:
            raise PrerequisiteRecoveryError("pr_b_probe_limit")
        phase = "MISSING" if probe["missing"] else (
            "UNKNOWN" if probe["unknown"] else "PROBED")
        new = {"schema":SCHEMA,"identity":identity,"graph_digest":graph_digest,
               "phase":phase,"initial_facts":facts,
               "current_facts":[dict(row) for row in facts],
               "project_environment_required":bool(probe.get("project_environment_required")),
               "project_environment_verified":False,
               "project_environment_evidence_sha256":"",
               "missing_ids":list(probe["missing"]),"unknown_ids":list(probe["unknown"]),
               "remediation_requested":bool(probe["remediation_requested"]),
               "remediation_action":"","verification_action":"","resumed_action":"",
               "remediation_evidence_sha256":"","verification_evidence_sha256":"",
               "resume_evidence_sha256":"","revision":0,
               "events":[{"seq":1,"type":"PROBED","state":phase}]}
        _seal(new)
        path = self._path(task_id)
        with exclusive_process_lock(path.with_suffix(".lock"),timeout_sec=10,
                                    label="PR-B/C/D ledger",legacy_kind="smartagent-prbcd-lock-v1"):
            old = self.read(task_id)
            if old is not None:
                if old["identity"] != identity:
                    raise PrerequisiteRecoveryError("pr_v3_identity_mismatch")
                return old
            write_json_atomic(path,new)
            return new

    def _transition(self, *, task_id: str, request_id: str, task_epoch: str, fn):
        path = self._path(task_id)
        with exclusive_process_lock(path.with_suffix(".lock"),timeout_sec=10,
                                    label="PR-B/C/D ledger",legacy_kind="smartagent-prbcd-lock-v1"):
            doc = self.read(task_id)
            if doc is None:
                raise PrerequisiteRecoveryError("pr_v3_not_initialized")
            if (doc["identity"]["request_id"] != request_id
                    or doc["identity"]["task_epoch"] != task_epoch):
                raise PrerequisiteRecoveryError("pr_v3_request_epoch_mismatch")
            changed = fn(doc)
            if changed:
                if len(doc["events"]) >= 2048:
                    raise PrerequisiteRecoveryError("pr_v3_event_budget")
                doc["events"].append({
                    "seq":len(doc["events"])+1,"type":changed,"state":doc["phase"]
                })
                _seal(doc)
                write_json_atomic(path,doc)
            return doc

    def before_action(self, *, task_id: str, request_id: str, task_epoch: str,
                      action: Mapping[str, object], scope_allowed: bool) -> dict:
        """Record intent; no permission is granted and no tool is executed."""
        action_id = _require_id(str(action.get("action_id", "")), "action_id")
        tool, operation = str(action.get("tool","")),str(action.get("operation","")).upper()
        def transition(doc):
            if not (tool=="run_command" and operation=="MUTATE"):
                return ""
            command = str(action.get("command", "") or "")
            # Only dependency installation/remediation commands belong to PR-C;
            # unrelated authorized file edits remain in the V9 security lane.
            adapter = classify_remediation(command)
            if adapter is None:
                return ""
            if adapter != "python":
                if doc["remediation_requested"]:
                    raise PrerequisiteRecoveryError("pr_c_unsupported_dependency_adapter")
                return ""
            if doc["phase"] not in {"MISSING","REMEDIATING","REMEDIATED_PENDING_VERIFY","VERIFIED","BLOCKED"}:
                raise PrerequisiteRecoveryError("pr_c_missing_probe_required")
            if doc["phase"] in {"REMEDIATING","REMEDIATED_PENDING_VERIFY",
                                "VERIFIED","BLOCKED"} and doc["remediation_action"]:
                raise PrerequisiteRecoveryError("pr_c_remediation_replay_blocked")
            if doc["phase"] != "MISSING":
                raise PrerequisiteRecoveryError("pr_c_remediation_not_in_missing_phase")
            if not any(item.startswith(("tool:","version:","package:"))
                       for item in doc["missing_ids"]):
                raise PrerequisiteRecoveryError("pr_c_dependency_missing_not_proven")
            if not doc["remediation_requested"] or not scope_allowed:
                raise PrerequisiteRecoveryError("pr_c_explicit_authorization_required")
            if not doc["missing_ids"]:
                raise PrerequisiteRecoveryError("pr_c_missing_probe_required")
            if doc["remediation_action"] and doc["remediation_action"] != action_id:
                raise PrerequisiteRecoveryError("pr_c_remediation_replay_blocked")
            doc["remediation_action"] = action_id
            doc["phase"] = "REMEDIATING"
            return "REMEDIATION_ADMITTED"
        return self._transition(task_id=task_id,request_id=request_id,
                                task_epoch=task_epoch,fn=transition)

    def action_result(self, *, task_id: str, request_id: str, task_epoch: str,
                      action: Mapping[str, object], result: Mapping[str, object],
                      evidence_ref: str) -> dict:
        """Transitions require actual Runtime-owned result and verification."""
        action_id = _require_id(str(action.get("action_id","")), "action_id")
        operation = str(action.get("operation","")).upper()
        tool = str(action.get("tool",""))
        executed = result.get("executed") is True
        if not isinstance(evidence_ref,str) or not evidence_ref or len(evidence_ref)>256:
            raise PrerequisiteRecoveryError("pr_d_runtime_evidence_id_required")
        status = str(result.get("result_status","")).upper()
        verified = str(result.get("effective_verification_status","")).upper()=="PASS"
        evidence = _digest({"ref":evidence_ref}) if evidence_ref else ""
        def transition(doc):
            phase = doc["phase"]
            if phase=="REMEDIATING" and doc["remediation_action"]==action_id:
                if executed and status=="SUCCEEDED":
                    doc["remediation_evidence_sha256"]=evidence
                    doc["phase"]="REMEDIATED_PENDING_VERIFY"
                    return "REMEDIATION_RESULT"
                doc["phase"]="BLOCKED"
                return "REMEDIATION_FAILED_OR_UNKNOWN"
            if phase=="REMEDIATED_PENDING_VERIFY" and tool=="run_command" and operation=="VERIFY":
                # Only independent action with an explicit backlink qualifies.
                if (action_id==doc["remediation_action"] or
                        str(action.get("verifies_action_id","")) != doc["remediation_action"]):
                    return ""
                doc["verification_action"]=action_id
                if executed and status=="SUCCEEDED" and verified:
                    doc["verification_evidence_sha256"]=evidence
                    doc["phase"]="VERIFIED"
                    return "INDEPENDENT_VERIFY_PASS"
                doc["phase"]="BLOCKED"
                return "INDEPENDENT_VERIFY_FAILED"
            if phase=="VERIFIED" and action_id not in {
                doc["remediation_action"],doc["verification_action"]
            } and operation not in {"MUTATE","VERIFY"}:
                # A random later PASS must not masquerade as the resumed
                # original operation. The retry must explicitly depend on
                # the independent verifier in the accepted V9 action DAG.
                dependencies = action.get("depends_on") or []
                linked = (isinstance(dependencies,list) and
                          doc["verification_action"] in dependencies)
                if linked and executed and status=="SUCCEEDED" and verified:
                    doc["resumed_action"]=action_id
                    doc["resume_evidence_sha256"]=evidence
                    doc["phase"]="RESUMED"
                    return "ORIGINAL_ACTION_RESUMED"
            return ""
        return self._transition(task_id=task_id,request_id=request_id,
                                task_epoch=task_epoch,fn=transition)

    def record_verified_environment(
        self, *, task_id: str, request_id: str, task_epoch: str,
        distribution: str, version: str, evidence_ref: str,
    ) -> dict:
        """Update current facts only after independent re-probe and publication."""
        package_id = "package:" + distribution.casefold().replace("_", "-")
        evidence_digest = _digest({"ref":evidence_ref})
        def transition(doc):
            if doc["phase"] != "RESUMED":
                raise PrerequisiteRecoveryError("pr_d_resume_required")
            if evidence_digest != doc["resume_evidence_sha256"]:
                raise PrerequisiteRecoveryError("pr_d_resume_evidence_mismatch")
            original = next(
                (row for row in doc["initial_facts"]
                 if str(row.get("id","")).casefold().replace("_", "-") == package_id
                 and row.get("state") == "MISSING"), None,
            )
            if not original:
                raise PrerequisiteRecoveryError("pr_d_missing_package_fact_required")
            constraint = str(original.get("constraint", ""))
            match = re.fullmatch(r"(==|>=|<=)([0-9]+(?:\.[0-9]+){0,3})", constraint)
            if not match or not _satisfies(version, match.group(1), match.group(2)):
                raise PrerequisiteRecoveryError("pr_d_installed_version_unsatisfied")
            if doc.get("project_environment_verified"):
                if doc.get("project_environment_evidence_sha256") != evidence_digest:
                    raise PrerequisiteRecoveryError("pr_d_environment_replay_mismatch")
                return ""
            doc["current_facts"] = [
                {**row, "state":"SATISFIED", "observed_version":version,
                 "source":"PROJECT_ENVIRONMENT_REPROBED"}
                if str(row.get("id","")).casefold().replace("_", "-") == package_id
                else row
                for row in doc["current_facts"]
            ]
            doc["project_environment_verified"] = True
            doc["project_environment_evidence_sha256"] = evidence_digest
            return "CURRENT_ENVIRONMENT_VERIFIED"
        return self._transition(
            task_id=task_id, request_id=request_id, task_epoch=task_epoch,
            fn=transition,
        )

    def terminal_ready(self, task_id: str) -> tuple[bool, str]:
        doc = self.read(task_id)
        if not doc:
            return True,"NO_PR_V3_LEDGER"
        # Existing V9 admission and TaskResult remain the only success gates.
        if doc["remediation_action"] and doc["phase"]!="RESUMED":
            return False,"PR_D_INDEPENDENT_VERIFY_OR_RESUME_REQUIRED"
        if (doc["remediation_action"] and doc.get("project_environment_required")
                and not doc.get("project_environment_verified")):
            return False, "PR_D_PROJECT_ENVIRONMENT_VERIFICATION_REQUIRED"
        return True,"PR_D_RECOVERY_RESUMED" if doc["phase"]=="RESUMED" else "PR_D_NO_REMEDIATION"


def summary(doc: Mapping[str, object]) -> dict:
    _validate(dict(doc))
    return {"schema":SCHEMA,"phase":doc["phase"],"revision":doc["revision"],
            "task_id":doc["identity"]["task_id"],"digest":doc["digest"],
            "missing_count":len(doc["missing_ids"]),
            "unknown_count":len(doc["unknown_ids"]),
            "remediation_action":doc["remediation_action"],
            "verification_action":doc["verification_action"],
            "resumed_action":doc["resumed_action"],
            "authorization_granted":False,
            "verified":doc["phase"] in {"VERIFIED","RESUMED"},
            "task_result_issued":False}
