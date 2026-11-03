"""Per-project, verified environment index using the existing Project Ledger.

Stored within <project>/.agents/project_context; reads never create files.
Records are hints and must be re-probed before executing a tool or claiming
dependency satisfaction. Only a Runtime-attested, authorized recovery may write.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

from .path_security import is_within, reject_reparse_components

SCHEMA = "SMARTAGENT_PROJECT_ENVIRONMENT_V1"
MAX_BYTES = 262144


def environment_index_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / ".agents" / "project_context" / "environment.json"


def read_project_environment(project_root: str | Path) -> dict:
    """Read a bounded candidate index; never mutate a read-only project."""
    path = environment_index_path(project_root)
    empty = {"schema": SCHEMA, "dependencies": {}, "tools": {}}
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return empty
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return empty
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return empty
    dependencies = doc.get("dependencies")
    tools = doc.get("tools")
    if not isinstance(dependencies, dict) or not isinstance(tools, dict):
        return empty
    return {"schema": SCHEMA, "dependencies": dependencies, "tools": tools}


def verified_candidate(project_root: str | Path, distribution: str) -> dict | None:
    """Return recorded provenance, not a fresh verification or authorization."""
    index = read_project_environment(project_root)
    record = index["dependencies"].get(str(distribution).casefold().replace("_", "-"))
    if not isinstance(record, dict) or record.get("status") != "VERIFIED":
        return None
    target = str(record.get("location", ""))
    if not target:
        return None
    try:
        root = Path(project_root).expanduser().resolve()
        location = Path(target).expanduser().resolve()
        # environment.json is only a hint; a tampered absolute path must not
        # redirect the future read-only probe outside the authorized project.
        if not location.is_dir() or not is_within(location, root):
            return None
        reject_reparse_components(location, root)
    except (OSError, ValueError, RuntimeError):
        return None
    return dict(record)


def authorize_verified_environment_write(
    project_root: str | Path, *, tool_context: object,
    authorized_paths: list[str] | tuple[str, ...],
    request_write_ceiling: Mapping | None = None,
) -> tuple[bool, str]:
    """Preflight a Runtime-owned environment.json write; never grant access.

    Original-request paths are read scope only. Persistence requires an
    independent writable ACL and, for a bounded PR-B/C/D request, the exact
    valid metadata ceiling. This gate runs before creating any project file.
    """
    root = Path(project_root).expanduser().resolve()
    destination = environment_index_path(root)
    try:
        declared = {
            Path(raw).expanduser().resolve()
            for raw in authorized_paths if str(raw).strip()
        }
    except (OSError, ValueError, RuntimeError, TypeError):
        return False, "PROJECT_ENVIRONMENT_REQUEST_SCOPE_INVALID"
    if root not in declared and destination not in declared:
        return False, "PROJECT_ENVIRONMENT_NOT_IN_REQUEST_SCOPE"
    if request_write_ceiling is not None:
        ceiling = request_write_ceiling
        if not isinstance(ceiling, Mapping) or not (
            ceiling.get("active") is True and ceiling.get("valid") is True
        ):
            return False, "PROJECT_ENVIRONMENT_WRITE_CEILING_INVALID"
        try:
            ceiling_root = Path(str(ceiling.get("project_root", ""))).resolve()
            ceiling_destination = Path(str(ceiling.get("metadata", ""))).resolve()
        except (OSError, ValueError, RuntimeError):
            return False, "PROJECT_ENVIRONMENT_WRITE_CEILING_INVALID"
        if (ceiling_root != root or ceiling_destination != destination
                or destination not in declared):
            return False, "PROJECT_ENVIRONMENT_WRITE_CEILING_SCOPE_MISMATCH"
    try:
        from .security_context import SecurityContext
        SecurityContext.from_agent(tool_context).require_write(
            destination, require_absolute=True,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # A readable path or model statement must never upgrade an ACL.
        return False, "PROJECT_ENVIRONMENT_WRITE_ACL_DENIED"
    return True, "PROJECT_ENVIRONMENT_WRITE_AUTHORIZED"


def publish_verified_dependency(
    project_root: str | Path, *,
    distribution: str,
    version: str,
    target: str | Path,
    request_id: str,
    action_id: str,
    evidence_ref: str,
    authorized_project_write: bool,
    restricted_metadata_write: bool = False,
) -> dict:
    """Record a confirmed PR-D resume, never infer permission from the ledger.

    The project root must be an authorized writable project for this task, and
    the dependency must have actual Runtime evidence (not Planner narrative).
    """
    if not authorized_project_write:
        raise PermissionError("project_environment_write_not_authorized")
    if not all(str(v).strip() for v in (distribution, version, request_id, action_id, evidence_ref)):
        raise ValueError("project_environment_evidence_required")
    root = Path(project_root).expanduser().resolve()
    location = Path(target).expanduser().resolve()
    if not root.is_dir() or not location.is_dir():
        raise ValueError("project_environment_directory_unavailable")
    if not is_within(location, root):
        # Reusing a pre-existing external SDK is fine, but do not write one
        # into the index via this project-scoped dependency install routine.
        raise ValueError("project_environment_target_outside_project")
    reject_reparse_components(location, root)
    destination = environment_index_path(root)
    reject_reparse_components(destination, root)
    if len(distribution) > 80 or len(version) > 80:
        raise ValueError("project_environment_field_length_invalid")
    from .process_file_lock import exclusive_process_lock
    from .project_ledger import append_project_event
    if restricted_metadata_write:
        # Use Runtime-owned locks; no ancillary files in the user project.
        import hashlib
        from .paths import runtime_root
        lock_root = runtime_root() / "project_environment_locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / (hashlib.sha256(str(destination).casefold().encode("utf-8")).hexdigest() + ".lock")
    else:
        lock_path = destination.with_name("environment.lock")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_process_lock(lock_path, timeout_sec=10.0, label="project environment"):
        doc = read_project_environment(root)
        key = distribution.casefold().replace("_", "-")
        entry = {
            "status": "VERIFIED",
            "version": version,
            "location": str(location),
            "source": "PR_D_VERIFIED_RESUME",
            "request_id": request_id,
            "action_id": action_id,
            "evidence_ref": evidence_ref,
        }
        doc["dependencies"][key] = entry
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("project_environment_index_limit")
        if restricted_metadata_write:
            # Only the authorized environment.json path is written. Do not
            # create project-local temporary/lock/journal files.
            with destination.open("w", encoding="utf-8", newline="\n") as fp:
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
        else:
            handle, temporary = tempfile.mkstemp(prefix="environment.", suffix=".tmp", dir=destination.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fp:
                    fp.write(payload)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    if not restricted_metadata_write:
        append_project_event(
            root, "environment_verified", operation_result="VERIFIED",
            changed_files=[], request_id=request_id,
            details={"distribution": key, "version": version, "location": str(location),
                     "action_id": action_id, "evidence_ref": evidence_ref},
            event_key="environment:" + request_id + ":" + action_id,
        )
    return entry


def publish_verified_tool(
    project_root: str | Path, *, name: str, path: str | Path,
    version: str, request_id: str, evidence_ref: str,
    authorized_project_write: bool,
) -> dict:
    """Persist a proven existing SDK/compiler path, not a new authorization.

    Referenced tools may be outside the project and may be used only if the
    future Task's read/execute policy permits them. The index never grants it.
    """
    if not authorized_project_write:
        raise PermissionError("project_environment_write_not_authorized")
    if not all(str(v).strip() for v in (name, path, version, request_id, evidence_ref)):
        raise ValueError("project_tool_verification_evidence_required")
    if len(name) > 80 or len(version) > 80:
        raise ValueError("project_tool_fields_invalid")
    root = Path(project_root).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    if not root.is_dir() or not candidate.exists():
        raise ValueError("project_tool_path_missing")
    from .process_file_lock import exclusive_process_lock
    from .project_ledger import append_project_event
    destination = environment_index_path(root)
    reject_reparse_components(destination, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_process_lock(destination.with_name("environment.lock"), timeout_sec=10,
                                label="project environment"):
        doc = read_project_environment(root)
        entry = {
            "path": str(candidate), "version": version,
            "status": "VERIFIED", "source": "RUNTIME_TOOL_PROBE",
            "request_id": request_id, "evidence_ref": evidence_ref,
        }
        doc["tools"][name.casefold()] = entry
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("project_environment_index_limit")
        descriptor, temporary = tempfile.mkstemp(prefix="environment.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    append_project_event(
        root, "environment_tool_verified", operation_result="VERIFIED",
        request_id=request_id, changed_files=[],
        details={"tool": name.casefold(), "path": str(candidate),
                 "version": version, "evidence_ref": evidence_ref},
        event_key="tool:" + request_id + ":" + name.casefold(),
    )
    return entry


def planner_environment_hint(project_root: str | Path) -> dict:
    """Bounded, untrusted candidates only; revalidate before any use."""
    doc = read_project_environment(project_root)
    result = {"schema": SCHEMA, "authority": "CANDIDATES_REQUIRING_RUNTIME_REPROBE",
              "dependencies": [], "tools": []}
    for name, row in list(sorted(doc["dependencies"].items()))[:20]:
        if isinstance(row, dict) and row.get("status") == "VERIFIED":
            result["dependencies"].append({
                "name": name, "version": str(row.get("version", ""))[:80],
                "location": str(row.get("location", ""))[:500],
                "last_evidence_ref": str(row.get("evidence_ref", ""))[:200],
            })
    for name, row in list(sorted(doc["tools"].items()))[:20]:
        if isinstance(row, dict) and row.get("status") == "VERIFIED":
            result["tools"].append({
                "name": name, "version": str(row.get("version", ""))[:80],
                "path": str(row.get("path", ""))[:500],
                "last_evidence_ref": str(row.get("evidence_ref", ""))[:200],
            })
    return result


def resolve_install_target(
    project_root: str | Path, *, explicit_target: str | Path | None = None,
    shared_venv: str | Path | None = None, authorize_shared_venv: bool = False,
) -> Path:
    """Policy-only target selection; never performs installations.

    Explicit project-scoped target wins. The shared SmartAgent .venv is used
    only when separately authorized; a project's writable grant is insufficient.
    """
    project = Path(project_root).expanduser().resolve()
    if explicit_target is not None:
        target = Path(explicit_target).expanduser()
        if not target.is_absolute():
            target = project / target
        target = target.resolve()
        if not is_within(target, project):
            raise PermissionError("explicit_dependency_target_outside_project")
        reject_reparse_components(target, project)
        return target
    if shared_venv is not None and authorize_shared_venv:
        path = Path(shared_venv).expanduser().resolve()
        if not path.name.casefold() == ".venv":
            raise ValueError("shared_runtime_venv_target_invalid")
        return path
    raise PermissionError("dependency_install_target_authorization_required")
