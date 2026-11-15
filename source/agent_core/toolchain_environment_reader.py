"""Git63: immutable, install-root-bound Global Toolchain inventory read Action.

This is not the Executor event Blackboard. It accepts no Planner path and
never invokes a shell, installer, package manager, or ledger refresh writer.
Installed binaries are hash-checked as *candidates*, never execution grants.
"""
from __future__ import annotations

from pathlib import Path

from .paths import install_root
from .managed_toolchains import managed_hints, read_catalog, store_root
from .runtime_environment import read_environment_ledger
from .runtime_software_inventory import build_runtime_software_inventory


def inspect_toolchain_environment(*, root: str | Path | None = None) -> dict:
    base = install_root(root)
    ledger = read_environment_ledger(base)
    recorded = bool(ledger)
    managed = managed_hints(ledger, base)
    external = ledger.get("external_tool_hints", {})
    external_candidates = []
    if isinstance(external, dict):
        for item in list(external.values())[:64]:
            if not isinstance(item, dict):
                continue
            external_candidates.append({
                "name": str(item.get("name", ""))[:100],
                "path": str(item.get("path", ""))[:1024],
                "availability": "RECORDED_REPROBE_REQUIRED",
                "execution_authorized": False,
            })
    try:
        catalog = [
            {"tool": str(row["tool"]), "version": str(row["version"])}
            for row in read_catalog(base)["packages"][:64]
        ]
        catalog_status = "VALID"
    except (OSError, ValueError, KeyError, TypeError):
        catalog = []
        catalog_status = "INVALID_OR_MISSING"
    fact_set = {
        "tools": ledger.get("tools", {}) if isinstance(ledger.get("tools"), dict) else {},
        "managed_tools": managed[:64],
        "external_tool_hints": external_candidates,
        "install_catalog": catalog,
    }
    inventory = build_runtime_software_inventory(fact_set, installation_root=base)
    # This action does not execute Git/Python: historical successful probes
    # must never be misrepresented as new, request-local execution receipts.
    for item in inventory["bundled_tool_probes"]:
        if item["availability"] == "CURRENT_AVAILABLE":
            item["availability"] = "RECORDED_REPROBE_REQUIRED"
        item["reprobe_required_for_execution"] = True
    return {
        "schema": "SMARTAGENT_GIT63_GLOBAL_TOOLCHAIN_INSPECTION_V1",
        "source": "CANONICAL_INSTALL_ROOT",
        "ledger_state": "RECORDED" if recorded else "NOT_RECORDED",
        "ledger_checked_at": ledger.get("checked_at") if recorded else None,
        "toolchain_root": str(store_root(base)),
        "catalog_status": catalog_status,
        "inventory": inventory,
        "read_only": True,
        "host_command_executed": False,
        "installer_invoked": False,
        "installation_granted": False,
        "execution_authorized": False,
        "next_action": "Resolve missing tools from official sources; use install_toolchain with validated source if missing; re-probe before Execute.",
    }


__all__ = ["inspect_toolchain_environment"]
