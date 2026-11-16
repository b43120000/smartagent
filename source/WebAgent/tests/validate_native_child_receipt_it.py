"""Opt-in Windows integration checks against the installed Native Broker."""
import argparse
import json
from pathlib import Path
import uuid

from agent_core.bounded_process import native_only_runtime_test
from agent_core.runtime_evidence_store import read_runtime_evidence
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext


CMD_PROBE = r"""
$cwd = [Environment]::CurrentDirectory
$cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
$name = '.smartagent-native-it-' + [Guid]::NewGuid().ToString('N') + '.bat'
$file = Join-Path $cwd $name
$body = @(
    '@echo off',
    'cd /d . || exit /b 51',
    'echo BAT_CWD=%CD%',
    'if not exist "%~nx0" exit /b 52',
    '"%SystemRoot%\System32\cmd.exe" /d /c exit /b 37',
    'set "child_rc=%ERRORLEVEL%"',
    'echo CHILD_EXIT=%child_rc%',
    'exit /b %child_rc%'
)
$stream = [IO.File]::Open($file, [IO.FileMode]::CreateNew)
try {
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes(($body -join "`r`n") + "`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
    $info = [Diagnostics.ProcessStartInfo]::new($cmd, "/d /c call $name")
    $info.WorkingDirectory = $cwd
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $child = [Diagnostics.Process]::Start($info)
    try {
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        if (-not $child.WaitForExit(10000)) { throw 'CMD_CHILD_TIMEOUT' }
        Write-Output $stdout.Result.Trim()
        if ($stderr.Result) { throw $stderr.Result }
        $global:LASTEXITCODE = $child.ExitCode
    } finally { $child.Dispose() }
} finally { [IO.File]::Delete($file) }
"""


def run(workspace: Path, cwd: Path) -> dict:
    context = WebAgentToolContext(workspace, interface_name="remote")

    def unexpected_planner(*_args):
        raise RuntimeError("The module IT does not invoke the Planner")

    loop = WebAgentProtocolLoop(workspace, unexpected_planner, tool_context=context,
                                progress_root=Path.cwd())
    run_suffix = uuid.uuid4().hex.upper()
    loop.task_id = context.current_task_id = "TASK-NATIVE-IT-" + run_suffix
    loop.run_id = context.current_request_id = "RR-NATIVE-IT-" + run_suffix
    loop.task_epoch = "EPOCH-" + run_suffix
    loop.authorized_paths = [str(cwd)]

    def dispatch(operation: str, command: str, suffix: str) -> tuple[dict, dict]:
        action_id = "ACT-NATIVE-IT-" + run_suffix + "-" + suffix
        action = {
            "tool": "run_command", "action_id": action_id, "operation": operation,
            "working_directory": str(cwd), "timeout": 40, "command": command,
        }
        with native_only_runtime_test():
            raw = loop._execute_runtime_action(action)
        receipt = decode_result(raw)
        assert receipt and receipt["executed"] is True, receipt
        data = receipt["data"]
        sandbox = data["sandbox_evidence"]
        assert sandbox["sandbox_backend"] == "WIN32_APPCONTAINER"
        assert sandbox["sandbox_identity_verified"] is True
        assert sandbox["kernel_outside_write_denied"] is True
        assert data["execution_id"] == sandbox["action_id"] == action_id
        pointer = loop.action_ledger[action_id]["runtime_evidence_store"]
        assert pointer["store_status"] == "PERSISTED_AND_READBACK_VERIFIED"
        readback = read_runtime_evidence(
            Path.cwd(), task_id=loop.task_id, request_id=loop.run_id,
            task_epoch=loop.task_epoch, evidence_ref=pointer["evidence_ref"],
        )
        assert readback["source_sha256"] == pointer["source_sha256"]
        return receipt, pointer

    child, child_ref = dispatch("TEST", CMD_PROBE, "CHILD")
    assert child["status"] == "FAILED", child
    assert child["data"]["exit_code"] == 37, child
    output = child["data"]["stdout"].splitlines()
    assert output and output[0].casefold() == f"BAT_CWD={cwd}".casefold(), output
    assert "CHILD_EXIT=37" in output, output
    large, large_ref = dispatch("INSPECT", "Write-Output ('x' * 16000)", "RECEIPT")
    assert large["status"] == "SUCCEEDED" and large["data"]["exit_code"] == 0
    assert large["data"]["stdout"] == "x" * 16000
    assert large["data"]["verification_status"] == "UNVERIFIED"
    # A deliberately truncated copy of the real result must not provide
    # execution or goal proof. The original Broker evidence is untouched.
    from agent_core.tool_result_contract import encode_result
    copied = encode_result(
        large["status"], large["reason"], tool="run_command",
        executed=large["executed"], data=large["data"],
    )
    assert decode_result(copied[:-1]) is None
    return {
        "status": "PASS", "task_id": loop.task_id,
        "native_child": child_ref["evidence_ref"],
        "large_receipt": large_ref["evidence_ref"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.workspace.resolve(), args.cwd.resolve())))
