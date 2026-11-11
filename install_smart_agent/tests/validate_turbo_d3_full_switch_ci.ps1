# P3-D3 disposable Windows old-Broker -> populated Root end-to-end test.
[CmdletBinding()]
param([switch]$CrashTest)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP){throw 'TURBO_D3_SWITCH_CI_ONLY'}
function Check([bool]$ok,[string]$msg){if(-not $ok){throw $msg}}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboD3Switch-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$ws=Join-Path $fixture 'workspace'
$old=Join-Path $ws 'old_broker'
$target=Join-Path $ws 'existing_case'
$secure=Join-Path $project 'localdata\secure\windows_security'
$policyPath=Join-Path $secure 'workspace_access_policy.json'
$activePath=Join-Path $secure 'turbo_os_activation.json'
$importPath=Join-Path $secure 'turbo_existing_root_import.json'
$switchPath=Join-Path $secure 'turbo_existing_root_switch.json'
$native=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$switch=Join-Path $PSScriptRoot '..\turbo_existing_root_switch.ps1'
try{
 New-Item -ItemType Directory -Force -Path $secure,$target | Out-Null
 $sub=Join-Path $target 'case_a'
 New-Item -ItemType Directory -Path $sub | Out-Null
 $inputFile=Join-Path $sub 'original.txt'
 [IO.File]::WriteAllText($inputFile,'ORIGINAL_INTACT')
 $inputHash=(Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash
 $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
 $policy=[ordered]@{schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1';revision=43;writable_workspaces=@($ws);read_only_roots=@();denied_write_roots=@();turbo_roots=@($old);turbo_requested=$true}
 [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
 [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
 & (Join-Path $PSScriptRoot '..\turbo_auto_activate.ps1') -ProjectRoot $project -Action Apply | Out-Null
 Check (Test-Path -LiteralPath $activePath -PathType Leaf) 'TURBO_D3_SWITCH_CI_OLD_NOT_ACTIVE'
 $oldData=Join-Path $old 'existing_output.txt'
 [IO.File]::WriteAllText($oldData,'OLD_USER_DATA')
 $oldHash=(Get-FileHash -LiteralPath $oldData -Algorithm SHA256).Hash
 & (Join-Path $PSScriptRoot '..\turbo_existing_root_prepare.ps1') -Action Prepare -ProjectRoot $project -Workspace $ws -TurboRoot $target | Out-Null
 $snaps=@(Get-ChildItem -LiteralPath (Join-Path $secure 'turbo_existing_root_snapshots') -Filter 'snapshot-*.json' -File)
 Check ($snaps.Count -eq 1) 'TURBO_D3_SWITCH_CI_TARGET_SNAPSHOT_MISSING'
 $snapshot=Get-Content -LiteralPath $snaps[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
 & (Join-Path $PSScriptRoot '..\turbo_broker_switch_checkpoint.ps1') -Action Assess -ProjectRoot $project -Workspace $ws -TargetTurboRoot $target -SnapshotId ([string]$snapshot.snapshot_id) | Out-Null
 $reviews=@(Get-ChildItem -LiteralPath (Join-Path $secure 'turbo_broker_switch_reviews') -Filter 'review-*.json' -File)
 Check ($reviews.Count -eq 1) 'TURBO_D3_SWITCH_CI_REVIEW_MISSING'
 $review=Get-Content -LiteralPath $reviews[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
 $args=@{ProjectRoot=$project;Workspace=$ws;TargetTurboRoot=$target;SnapshotId=[string]$snapshot.snapshot_id;ReviewId=[string]$review.review_id}
 $wrong=$false
 try { & $switch -Action Switch @args -ConfirmTarget $old | Out-Null }
 catch {$wrong=$_.Exception.Message -match 'TURBO_SWITCH_EXPLICIT_TARGET_SCOPE_CONFIRMATION_FAILED'}
 Check $wrong 'TURBO_D3_SWITCH_CI_WRONG_CONFIRMATION_ACCEPTED'
 Check (Test-Path -LiteralPath $activePath) 'TURBO_D3_SWITCH_CI_PRECHECK_REVOKED_OLD'
 Write-Host 'TURBO_D3_SWITCH_CI_NEGATIVE_SCOPE_GATE_PASS'
 if($CrashTest){
   # Run an isolated child PowerShell so abrupt exit cannot terminate CI.
   $child=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$switch,
     '-Action','Switch','-ProjectRoot',$project,'-Workspace',$ws,
     '-TargetTurboRoot',$target,'-SnapshotId',[string]$snapshot.snapshot_id,
     '-ReviewId',[string]$review.review_id,'-ConfirmTarget',$target,
     '-TestCrashAfterNewActivation')
   & powershell.exe @child | Out-Null
   Check ($LASTEXITCODE -eq 87) ('TURBO_D3_SWITCH_CI_CRASH_EXIT_WRONG:'+ $LASTEXITCODE)
   $global:LASTEXITCODE=0
   Check (Test-Path -LiteralPath $switchPath -PathType Leaf) 'TURBO_D3_SWITCH_CI_CRASH_JOURNAL_NOT_PERSISTED'
   Check (Test-Path -LiteralPath $activePath -PathType Leaf) 'TURBO_D3_SWITCH_CI_CRASH_NEW_BROKER_NOT_PROVISIONED'
   $pending=Get-Content -LiteralPath $switchPath -Raw -Encoding UTF8 | ConvertFrom-Json
   Check ($pending.status -eq 'NEW_ACTIVATING') 'TURBO_D3_SWITCH_CI_CRASH_WRONG_STAGE'
   $active=Get-Content -LiteralPath $activePath -Raw -Encoding UTF8 | ConvertFrom-Json
   $cmd=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("Write-Output 'MUST_NOT_EXECUTE'"))
   & powershell.exe -NoProfile -NonInteractive -File ([string]$active.broker_entry) -ProjectRoot $project -Workspace $ws -WorkingDirectory $target -ExpectedRevision 44 -TaskId 'CRASH-TEST' -ActionId 'BLOCKED-ACTION' -CommandBase64 $cmd -TimeoutSeconds 10 -MaxOutputBytes 1024 2>&1 | Out-Null
   Check ($LASTEXITCODE -ne 0) 'TURBO_D3_SWITCH_CI_PENDING_ALLOWED_NATIVE_EXECUTION'
   $global:LASTEXITCODE=0
   Write-Host 'TURBO_D3_SWITCH_CI_CRASH_PENDING_DENIES_BROKER_PASS'
   & $switch -Action Recover -ProjectRoot $project
   Check (-not (Test-Path -LiteralPath $switchPath)) 'TURBO_D3_SWITCH_CI_RECOVER_JOURNAL_REMAINS'
   Check (-not (Test-Path -LiteralPath $activePath)) 'TURBO_D3_SWITCH_CI_RECOVER_ACTIVATION_REMAINS'
   Check (-not (Test-Path -LiteralPath $importPath)) 'TURBO_D3_SWITCH_CI_RECOVER_IMPORT_REMAINS'
   $p=Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
   Check ($p.turbo_requested -eq $false) 'TURBO_D3_SWITCH_CI_RECOVER_POLICY_STILL_ON'
   Check ((Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash -eq $inputHash) 'TURBO_D3_SWITCH_CI_RECOVER_ORIGINAL_DATA_CHANGED'
   Check ((Get-FileHash -LiteralPath $oldData -Algorithm SHA256).Hash -eq $oldHash) 'TURBO_D3_SWITCH_CI_RECOVER_OLD_DATA_CHANGED'
   Write-Host 'TURBO_D3_SWITCH_CI_HARD_CRASH_CROSS_PROCESS_RECOVERY_SAFE_OFF_PASS'
   return
 }
 & $switch -Action Switch @args -ConfirmTarget $target
 Check (-not (Test-Path -LiteralPath $switchPath)) 'TURBO_D3_SWITCH_CI_PENDING_TRANSACTION'
 $a=Get-Content -LiteralPath $activePath -Raw -Encoding UTF8 | ConvertFrom-Json
 $p=Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ($a.existing_root_import -eq $true -and $a.status -eq 'ACTIVE_PROVISIONED' -and
   $p.turbo_requested -eq $true -and [int]$p.revision -eq 44 -and $p.turbo_roots[0] -eq $target) 'TURBO_D3_SWITCH_CI_NEW_BINDING_BAD'
 Check ((Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash -eq $inputHash) 'TURBO_D3_SWITCH_CI_TARGET_CONTENT_CHANGED'
 Check ((Get-FileHash -LiteralPath $oldData -Algorithm SHA256).Hash -eq $oldHash) 'TURBO_D3_SWITCH_CI_OLD_CONTENT_CHANGED'
 Write-Host 'TURBO_D3_SWITCH_CI_OLD_TO_EXISTING_NATIVE_ACTIVE_PASS'
 # Invoke the installed protected broker entry, NOT host-side script.
 $newFile=Join-Path $sub 'from_real_broker.txt'
 $text="[IO.File]::WriteAllText('$($newFile.Replace("'","''"))','NATIVE_WRITTEN')"
 $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
 $raw=(& powershell.exe -NoProfile -NonInteractive -File ([string]$a.broker_entry) -ProjectRoot $project -Workspace $ws -WorkingDirectory $target -ExpectedRevision 44 -TaskId 'CI-D3-TASK' -ActionId 'CI-D3-ACTION' -CommandBase64 $encoded -TimeoutSeconds 30 -MaxOutputBytes 65536) -join ''
 $receipt=$raw | ConvertFrom-Json -ErrorAction Stop
 Check ($receipt.status -eq 'EXECUTED_IN_OS_APPCONTAINER' -and $receipt.sandbox_identity_verified -eq $true -and
     $receipt.kernel_outside_read_denied -eq $true -and $receipt.command_exit_code -eq 0) 'TURBO_D3_SWITCH_CI_NATIVE_ACTION_FAILED'
 Check ([IO.File]::ReadAllText($newFile) -ceq 'NATIVE_WRITTEN') 'TURBO_D3_SWITCH_CI_NEW_FILE_MISSING'
 Write-Host 'TURBO_D3_SWITCH_CI_NATIVE_ACTION_ATTESTATION_PASS'
 & $native -Action Revoke -ProjectRoot $project | Out-Null
 Check (-not (Test-Path -LiteralPath $activePath) -and -not (Test-Path -LiteralPath $importPath)) 'TURBO_D3_SWITCH_CI_REVOKE_NOT_COMPLETE'
 Check ([IO.File]::ReadAllText($newFile) -ceq 'NATIVE_WRITTEN') 'TURBO_D3_SWITCH_CI_NEW_DATA_REMOVED'
 Check ((Get-FileHash -LiteralPath $inputFile -Algorithm SHA256).Hash -eq $inputHash) 'TURBO_D3_SWITCH_CI_ORIGINAL_DATA_CHANGED'
 foreach($entry in @($snapshot.entries)) {
    if ((Get-Acl -LiteralPath ([string]$entry.path) -Audit).Sddl -cne [string]$entry.sddl) {
       throw ('TURBO_D3_SWITCH_CI_REVOKE_SDDL_DRIFT:'+$entry.path)
    }
 }
 Write-Host 'TURBO_D3_SWITCH_CI_EXACT_SDDL_RESTORED_PASS'
} finally {
 if(Test-Path -LiteralPath $switchPath){
   try{& $switch -Action Recover -ProjectRoot $project | Out-Null}catch{Write-Warning ('TURBO_D3_CI_SWITCH_RECOVERY_FAILED:'+ $_.Exception.Message)}
 }
 if(Test-Path -LiteralPath $activePath){
   try{& $native -Action Revoke -ProjectRoot $project | Out-Null}catch{Write-Warning ('TURBO_D3_CI_REVOKE_FAILED:'+ $_.Exception.Message)}
 }
 if((Test-Path -LiteralPath $switchPath) -or (Test-Path -LiteralPath $activePath) -or (Test-Path -LiteralPath $importPath)){
    Write-Warning ('TURBO_D3_CI_UNRECOVERED_DISPOSABLE_ROOT:'+ $fixture)
 } elseif(Test-Path -LiteralPath $fixture) {Remove-Item -LiteralPath $fixture -Recurse -Force}
}
