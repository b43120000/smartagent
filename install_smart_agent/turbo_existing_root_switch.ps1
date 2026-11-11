# P3-D3 controlled old-Broker -> existing-Root switch.
# An explicit elevated operator action; NEVER automatically initiated by
# Planner/Telegram/Edit Workspace/ACLstatus. All active commands share one OS
# mutex, so switch cannot race an in-flight compliant Broker action.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][ValidateSet('Switch','Recover','Status')][string]$Action,
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [string]$Workspace='', [string]$TargetTurboRoot='', [string]$SnapshotId='',
 [string]$ReviewId='', [string]$ConfirmTarget='',
 [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p){if([string]::IsNullOrWhiteSpace($p)){throw 'TURBO_SWITCH_EMPTY_PATH'};return [IO.Path]::GetFullPath($p).TrimEnd('\')}
function Same([string]$a,[string]$b){return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)}
function Under([string]$a,[string]$b){return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)}
function Read-Json([string]$path) {
 if(-not (Test-Path -LiteralPath $path -PathType Leaf)){throw ('TURBO_SWITCH_REQUIRED_STATE_MISSING:'+$path)}
 if((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'TURBO_SWITCH_STATE_REPARSE'}
 return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function Save-Switch([object]$record){
 $tmp=$script:switchPath+'.tmp'
 $bytes=[Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Depth 12))
 $stream=[IO.FileStream]::new($tmp,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
 try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
 & icacls.exe $tmp '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
 if($LASTEXITCODE -ne 0){throw 'TURBO_SWITCH_JOURNAL_ACL_FAILED'}
 Move-Item -LiteralPath $tmp -Destination $script:switchPath -Force -ErrorAction Stop
}
function Write-Policy([object]$policy){
 $bytes=[Text.Encoding]::UTF8.GetBytes(($policy | ConvertTo-Json -Depth 12))
 # Preserve original file ACL: write in place, do not replace with a new file.
 $stream=[IO.FileStream]::new($script:policyPath,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::None)
 try{$stream.SetLength(0);$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}
 finally{$stream.Dispose()}
}
function Force-Safe-Off {
 $record=Read-Json $script:switchPath
 if($record.schema -ne 'SMARTAGENT_TURBO_EXISTING_SWITCH_V1' -or
    -not (Same $record.install_root $script:project)){
    throw 'TURBO_SWITCH_RECOVERY_JOURNAL_UNTRUSTED'
 }
 $native=Join-Path $PSScriptRoot 'turbo_os_activation.ps1'
 $active=Join-Path $script:secure 'turbo_os_activation.json'
 $import=Join-Path $script:secure 'turbo_existing_root_import.json'
 # This may require re-entering this same named Mutex; actions run on the
 # same elevated PowerShell thread. Revoke is idempotently retried only from
 # the protected transaction journal and exact Package ID.
 if(Test-Path -LiteralPath $active -PathType Leaf) {
    & $native -Action Revoke -ProjectRoot $script:project
 }
 if(Test-Path -LiteralPath $import -PathType Leaf) {
    & (Join-Path $PSScriptRoot 'turbo_existing_root_import.ps1') -Action Recover -ProjectRoot $script:project
 }
 if((Test-Path -LiteralPath $active) -or (Test-Path -LiteralPath $import)) {
    throw 'TURBO_SWITCH_RECOVERY_BROKER_STILL_ACTIVE'
 }
 $policy=Read-Json $script:policyPath
 if($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'){
    throw 'TURBO_SWITCH_RECOVERY_POLICY_INVALID'
 }
 $policy.turbo_requested=$false
 $policy.turbo_roots=@()
 $policy.revision=[int]$policy.revision+1
 Write-Policy $policy
 $record.status='RECOVERED_SAFE_OFF'
 $record.safe_off_revision=[int]$policy.revision
 Save-Switch $record
 $history=Join-Path $script:secure ('turbo_switch_history-'+[string]$record.switch_id+'.json')
 Move-Item -LiteralPath $script:switchPath -Destination $history -ErrorAction Stop
 Write-Host 'TURBO_SWITCH_RECOVERY_SAFE_OFF_PASS'
 Write-Host 'TURBO_SWITCH_PRODUCTION_FULL_ACCESS=DISABLED'
}
$script:project=Full $ProjectRoot
$script:secure=Join-Path $script:project 'localdata\secure\windows_security'
$script:switchPath=Join-Path $script:secure 'turbo_existing_root_switch.json'
$script:policyPath=Join-Path $script:secure 'workspace_access_policy.json'
if($Action -eq 'Status'){
 if(Test-Path -LiteralPath $script:switchPath){
    Write-Host ('TURBO_SWITCH_STATUS='+(Read-Json $script:switchPath).status)
 }else{Write-Host 'TURBO_SWITCH_STATUS=NO_PENDING_TRANSACTION'}
 return
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'TURBO_SWITCH_ELEVATED_ADMINISTRATOR_REQUIRED'
}
$sha=[Security.Cryptography.SHA256]::Create()
try{$digest=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:project.ToUpperInvariant()))).Replace('-','')}
finally{$sha.Dispose()}
$mutex=[Threading.Mutex]::new($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
try{
 try{$held=$mutex.WaitOne([TimeSpan]::FromSeconds(320))}
 catch [Threading.AbandonedMutexException]{$held=$true}
 if(-not $held){throw 'TURBO_SWITCH_MUTEX_TIMEOUT'}
 if($Action -eq 'Recover'){
    if(-not (Test-Path -LiteralPath $script:switchPath)){
       Write-Host 'TURBO_SWITCH_RECOVERY_NOT_REQUIRED'
       return
    }
    Force-Safe-Off
    return
 }
 if(Test-Path -LiteralPath $script:switchPath){
    throw 'TURBO_SWITCH_PENDING_TRANSACTION_REQUIRES_RECOVER'
 }
 if($SnapshotId -cnotmatch '^[0-9a-f]{32}$' -or
    $ReviewId -cnotmatch '^[0-9a-f]{32}$'){
    throw 'TURBO_SWITCH_SNAPSHOT_AND_REVIEW_IDS_REQUIRED'
 }
 $workspace=Full $Workspace
 $target=Full $TargetTurboRoot
 if(-not (Same $ConfirmTarget $target) -or
    -not (Under $target $workspace) -or
    (Under $target $script:project) -or
    (Under $workspace $script:project) -or
    (Under $script:project $workspace)){
    throw 'TURBO_SWITCH_EXPLICIT_TARGET_SCOPE_CONFIRMATION_FAILED'
 }
 $mode=Read-Json (Join-Path $script:secure 'acl_mode.json')
 if($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
    -not (Same $mode.install_root $script:project)){
    throw 'TURBO_SWITCH_ACL_ON_REQUIRED'
 }
 $policy=Read-Json $script:policyPath
 if($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
    $policy.turbo_requested -ne $true -or
    @($policy.writable_workspaces | Where-Object {Same ([string]$_) $workspace}).Count -ne 1){
    throw 'TURBO_SWITCH_POLICY_INVALID'
 }
 $reviewDir=Join-Path $script:secure 'turbo_broker_switch_reviews'
 $review=Read-Json (Join-Path $reviewDir ('review-'+$ReviewId+'.json'))
 $oldJournalPath=Join-Path $script:secure 'turbo_os_activation.json'
 $old=Read-Json $oldJournalPath
 if($review.schema -ne 'SMARTAGENT_TURBO_BROKER_SWITCH_REVIEW_V1' -or
    $review.status -ne 'ASSESSMENT_ONLY' -or
    $review.authorization_to_revoke -ne $false -or
    $review.authorization_to_mutate -ne $false -or
    $review.review_id -cne $ReviewId -or $review.snapshot_id -cne $SnapshotId -or
    -not (Same $review.target_turbo_root $target) -or
    -not (Same $review.install_root $script:project) -or
    $old.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
    $old.status -ne 'ACTIVE_PROVISIONED' -or
    -not (Same $old.turbo_root $review.previous_turbo_root) -or
    [string]$old.package_sid -cne [string]$review.previous_package_sid -or
    (Get-FileHash -LiteralPath $oldJournalPath -Algorithm SHA256).Hash -cne
       [string]$review.original_journal_sha256){
    throw 'TURBO_SWITCH_UNTRUSTED_BROKER_REVIEW'
 }
 # Before any mutation, independently check the old Broker and the 68-item
 # target using the EXACT review and original snapshot. This also preserves
 # an extra immutable snapshot of the OLD Root security metadata.
 & (Join-Path $PSScriptRoot 'turbo_p3d_switch_preconditions.ps1') -ProjectRoot $script:project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $SnapshotId -ReviewId $ReviewId -MaxEntries $MaxEntries
 $switchId=[Guid]::NewGuid().ToString('N')
 $record=[ordered]@{
    schema='SMARTAGENT_TURBO_EXISTING_SWITCH_V1'
    status='OLD_REVOKING'
    switch_id=$switchId
    install_root=$script:project
    workspace_root=$workspace
    target_turbo_root=$target
    old_root=[string]$review.previous_turbo_root
    old_package_sid=[string]$old.package_sid
    old_review_id=$ReviewId
    target_snapshot_id=$SnapshotId
    old_policy_revision=[int]$policy.revision
    new_policy_revision=0
    new_snapshot_id=''
    started_at=[DateTimeOffset]::UtcNow.ToString('o')
 }
 Save-Switch $record
 try{
    & (Join-Path $PSScriptRoot 'turbo_os_activation.ps1') -Action Revoke -ProjectRoot $script:project
    if(Test-Path -LiteralPath $oldJournalPath){
       throw 'TURBO_SWITCH_OLD_BROKER_NOT_REVOKED'
    }
    $record.status='OLD_REVOKED'
    Save-Switch $record
    # Verify the OLD native Package ACE is genuinely gone, not merely a
    # Journal deletion or return code.
    & (Join-Path $PSScriptRoot 'turbo_broker_switch_checkpoint.ps1') -Action VerifyRevoked -ProjectRoot $script:project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $SnapshotId -ReviewId $ReviewId -MaxEntries $MaxEntries
    # Policy update is controlled by this same protected transaction, not
    # by a Planner tool call or an unverified Edit Workspace input.
    $policy.turbo_requested=$true
    $policy.turbo_roots=@($target)
    $policy.revision=[int]$policy.revision+1
    Write-Policy $policy
    $record.new_policy_revision=[int]$policy.revision
    $record.status='NEW_SNAPSHOT_PREPARING'
    Save-Switch $record
    $snapWriter=Join-Path $PSScriptRoot 'turbo_existing_root_snapshot.ps1'
    $new=@(& $snapWriter -ProjectRoot $script:project -Workspace $workspace -TurboRoot $target -MaxEntries $MaxEntries -CandidateOnly -EmitReceipt |
      Where-Object {$_.schema -eq 'SMARTAGENT_TURBO_B_SNAPSHOT_RECEIPT_V1'})
    if($new.Count -ne 1 -or $new[0].candidate_only -ne $true -or
       $new[0].authorization_to_mutate -ne $false){
       throw 'TURBO_SWITCH_TARGET_NEW_SNAPSHOT_UNTRUSTED'
    }
    $newId=[string]$new[0].snapshot_id
    & (Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1') -Action Verify -ProjectRoot $script:project -Workspace $workspace -TurboRoot $target -SnapshotId $newId -MaxEntries $MaxEntries
    $record.new_snapshot_id=$newId
    $record.status='NEW_ACTIVATING'
    Save-Switch $record
    & (Join-Path $PSScriptRoot 'turbo_existing_root_import.ps1') -Action Activate -ProjectRoot $script:project -Workspace $workspace -TurboRoot $target -SnapshotId $newId -SwitchReceiptId $switchId -MaxEntries $MaxEntries
    $active=Read-Json $oldJournalPath
    if($active.status -ne 'ACTIVE_PROVISIONED' -or
       $active.existing_root_import -ne $true -or
       $active.existing_import_snapshot_id -cne $newId -or
       -not (Same $active.turbo_root $target) -or
       [int]$active.policy_revision -ne [int]$policy.revision){
       throw 'TURBO_SWITCH_NEW_NATIVE_ENROLLMENT_NOT_ATTESTED'
    }
    $record.status='COMPLETE'
    Save-Switch $record
    $history=Join-Path $script:secure ('turbo_switch_history-'+$switchId+'.json')
    Move-Item -LiteralPath $script:switchPath -Destination $history -ErrorAction Stop
    Write-Host 'TURBO_SWITCH_EXISTING_ROOT_NATIVE_BROKER_ACTIVE'
    Write-Host ('TURBO_SWITCH_NEW_SNAPSHOT_ID='+$newId)
    Write-Host ('TURBO_SWITCH_NEW_POLICY_REVISION='+[int]$policy.revision)
    Write-Host 'TURBO_SWITCH_FULL_ACCESS_SCOPE=OS_APP_CONTAINER_PER_ACTION'
 }catch{
    $failure=$_
    # Leave a durable journal and fail-closed policy for explicit recovery.
    # An interrupted switch must NEVER reauthorize the revoked old broker.
    Write-Warning ('TURBO_SWITCH_BLOCKED_RECOVER_REQUIRED:'+$failure.Exception.Message)
    throw $failure
 }
}
finally{
 if($held){try{$mutex.ReleaseMutex()}catch{}}
 $mutex.Dispose()
}
