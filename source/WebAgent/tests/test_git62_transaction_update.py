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
