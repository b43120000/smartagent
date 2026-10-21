[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Commit,
  [string]$SourceRoot='C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1',
  [string]$TargetRoot='E:\card\release\SmartAgentv2'
)
$ErrorActionPreference='Stop'
$source=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
$target=[IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
if($source.Equals($target,[StringComparison]::OrdinalIgnoreCase)){throw 'SOURCE_TARGET_MUST_DIFFER'}
if($Commit -notmatch '^[a-fA-F0-9]{7,40}$'){throw 'COMMIT_SHA_REQUIRED'}
$git=Join-Path $source '.venv\git\cmd\git.exe'
if(!(Test-Path -LiteralPath $git)){throw 'BUNDLED_GIT_MISSING: run install_smart_agent\launchers\ensure_portable_runtime.bat first'}
function Invoke-Git([string[]]$Arguments) {
  & $git @Arguments
  if($LASTEXITCODE -ne 0){throw "GIT_FAILED: $($Arguments[0]) (exit $LASTEXITCODE)"}
}
foreach($repo in @($source,$target)){
  $top=(& $git -C $repo rev-parse --show-toplevel)
  if($LASTEXITCODE -ne 0 -or !([IO.Path]::GetFullPath($top).TrimEnd('\').Equals($repo,[StringComparison]::OrdinalIgnoreCase))){throw "REPOSITORY_ROOT_MISMATCH: $repo"}
}
if((& $git -C $target status --porcelain) -or $LASTEXITCODE -ne 0){throw 'TARGET_DIRTY: commit or preserve destination changes before cherry-pick'}
foreach($state in @('CHERRY_PICK_HEAD','MERGE_HEAD','rebase-merge','rebase-apply')){
  $path=& $git -C $target rev-parse --git-path $state
  if(![IO.Path]::IsPathRooted($path)){$path=Join-Path $target $path}
  if(Test-Path -LiteralPath $path){throw "TARGET_OPERATION_IN_PROGRESS: $state"}
}
$sha=(& $git -C $source rev-parse --verify "$Commit^{commit}").Trim()
if($LASTEXITCODE -ne 0){throw 'SOURCE_COMMIT_NOT_FOUND'}
# A previously transferred change is idempotent and never replayed.
$seen=& $git -C $target log --format=%B --fixed-strings --grep="cherry picked from commit $sha"
if($seen){Write-Output "ALREADY_APPLIED $sha";exit 0}
Invoke-Git -Arguments @('-C',$target,'fetch','--no-tags',$source,$sha)
Invoke-Git -Arguments @('-C',$target,'cherry-pick','-x',$sha)
Write-Output "GIT_RELEASE_APPLIED $sha"
# Conflicts stay visible in Git. Resolve then cherry-pick --continue, or --abort.
# This command never resets, overwrites dirty files, copies localdata, or pushes.
