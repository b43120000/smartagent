"""Fail-closed wiring contract for explicit protected Turbo import recovery."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_force_stop_does_not_recover_on_normal_restart():
    bat = (ROOT / "force_stop_all_agents.bat").read_text(encoding="utf-8")
    assert '--recover-turbo-import' in bat
    assert 'if defined RECOVER_TURBO_ROOT' in bat
    assert 'if not "%STOP_EXIT%"=="0"' in bat
    assert 'turbo_pending_import_recovery.ps1' in bat
    assert '--require-startup-contract' in bat
    assert '--root "%SMARTAGENT_INSTALL_ROOT%"' in bat
    assert '--root "%~dp0."' not in bat
    assert 'shift\n' not in bat.replace('shift /1', 'SHIFTPRESERVE')
    assert bat.count('shift /1') >= 8
    assert 'set "SMARTAGENT_RECOVERY_SCRIPT=%SMARTAGENT_INSTALL_ROOT%\\install_smart_agent\\turbo_pending_import_recovery.ps1"' in bat
    assert 'if not exist "%SMARTAGENT_RECOVERY_SCRIPT%"' in bat
    assert 'TURBO_RECOVERY_ELEVATED_EXIT_PASS' in bat
    assert 'native success receipt missing' in bat
    assert 'if errorlevel 1' in bat
    assert '%~dp0install_smart_agent\\turbo_pending_import_recovery.ps1' not in bat
    assert 'Recovery BLOCKED: force stop or Git upgrade did not pass.' in bat
    assert "TURBO_PENDING_IMPORT_RECOVERY_PASS" not in bat


def test_native_recovery_requires_machine_and_manifest_before_mutation():
    script = (ROOT / "install_smart_agent/turbo_pending_import_recovery.ps1").read_text(encoding="utf-8")
    required = [
        'Start-Process', '-Verb RunAs',
        'require_protocol_manifest', 'load_machine_authorization',
        'TURBO_RECOVERY_ACTIVATION_ALREADY_EXISTS',
        'TURBO_RECOVERY_JOURNAL_UNTRUSTED',
        'TURBO_RECOVERY_SNAPSHOT_UNTRUSTED',
        '-Action Recover', 'TURBO_RECOVERY_POSTCONDITION_FAILED',
        'TURBO_PENDING_IMPORT_RECOVERY_PASS',
    ]
    assert all(k in script for k in required)
    # Journals are removed only by the native snapshot-based Recover action.
    assert 'Remove-Item -LiteralPath $paths.import' not in script
    assert script.index('load_machine_authorization') < script.index('-Action Recover')
    assert script.index('TURBO_RECOVERY_ACTIVATION_ALREADY_EXISTS') < script.index('-Action Recover')
    assert script.index('TURBO_RECOVERY_SNAPSHOT_UNTRUSTED') < script.index('-Action Recover')
