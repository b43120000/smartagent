"""Bounded read-only software inventory for Planner decisions.

Probed bundled Git/Python are CURRENT_AVAILABLE only after an actual binary
probe. Extra .venv executables and ProjectSync records are leads requiring
a fresh version/functionality check and ordinary execution admission.
No inventory row grants download, installation, network or filesystem rights.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .paths import install_root as canonical_install_root

SCHEMA = "SMARTAGENT_RUNTIME_SOFTWARE_INVENTORY_V1"
MAX_BUNDLED_EXTRA = 24
MAX_PROJECT_ITEMS = 16


def build_runtime_software_inventory(
    environment: Mapping | None,
    project_index: Mapping | None = None,
    *, installation_root: str | Path | None = None,
) -> dict:
    package_root = canonical_install_root(installation_root)
    venv = package_root / ".venv"
    env = environment if isinstance(environment, Mapping) else {}
    source_tools = env.get("tools") if isinstance(env.get("tools"), Mapping) else {}
    bundled = []
    for name in ("git", "python"):
        row = source_tools.get(name) if isinstance(source_tools.get(name), Mapping) else {}
        status = str(row.get("status") or "UNKNOWN")
        if status not in {"AVAILABLE", "MISSING", "INVALID"}:
            status = "UNKNOWN"
        # Only the Runtime's binary probe is current evidence.
        bundled.append({
            "name": name, "source": "BUNDLED_VENV",
            "availability": "CURRENT_AVAILABLE" if status == "AVAILABLE"
                            else "UNKNOWN" if status == "UNKNOWN"
                            else status,
            "probe_status": status,
            "executable": str(row.get("path") or "")[:1024],
            "version": str(row.get("version") or "")[:200],
            "probe_exit_code": row.get("probe_exit_code"),
            "execution_authorized": False,
        })
    # Enumerate only shallow executable *names* from the Agent-owned .venv;
    # these are candidates, not successful probes or executable permission.
    extras = []
    if venv.is_dir() and not venv.is_symlink():
        for relative in (Path("Scripts"), Path("bin"), Path("git") / "cmd"):
            directory = venv / relative
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
                for entry in entries:
                    if len(extras) >= MAX_BUNDLED_EXTRA:
                        break
                    if not entry.is_file() or entry.is_symlink():
                        continue
                    if entry.suffix.casefold() not in {".exe", ".cmd", ".bat"} and (
                            directory.name == "Scripts" or directory.name == "cmd"):
                        continue
                    if entry.name.casefold() in {"python.exe", "git.exe"}:
                        continue
                    extras.append({
                        "name": entry.name[:120],
                        "location": str(entry)[:1024],
                        "source": "BUNDLED_VENV",
                        "availability": "PRESENT_REPROBE_REQUIRED",
                        "execution_authorized": False,
                    })
            except OSError:
                continue
            if len(extras) >= MAX_BUNDLED_EXTRA:
                break
    index = project_index if isinstance(project_index, Mapping) else {}
    index_status = str(index.get("index_status") or "NOT_OBSERVED")
    tools = []
    dependencies = []
    if (index.get("authority") == "CANDIDATES_REQUIRING_RUNTIME_REPROBE"
            and index_status == "PRESENT"):
        for source, target, locfield in (
            (index.get("tools"), tools, "path"),
            (index.get("dependencies"), dependencies, "location"),
        ):
            if not isinstance(source, list):
                continue
            for item in source[:MAX_PROJECT_ITEMS]:
                if not isinstance(item, Mapping):
                    continue
                target.append({
                    "name": str(item.get("name") or "")[:80],
                    "version_recorded": str(item.get("version") or "")[:80],
                    "location_hint": str(item.get(locfield) or "")[:500],
                    "prior_evidence_ref": str(item.get("last_evidence_ref") or "")[:160],
                    "availability": "RECORDED_REPROBE_REQUIRED",
                    "execution_authorized": False,
                })
    return {
        "schema": SCHEMA,
        "installation_source": "CANONICAL_INSTALLATION_ROOT",
        "bundled_environment_path": str(venv),
        "bundled_tool_probes": bundled,
        "bundled_other_executable_candidates": extras,
        "project_environment_index_status": index_status,
        "project_recorded_tool_candidates": tools,
        "project_recorded_dependency_candidates": dependencies,
        "selection_policy": {
            "use_existing_verified_tool_first": True,
            "explicit_user_path_overrides_default": True,
            "bundled_default_location": str(venv),
            "project_specific_install_requires_normal_target_admission": True,
            "shared_venv_mutation_requires_separate_authorization": True,
            "recorded_project_candidate_must_be_reprobed": True,
        },
        "inventory_grants_installation_or_network_authorization": False,
    }


__all__ = ["SCHEMA", "build_runtime_software_inventory"]
