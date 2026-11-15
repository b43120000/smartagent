"""Git63 Global Managed Toolchains: evidence, catalog and no-approval semantics."""
import json
import hashlib
from pathlib import Path
import pytest
from agent_core.managed_toolchains import (
    CATALOG_SCHEMA, RECEIPT_SCHEMA, read_catalog, select_package,
    register_install_receipt, managed_hints, register_external_hint, validate_source,
)
from agent_core import runtime_environment
from agent_core.tool_capabilities import get_allowed_tools
from agent_core.smartagent_protocol import TOOL_ENVELOPE_SCHEMAS


def _catalog(root):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    record = {"tool": "jdk", "version": "17", "archive_type": "zip",
              "archive_sha256": "b"*64,
              "url": "https://github.com/vendor/verified/archive.zip",
              "executables": {"java": "jdk-17/bin/java.exe"}}
    (cfg/"managed_toolchains_catalog.json").write_text(json.dumps(
        {"schema": CATALOG_SCHEMA, "packages": [record]}))
    return record


def test_catalog_is_pinned_and_rejects_unsafe_paths(tmp_path):
    row = _catalog(tmp_path)
    assert select_package("jdk", "17", tmp_path) == row
    with pytest.raises(ValueError, match="CATALOG"):
        select_package("jdk", "18", tmp_path)
    row["url"] = "https://untrusted.example/jdk.zip"
    (tmp_path/"config"/"managed_toolchains_catalog.json").write_text(
        json.dumps({"schema": CATALOG_SCHEMA, "packages": [row]}))
    with pytest.raises(ValueError, match="URL_UNTRUSTED"):
        read_catalog(tmp_path)
    row["url"] = "https://github.com/vendor/tool.zip"
    row["executables"] = {"java": "../../evil.exe"}
    (tmp_path/"config"/"managed_toolchains_catalog.json").write_text(
        json.dumps({"schema": CATALOG_SCHEMA, "packages": [row]}))
    with pytest.raises(ValueError, match="EXECUTABLE_INVALID"):
        read_catalog(tmp_path)


def test_refresh_never_erases_previous_verified_install(monkeypatch, tmp_path):
    path = tmp_path/"localdata"/"metadata"/"environment_ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": runtime_environment.ENVIRONMENT_LEDGER_SCHEMA,
        "tools":{}, "managed_tools":{"java:17":{"alias":"java"}},
        "external_tool_hints":{"jdk":{"execution_authorized":False}}}))
    monkeypatch.setattr(runtime_environment, "_probe_tool", lambda *args: {"status":"MISSING"})
    result=runtime_environment.refresh_environment_ledger(tmp_path)
    assert "java:17" in result["managed_tools"]
    assert "jdk" in result["external_tool_hints"]
    assert "python" in result["tools"] and "git" in result["tools"]


def test_native_os_receipt_required_and_hash_verified(tmp_path):
    _catalog(tmp_path)
    exe=tmp_path/"localdata"/"toolchains"/"jdk"/"17"/"jdk-17"/"bin"/"java.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake-ci-executable")
    receipt={"schema":RECEIPT_SCHEMA, "tool":"jdk", "version":"17",
        "status":"INSTALLED", "archive_sha256":"b"*64,
        "sandbox_backend":"WIN32_APPCONTAINER",
        "sandbox_identity_verified":True, "kernel_outside_write_denied":True}
    with pytest.raises(ValueError, match="ATTESTATION"):
        register_install_receipt({**receipt,"sandbox_identity_verified":False},root=tmp_path)
    registered=register_install_receipt(receipt,root=tmp_path)
    assert registered["status"]=="RECORDED_REPROBE_REQUIRED"
    ledger=json.loads((tmp_path/"localdata"/"metadata"/"environment_ledger.json").read_text())
    hints=managed_hints(ledger,tmp_path)
    assert len(hints)==1 and hints[0]["execution_authorized"] is False
    assert hints[0]["path"]==str(exe)
    exe.write_bytes(b"modified")
    assert managed_hints(ledger,tmp_path)==[]


def test_legacy_external_registry_is_hint_only(tmp_path):
    register_external_hint({"name":"java.exe","path":str(tmp_path/"java.exe"),
        "last_evidence_ref":"MIN-EV-EXAMPLE"},tmp_path)
    doc=json.loads((tmp_path/"localdata"/"metadata"/"environment_ledger.json").read_text())
    rec=next(iter(doc["external_tool_hints"].values()))
    assert rec["execution_authorized"] is False


def test_minimal_core_and_web_agent_expose_typed_installer():
    assert "install_toolchain" in get_allowed_tools("local")
    assert "install_toolchain" in get_allowed_tools("remote")
    assert TOOL_ENVELOPE_SCHEMAS["install_toolchain"]["required"]=={"name":str,"version":str}


def test_unconfigured_catalog_cannot_trigger_untrusted_host_command(monkeypatch,tmp_path):
    _catalog(tmp_path)
    from agent_core import managed_toolchain_install as installer
    monkeypatch.setattr(installer, "install_root", lambda ignored: tmp_path)
    result=json.loads(installer.install_toolchain_action({"name":"unknown","version":"123"},root=tmp_path))
    # Format may be encoded with a Runtime envelope; inspect the text for
    # the deterministic catalog rejection, never accept arbitrary URLs.
    assert "GIT63_TOOL_NOT_IN_TRUSTED_CATALOG" in json.dumps(result)

def test_repository_catalog_does_not_preinstall_jdk_or_any_other_tool():
    # Task demand, not installation-time defaults, chooses the tool/version.
    root = Path(__file__).resolve().parents[3]
    assert read_catalog(root)["packages"] == []


def test_planner_can_resolve_dynamic_zip_source_without_hardcoded_jdk(tmp_path):
    _catalog(tmp_path)
    source = {
        "archive_type": "zip", "archive_sha256": "a" * 64,
        "url": "https://services.gradle.org/distributions/tool-8.0.zip",
        "checksum_url": "https://services.gradle.org/distributions/tool-8.0.zip.sha256",
        "executables": {"gradle": "tool-8.0/bin/gradle.bat"},
    }
    pkg = select_package("gradle", "8.0", tmp_path, source=source)
    assert pkg["tool"] == "gradle"
    assert pkg["executables"]["gradle"].endswith(".bat")
    assert TOOL_ENVELOPE_SCHEMAS["install_toolchain"]["optional"]["source"] is dict
    with pytest.raises(ValueError, match="CHECKSUM_ORIGIN"):
        validate_source("gradle", "8.0", {
            **source, "checksum_url": "https://github.com/unknown/checksum.txt"
        })
    with pytest.raises(ValueError, match="EXECUTABLE_PATH"):
        validate_source("gradle", "8.0", {
            **source, "executables": {"gradle": "../../escape.bat"}
        })


def test_dynamic_install_requires_native_checksum_evidence_and_reuse(tmp_path):
    _catalog(tmp_path)
    source = {
        "archive_type": "zip", "archive_sha256": "c" * 64,
        "url": "https://services.gradle.org/distributions/gradle-8.0-bin.zip",
        "checksum_url": "https://services.gradle.org/distributions/gradle-8.0-bin.zip.sha256",
        "executables": {"gradle": "gradle-8.0/bin/gradle.bat"},
    }
    exe = tmp_path / "localdata" / "toolchains" / "gradle" / "8.0" / "gradle-8.0" / "bin" / "gradle.bat"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"dummy-content-for-test")
    receipt = {
        "schema": RECEIPT_SCHEMA, "tool": "gradle", "version": "8.0",
        "status": "INSTALLED", "archive_sha256": "c" * 64,
        "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_write_denied": True,
    }
    with pytest.raises(ValueError, match="CHECKSUM_ATTESTATION"):
        register_install_receipt(receipt, root=tmp_path, source=source)
    receipt["publisher_checksum_verified"] = True
    register_install_receipt(receipt, root=tmp_path, source=source)
    assert select_package("gradle", "8.0", tmp_path)["url"] == source["url"]
    ledger = json.loads((tmp_path / "localdata" / "metadata" / "environment_ledger.json").read_text())
    assert len(managed_hints(ledger, tmp_path)) == 1
    with pytest.raises(ValueError, match="SOURCE_CONFLICT"):
        register_install_receipt(receipt, root=tmp_path, source={
            **source, "url": "https://services.gradle.org/distributions/evil.zip"
        })
