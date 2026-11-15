"""Git63 regression: PowerShell provider PWD must equal the admitted OS CWD.

Static contract tests run on CI without Windows admin rights. The actual
AppContainer CreateProcess / native token result still requires Windows Live.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "install_smart_agent" / "turbo_appcontainer_probe.cs"
BROKER = ROOT / "install_smart_agent" / "turbo_os_execute.ps1"


def test_native_launcher_sets_powershell_location_before_user_script():
    source = NATIVE.read_text(encoding="utf-8")
    start = source.index("string safeCwd = System.IO.Path.GetFullPath(cwd)")
    end = source.index("string encoded = Convert.ToBase64String", start)
    native_wrapper = source[start:end]
    assert "Replace(\"'\", \"''\")" in native_wrapper
    assert "Set-Location -LiteralPath '" in native_wrapper
    assert "(Get-Location).Path" in native_wrapper
    assert "[Environment]::CurrentDirectory" in native_wrapper
    assert "GIT63_NATIVE_POWERSHELL_CWD_MISMATCH" in native_wrapper
    assert native_wrapper.index("Set-Location -LiteralPath") < native_wrapper.index("& {\\n")
    assert "try { \" + bindPowerShellLocation" in native_wrapper
    assert "-ErrorAction Stop" in native_wrapper


def test_native_launcher_uses_exact_broker_cwd_for_both_process_and_shell():
    source = NATIVE.read_text(encoding="utf-8")
    broker = BROKER.read_text(encoding="utf-8")
    # Do not replace the exact authorized CWD with installation or workspace root.
    assert "environment, cwd, ref si, out pi" in source
    assert "string safeCwd = System.IO.Path.GetFullPath(cwd)" in source
    assert "string raw = LaunchPowerShell(moniker, wrapped, root," in source
    assert "$moniker,$nativeScript,$state.cwd," in broker
    assert source.count("Set-Location -LiteralPath '") == 1
    assert source.count("LaunchWithTrustedPath(moniker, executable,") >= 1


def test_native_cwd_fix_does_not_spawn_host_fallback_or_mutate_host_pwd():
    source = NATIVE.read_text(encoding="utf-8")
    start = source.index("public static string LaunchPowerShell(string moniker, string script,")
    stop = source.index("public static string LaunchPowerShellCaptured(", start)
    function = source[start:stop]
    assert "Set-Location -LiteralPath '" in function
    assert "Encoding.Unicode.GetBytes(body)" in function
    assert "-NoProfile -NonInteractive -EncodedCommand " in function
    assert "LaunchWithTrustedPath(" in function
    assert "Process.Start(" not in function
    assert "Environment.CurrentDirectory =" not in function
