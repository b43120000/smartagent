# Windows CI fixture for non-mutating ACL ON stage diagnosis.
$ErrorActionPreference='Stop'
$tool=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\diagnose_acl_on.ps1'))
$fixture=Join-Path $env:RUNNER_TEMP ('acl-on-fixture-' + [Guid]::NewGuid().ToString('N'))
$root=Join-Path $fixture 'smartagent'
$machine=Join-Path $fixture 'machine'
$sec=Join-Path $root 'localdata\secure\windows_security'
$serviceDir=Join-Path $fixture 'service'
$modePath=Join-Path $sec 'acl_mode.json'
$profilePath=Join-Path $sec 'security_profile.json'
$authPath=Join-Path $machine 'machine_authorization.json'
$servicePath=Join-Path $serviceDir 'service_state.json'
$reportPath=Join-Path $fixture 'result.json'
$cases=0

function Write-Json([string]$Path,$Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
}
function Assert-Stage([string]$Name,[string]$Stage,[string]$ExpectedVerdict,[int]$ExpectedExit) {
    $before=(Get-FileHash -LiteralPath $modePath -Algorithm SHA256).Hash
    $out=& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tool -ProjectRoot $root -Fixture -MachineRoot $machine -ReportPath $reportPath
    $exitCode=$LASTEXITCODE
    $result=Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($exitCode -ne $ExpectedExit -or $result.verdict -ne $ExpectedVerdict -or
        [string]$result.earliest_unmet_postcondition -ne $Stage -or
        $result.exact_failure_cause_proven -ne $false -or
        $result.full_os_acl_attestation -ne $false -or
        $result.fixture_only -ne $true) {
        throw ('CASE FAILED: ' + $Name + '; result=' + ($result | ConvertTo-Json -Depth 8) + '; console=' + ($out | Out-String))
    }
    $after=(Get-FileHash -LiteralPath $modePath -Algorithm SHA256).Hash
    if ($before -ne $after) { throw ('READ_ONLY VIOLATION: ' + $Name) }
    $script:cases += 1
    Write-Host ('PASS: ' + $Name)
}
try {
    New-Item -ItemType Directory -Force -Path $sec,$machine,$serviceDir | Out-Null
    foreach ($name in @(
        '.venv\Scripts\python.exe',
        'config\protocol_manifest.json',
        'install_smart_agent\configure_security.ps1',
        'source\agent_core\windows_security.py'
    )) {
        $file=Join-Path $root $name
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $file) | Out-Null
        [IO.File]::WriteAllText($file,'FIXTURE_ONLY')
    }
    $mode=@{schema='SMARTAGENT_ACL_MODE_V1';install_root=$root;mode='transitioning_on'}
    Write-Json $modePath $mode
    Assert-Stage 'transitioning_on missing profile' 'SECURITY_PROFILE' 'FAILED_OR_INCOMPLETE' 1

    $profile=@{
        schema='SMARTAGENT_WINDOWS_SECURITY_V2'
        executor_sid='S-1-5-21-123-456-789-1001'
        executor_endpoint=$serviceDir
        executor_code_root=(Join-Path $sec 'executor_code')
        executor_user='SmartAgentExecutor_fixture'
    }
    Write-Json $profilePath $profile
    Assert-Stage 'published profile but executor not ready' 'EXECUTOR_SELF_TEST' 'FAILED_OR_INCOMPLETE' 1

    $service=@{
        self_test=@{passed=$true;mode='on'}
        heartbeat_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    Write-Json $servicePath $service
    $auth=@{
        schema='SMARTAGENT_MACHINE_AUTHORIZATION_V1'
        install_root=$root
        security_profile=$profilePath
        executor_sid=$profile.executor_sid
    }
    Write-Json $authPath $auth
    Assert-Stage 'all snapshot markers present but mode not committed' 'ACL_MODE_COMMIT' 'FAILED_OR_INCOMPLETE' 1

    $mode.mode='on'
    Write-Json $modePath $mode
    Assert-Stage 'mode on still not certified by snapshot' '' 'NOT_PROVEN' 2

    $auth.install_root='C:\foreign-smartagent'
    Write-Json $authPath $auth
    Assert-Stage 'foreign machine authorization' 'MACHINE_AUTHORIZATION' 'FAILED_OR_INCOMPLETE' 1

    Remove-Item -LiteralPath (Join-Path $root 'config\protocol_manifest.json') -Force
    Assert-Stage 'missing precheck manifest' 'PRECHECK' 'FAILED_OR_INCOMPLETE' 1

    Write-Host ('ACL_ON_DIAGNOSIS_WINDOWS_FIXTURE_PASS cases=' + $cases)
} finally {
    if (Test-Path -LiteralPath $fixture) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}
