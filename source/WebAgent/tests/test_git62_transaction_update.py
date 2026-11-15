"""Git62 post-pull update gate regression (no live ACL, GitHub or ProgramData)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

def test_update_bat_entrypoint_uses_explicit_sha_and_no_direct_machine_edits():
    bat=(ROOT/"update.bat").read_text(encoding="utf-8")
    assert "GIT62_TRANSACTION_UPDATE_LAUNCHER_PARSE_OK" in bat
    assert "git62_transaction_update.ps1" in bat
    assert "TARGET_SHA" in bat
    assert "Unknown option. Only --verify is supported." in bat
    assert "machine_authorization.json" not in bat

def test_transaction_validates_source_and_root_before_stopping():
    src=(ROOT/"install_smart_agent/git62_transaction_update.ps1").read_text(encoding="utf-8")
    for keyword in ("_verify_upgrade_commit", "VERIFY_COMMIT_AND_FILES", "NATIVE_ROOT_PRECHECK",
                    "-VerifyOnly", "STOP_AGENTS", "REBIND_MACHINE_AUTHORIZATION",
                    "POSTCHECK_REMOTE", "UPDATE_STATUS=PASS", "UPDATE_STATUS=FAIL"):
        assert keyword in src
    assert src.index("NATIVE_ROOT_PRECHECK") < src.index("STOP_AGENTS")
    assert src.index("STOP_AGENTS") < src.index("REBIND_MACHINE_AUTHORIZATION")
    assert "turbo_multi_root_upgrade_authorization.ps1" in src
    assert "$machine.protocol_manifest_sha256=" not in src  # no blind machine digest assignment
    assert "UPDATE_SINGLE_ROOT_REQUIRES_SEPARATE_VERIFIED_MIGRATION" in src
    assert "UPDATE_PINNED_UPDATER_CHANGED" in src

def test_per_root_precheck_never_writes_journal():
    src=(ROOT/"install_smart_agent/turbo_multi_root_rebind.ps1").read_text(encoding="utf-8")
    assert "[switch]$VerifyOnly" in src
    assert "GIT62_PER_ROOT_PRECHECK_PASS" in src
    assert "GIT62_REBIND_KERNEL_ATTESTATION_FAILED:root=" in src
    assert "GIT62_REBIND_JOURNAL_COUNT_MISMATCH" in src
    assert src.index("if($VerifyOnly)") < src.index("foreach($item in $verified)")

def test_windows_powershell_native_python_c_does_not_depend_on_embedded_quotes():
    """PowerShell 5.1 can strip embedded double quotes from native -c argument strings."""
    import ast
    import re
    source=(ROOT/"install_smart_agent/git62_transaction_update.ps1").read_text(encoding="utf-8")
    validate=re.search(r"(?m)^\s*\$validate='([^']+)'$",source)
    post=re.search(r"(?m)^\s*& \$python -c '([^']+)'$",source)
    assert validate is not None and post is not None
    for statement in (validate.group(1),post.group(1)):
        assert '"' not in statement
        ast.parse(statement)
    assert 'print(UPDATE_VERIFIED_COMMIT_AND_CORE_FILES)' not in source
    assert 'print(UPDATE_MACHINE_AUTHORIZATION_PASS)' not in source


def test_uac_console_pauses_only_after_elevated_worker_completes():
    source=(ROOT/"install_smart_agent/git62_transaction_update.ps1").read_text(encoding="utf-8")
    assert "} finally {" in source
    assert "if($Elevated){" in source
    assert "Read-Host 'Press ENTER to close'" in source
    assert "ELEVATED_RESULT=PASS" in source
    assert source.count("$updateSucceeded=$true") == 2
    assert "ELEVATED_RESULT=FAIL stage=" in source
    assert "Start-Process -FilePath $exe -ArgumentList $arg -Verb RunAs -Wait -PassThru" in source
    assert "exit 20\n} finally {" in source


def test_missing_sentinel_recovery_is_explicit_and_verify_mode_stays_read_only():
    native=(ROOT/"install_smart_agent/turbo_multi_root_rebind.ps1").read_text(encoding="utf-8")
    updater=(ROOT/"install_smart_agent/git62_transaction_update.ps1").read_text(encoding="utf-8")
    assert "[switch]$RepairMissingSentinels" in native
    assert "GIT62_REBIND_REPAIR_AND_VERIFY_CONFLICT" in native
    assert "GIT62_REBIND_PARTIAL_COMMIT_FORBIDDEN" in native
    assert "GIT62_REBIND_SELECTED_JOURNAL_MISSING" in native
    assert "[IO.FileMode]::CreateNew" in native
    assert "GIT62_REBIND_SENTINEL_PARENT_REPARSE" in native
    assert "GIT62_REBIND_SENTINEL_UNTRUSTED" in native
    assert "GIT62_SENTINEL_RECOVERY_PRECHECK_PASS" in native
    assert native.index("GIT62_REBIND_BROKER_HASH_CHANGED") < native.index("[IO.FileMode]::CreateNew")
    assert native.index("GIT62_REBIND_KERNEL_ATTESTATION_FAILED") < native.index("if($RepairMissingSentinels){")
    assert native.index("if($RepairMissingSentinels){") < native.index("foreach($item in $verified)")
    assert "if(-not $VerifyOnly){" in updater
    assert "-RepairMissingSentinels" in updater
    assert updater.index("NATIVE_SENTINEL_RECOVERY") < updater.index("NATIVE_ROOT_PRECHECK")
