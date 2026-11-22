"""Git63 Planner/Runtime Global Toolchain discovery bridge regressions.

A valid inventory read is an explicit path-free action, not an Executor
Blackboard event slice and never implicit host command execution.
"""
import json
from pathlib import Path

from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_capabilities import get_allowed_tools
from agent_core.toolchain_environment_reader import inspect_toolchain_environment
from agent_core.managed_toolchains import register_install_receipt
from agent_core.runtime_environment import ENVIRONMENT_LEDGER_SCHEMA


def _root(tmp_path):
    catalog = tmp_path / "config" / "managed_toolchains_catalog.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(json.dumps({
        "schema": "SMARTAGENT_GIT63_CATALOG_V1", "packages": []
    }), encoding="utf-8")
    return tmp_path


def test_explicit_inventory_tool_admitted_across_web_and_remote():
    request = {"tool": "inspect_toolchain_environment", "action_id": "A1"}
    accepted, diagnostic = validate_tool_envelope(request)
    assert accepted, diagnostic
    assert "inspect_toolchain_environment" in get_allowed_tools("web_direct")
    assert "inspect_toolchain_environment" in get_allowed_tools("remote")
    assert "inspect_toolchain_environment" in get_allowed_tools("local")
    invalid, detail = validate_tool_envelope({
        **request, "path": "C:\\smartagent\\localdata"
    })
    assert not invalid
    assert detail["reason"] == "unexpected_field"


def test_blackboard_rejects_inventory_section_with_correct_tool_guidance():
    accepted, diagnostic = validate_tool_envelope({
        "tool": "read_runtime_blackboard",
        "action_id": "A2", "section": "runtime_software_inventory",
    })
    assert not accepted
    assert diagnostic["reason"] == "runtime_blackboard_read_slice_invalid"
    assert "inspect_toolchain_environment" in diagnostic.get("suggestion", "")
    assert "allowed_sections" in diagnostic.get("detail", "")


def test_read_only_empty_catalog_does_not_claim_tool_missing(tmp_path):
    root = _root(tmp_path)
    ledger_file = root / "localdata" / "metadata" / "environment_ledger.json"
    observation = inspect_toolchain_environment(root=root)
    assert observation["ledger_state"] == "NOT_RECORDED"
    assert observation["catalog_status"] == "VALID"
    assert observation["inventory"]["managed_install_catalog"] == []
    assert observation["inventory"]["global_managed_tool_candidates"] == []
    assert observation["host_command_executed"] is False
    assert observation["installer_invoked"] is False
    assert observation["execution_authorized"] is False
    assert not ledger_file.exists(), "Read must not refresh or write a Ledger"


def test_registered_tool_visible_after_install_without_second_install(tmp_path, monkeypatch):
    root = _root(tmp_path)
    source = {
        "archive_type": "zip", "archive_sha256": "b" * 64,
        "url": "https://services.gradle.org/distributions/gradle-8.0-bin.zip",
        "checksum_url": "https://services.gradle.org/distributions/gradle-8.0-bin.zip.sha256",
        "executables": {"gradle": "gradle-8.0/bin/gradle.bat"}
    }
    exe = (root / "localdata" / "toolchains" / "gradle" / "8.0"
           / "gradle-8.0" / "bin" / "gradle.bat")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"test-only-unexecutable-fixture")
    receipt = {
        "schema": "SMARTAGENT_GIT63_INSTALL_RECEIPT_V1",
        "tool": "gradle", "version": "8.0", "status": "INSTALLED",
        "archive_sha256": "b" * 64, "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_write_denied": True,
        "publisher_checksum_verified": True,
    }
    register_install_receipt(receipt, root=root, source=source)
    ledger_file = root / "localdata" / "metadata" / "environment_ledger.json"
    before = ledger_file.read_bytes()
    def no_execution(*args, **kwargs):
        raise AssertionError("Read must not execute a process")
    monkeypatch.setattr("subprocess.run", no_execution)
    observed = inspect_toolchain_environment(root=root)
    managed = observed["inventory"]["global_managed_tool_candidates"]
    assert len(managed) == 1
    assert managed[0]["name"] == "gradle"
    assert managed[0]["availability"] == "RECORDED_REPROBE_REQUIRED"
    assert managed[0]["execution_authorized"] is False
    assert observed["ledger_state"] == "RECORDED"
    assert ledger_file.read_bytes() == before, "Read must not mutate registration"


def test_inventory_read_is_not_terminal_evidence(tmp_path):
    observation = inspect_toolchain_environment(root=_root(tmp_path))
    assert observation["inventory"]["inventory_grants_installation_or_network_authorization"] is False
    assert observation["inventory"]["managed_install_per_action_confirmation"] is False
    assert observation["inventory"]["dynamic_toolchain_install"] is True
