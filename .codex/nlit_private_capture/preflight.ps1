# SA-NLIT-20261024 offline dual-axis PRE-FLIGHT ONLY; no mutation/dispatch.
# Windows PowerShell 5.1+, run with -File. An actual isolated test is separate.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ProductionInstall,
 [Parameter(Mandatory=$true)][string]$ProductionWorkspace,
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$TestWorkspace,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$PrivateCaptureRoot,
 [Parameter(Mandatory=$true)][string]$ArtifactManifest,
 [string]$GitExe='git',
 [ValidateSet('BeforeStart','AfterStart')][string]$Phase='BeforeStart',
 [string]$ExpectedProductSha='309305b2eeeb79aed0925dc4dfee45af5193f8b5'
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Fail([string]$Code) { throw "NLIT_PREFLIGHT_BLOCKED:$Code" }
function Abs([string]$PathText) {
 if (-not [System.IO.Path]::IsPathRooted($PathText)) { Fail 'RELATIVE_PATH' }
 if (!(Test-Path -LiteralPath $PathText -PathType Container)) { Fail 'MISSING_DIRECTORY' }
 return (Resolve-Path -LiteralPath $PathText).ProviderPath.TrimEnd('\','/')
}
function Same([string]$A,[string]$B) {
 return [string]::Equals($A.TrimEnd('\','/'),$B.TrimEnd('\','/'),
                        [StringComparison]::OrdinalIgnoreCase)
}
function Nested([string]$A,[string]$B) {
 $a1=$A.TrimEnd('\','/')+'\'
 $b1=$B.TrimEnd('\','/')+'\'
 return (Same $A $B) -or $a1.StartsWith($b1,[StringComparison]::OrdinalIgnoreCase)
}
function NoLinks([string]$PathText) {
 $dir=Get-Item -LiteralPath $PathText -Force
 while ($null -ne $dir) {
   if (($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'REPARSE_POINT_IN_PATH' }
   $dir=$dir.Parent
 }
}
function BlockAlias([string]$A,[string]$B) {
 if ((Nested $A $B) -or (Nested $B $A)) { Fail 'PATHS_OVERLAP' }
}
$prod=Abs $ProductionInstall
$prodWork=Abs $ProductionWorkspace
$test=Abs $TestInstall
$testWork=Abs $TestWorkspace
$obs=Abs $ObserverDir
$private=Abs $PrivateCaptureRoot
foreach ($p in @($prod,$prodWork,$test,$testWork,$obs,$private)) { NoLinks $p }
foreach ($a in @($test,$testWork,$obs,$private)) {
 foreach ($b in @($prod,$prodWork)) { BlockAlias $a $b }
}
foreach ($pair in @(@($test,$testWork),@($test,$obs),@($test,$private),
                     @($testWork,$obs),@($testWork,$private),@($obs,$private))) {
 BlockAlias $pair[0] $pair[1]
}
$prodWriter=[IO.Path]::GetFullPath((Join-Path $prodWork '.agents\chunked_writes'))
$testWriter=[IO.Path]::GetFullPath((Join-Path $testWork '.agents\chunked_writes'))
BlockAlias $testWriter $prodWriter
if ($Phase -eq 'BeforeStart') {
 if (Test-Path -LiteralPath (Join-Path $testWork '.agents')) { Fail 'TEST_WORKSPACE_NOT_FRESH' }
}
if (Test-Path -LiteralPath $testWriter) {
 $man=Join-Path $testWriter 'manifests'
 if (Test-Path -LiteralPath $man) {
   $existing=@(Get-ChildItem -LiteralPath $man -Filter '*.json' -File)
   if ($existing.Count -gt 0) { Fail 'TEST_WRITER_NOT_EMPTY_BEFORE_TARGETED_IT19' }
 }
}
$insecure=@('S-1-1-0','S-1-5-11','S-1-5-32-545')
$acl=Get-Acl -LiteralPath $private
foreach ($ace in $acl.Access) {
 if ($ace.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
 try { $sid=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
 catch { Fail 'PRIVATE_ACL_IDENTITY_NOT_RESOLVED' }
 if ($insecure -contains $sid) { Fail 'PRIVATE_ACL_BROAD_READERS' }
}
$manifestFile=[IO.Path]::GetFullPath($ArtifactManifest)
if (!(Test-Path -LiteralPath $manifestFile -PathType Leaf)) { Fail 'ARTIFACT_MANIFEST_MISSING' }
if (!(Same ([IO.Path]::GetDirectoryName($manifestFile)) $obs)) { Fail 'ARTIFACT_MANIFEST_NOT_OBSERVER_LOCAL' }
$m=Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($m.schema -ne 'SMARTAGENT_NLIT_OBSERVER_ARTIFACTS_V1') { Fail 'BAD_ARTIFACT_SCHEMA' }
foreach ($entry in $m.artifacts) {
 $name=[string]$entry.file
 if ($name -notmatch '^[a-zA-Z0-9_]+[.](py|ps1)$') { Fail 'ARTIFACT_NAME_INVALID' }
 $path=Join-Path $obs $name
 if (!(Test-Path -LiteralPath $path -PathType Leaf)) { Fail 'ARTIFACT_MISSING' }
 $sha=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
 if ($sha -ne ([string]$entry.sha256).ToLowerInvariant()) { Fail 'ARTIFACT_SHA256_MISMATCH' }
 if ((Get-Item -LiteralPath $path).Length -ne [long]$entry.bytes) { Fail 'ARTIFACT_BYTES_MISMATCH' }
}
$localHead=(& $GitExe -C $test rev-parse HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { Fail 'LOCAL_GIT_HEAD_UNAVAILABLE' }
if ($localHead -ne $ExpectedProductSha) { Fail 'PRODUCT_SHA_MISMATCH' }
$modified=(& $GitExe -C $test status --porcelain --untracked-files=no 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $modified) { Fail 'TRACKED_WORKTREE_DIRTY' }
$python=Join-Path $test '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { Fail 'ISOLATED_PYTHON_MISSING' }
$oldPath=$env:PYTHONPATH
try {
 $env:PYTHONPATH=Join-Path $test 'source'
 Push-Location $test
 try {
  $probe = & $python -c "import json; from agent_core.protocol_manifest import validate_protocol_manifest; d=validate_protocol_manifest('.'); print(json.dumps(dict(status=d.get('status'),hash_verification=d.get('hash_verification'),hash_mismatches=d.get('hash_mismatches'))))" 2>&1
  if ($LASTEXITCODE -ne 0) { Fail 'MANIFEST_VALIDATOR_FAILED' }
  $obj=($probe | Select-Object -Last 1) | ConvertFrom-Json
  if ($obj.status -ne 'PASS' -or $obj.hash_verification -ne 'PASS' -or @($obj.hash_mismatches).Count -ne 0) { Fail 'PRODUCT_MANIFEST_MISMATCH' }
 } finally { Pop-Location }
} finally { $env:PYTHONPATH=$oldPath }
function SessionPort([string]$Install) {
 $file=Join-Path $Install 'config\browser_session.json'
 if (!(Test-Path -LiteralPath $file -PathType Leaf)) { Fail 'BROWSER_PREFERENCES_MISSING' }
 $p=Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
 $instance=[int]$p.instance
 if ($instance -lt 1 -or $instance -gt 5) { Fail 'BROWSER_INSTANCE_INVALID' }
 if ($p.browser -eq 'chrome') { return 12800+$instance }
 if ($p.browser -eq 'edge') { return 12810+$instance }
 Fail 'BROWSER_KIND_INVALID'
}
$portProd=SessionPort $prod
$portTest=SessionPort $test
if ($portProd -eq $portTest) { Fail 'BROWSER_PORT_SHARED' }
if ($Phase -eq 'BeforeStart') {
 if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { Fail 'PORT_PROBE_UNAVAILABLE' }
 $listeners=@(Get-NetTCPConnection -State Listen -LocalPort $portTest -ErrorAction SilentlyContinue)
 if ($listeners.Count -gt 0) { Fail 'TEST_BROWSER_PORT_ALREADY_BOUND' }
}
Write-Output ("NLIT_PREFLIGHT_PASS phase={0} baseline={1} artifacts={2} path_isolation=PASS browser_port_distinct=PASS private_acl=CHECKED" -f $Phase,$localHead,@($m.artifacts).Count)
Write-Output 'NLIT_PREFLIGHT_LIMITS: No real Planner submission, browser response, shared-process ownership or NL-IT acceptance proven.'
