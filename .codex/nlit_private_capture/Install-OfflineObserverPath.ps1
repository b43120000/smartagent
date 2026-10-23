# NLIT isolated embedded CPython ._pth overlay; applies ONLY to TEST install.
# Default read-only. -Apply makes immutable backup and atomically replaces test ._pth.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$ProductionInstall,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$PrivateRoot,
 [Parameter(Mandatory=$true)][string]$ArtifactManifest,
 [string]$GitExe='git',
 [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Block([string]$Code) { throw "NLIT_OFFLINE_HOOK_BLOCKED:$Code" }
function Root([string]$P) {
 if (![IO.Path]::IsPathRooted($P)) { Block 'RELATIVE_PATH' }
 if (!(Test-Path -LiteralPath $P -PathType Container)) { Block 'MISSING_DIR' }
 return (Resolve-Path -LiteralPath $P).ProviderPath.TrimEnd('\','/')
}
function Disjoint([string]$A,[string]$B) {
 $a1=$A.TrimEnd('\','/')+'\'
 $b1=$B.TrimEnd('\','/')+'\'
 return !($a1.StartsWith($b1,[StringComparison]::OrdinalIgnoreCase) -or
          $b1.StartsWith($a1,[StringComparison]::OrdinalIgnoreCase))
}
function NoLinks([string]$P) {
 $obj=Get-Item -LiteralPath $P -Force
 while ($null -ne $obj) {
  if (($obj.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
   Block 'PATH_CONTAINS_REPARSE_POINT'
  }
  $obj=$obj.Parent
 }
}
$test=Root $TestInstall
$live=Root $ProductionInstall
$obs=Root $ObserverDir
$private=Root $PrivateRoot
foreach ($p in @($test,$live,$obs,$private)) { NoLinks $p }
foreach ($pair in @(@($test,$live),@($obs,$test),@($obs,$live),
                    @($private,$test),@($private,$live),@($private,$obs))) {
 if (!(Disjoint $pair[0] $pair[1])) { Block 'SEPARATE_ROOTS_REQUIRED' }
}
$testHead=(& $GitExe -C $test rev-parse HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $testHead -ne '309305b2eeeb79aed0925dc4dfee45af5193f8b5') {
 Block 'TEST_INSTALL_PRODUCT_HEAD_MISMATCH'
}
$modified=(& $GitExe -C $test status --porcelain --untracked-files=no 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $modified) { Block 'TRACKED_SOURCE_DIRTY' }
$manifestPath=[IO.Path]::GetFullPath($ArtifactManifest)
if (![string]::Equals([IO.Path]::GetDirectoryName($manifestPath),$obs,[StringComparison]::OrdinalIgnoreCase)) {
 Block 'ARTIFACT_MANIFEST_NOT_INSIDE_OBSERVER_DIR'
}
if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { Block 'MANIFEST_MISSING' }
$manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema -ne 'SMARTAGENT_NLIT_OBSERVER_ARTIFACTS_V1' -or
    $manifest.product_baseline_sha -ne $testHead -or @($manifest.artifacts).Count -lt 5) {
 Block 'MANIFEST_IDENTITY_MISMATCH'
}
foreach ($item in $manifest.artifacts) {
 $name=[string]$item.file
 if ($name -notmatch '^[A-Za-z0-9_.-]+\.(py|ps1)$' -or $name.Contains('..')) {
  Block 'INVALID_ARTIFACT_NAME'
 }
 $file=Join-Path $obs $name
 if (!(Test-Path -LiteralPath $file -PathType Leaf)) { Block 'ARTIFACT_FILE_MISSING' }
 if (((Get-Item -LiteralPath $file).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
  Block 'ARTIFACT_REPARSE_POINT'
 }
 $hash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
 if ($hash -ne ([string]$item.sha256).ToLowerInvariant() -or
     (Get-Item -LiteralPath $file).Length -ne [long]$item.bytes) {
  Block 'ARTIFACT_BYTE_HASH_MISMATCH'
 }
}
foreach ($file in @('sitecustomize.py','private_capture.py')) {
 if (!(Test-Path -LiteralPath (Join-Path $obs $file) -PathType Leaf)) {
  Block 'REQUIRED_HOOK_SOURCE_MISSING'
 }
}
$python=Join-Path $test '.venv\Scripts\python.exe'
$scripts=Join-Path $test '.venv\Scripts'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { Block 'ISOLATED_PYTHON_MISSING' }
$pthFiles=@(Get-ChildItem -LiteralPath $scripts -File -Filter 'python*._pth')
if ($pthFiles.Count -ne 1 -or $pthFiles[0].Name -notmatch '^python3[0-9]{2}\._pth$') {
 Block 'ONE_VERSIONED_EMBEDDED_PTH_REQUIRED'
}
$pth=$pthFiles[0].FullName
if (($pthFiles[0].Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
 Block 'PTH_REPARSE_POINT'
}
$utf8=[System.Text.UTF8Encoding]::new($false,$true)
$oldBytes=[IO.File]::ReadAllBytes($pth)
try { $oldText=$utf8.GetString($oldBytes) }
catch { Block 'PTH_NOT_UTF8' }
$oldHash=(Get-FileHash -LiteralPath $pth -Algorithm SHA256).Hash.ToLowerInvariant()
$lines=@($oldText -split '\r\n|\n|\r' | Where-Object { $_ -ne '' })
if (@($lines | Where-Object { $_.Trim() -eq 'import site' }).Count -ne 1) {
 Block 'PTH_IMPORT_SITE_NOT_PRESENT'
}
if ($obs.Contains([char]13) -or $obs.Contains([char]10)) { Block 'OBSERVER_PATH_HAS_NEWLINE' }
$already=@($lines | Where-Object {
 [string]::Equals($_.Trim(),$obs,[StringComparison]::OrdinalIgnoreCase)
}).Count -gt 0
if (!$already) {
 if (!$Apply) {
  Write-Output ('NLIT_EMBEDDED_PTH_ACTION_REQUIRED old_sha256={0} product={1} manifest_artifacts={2}' -f
    $oldHash,$testHead,@($manifest.artifacts).Count)
  exit 3
 }
 $newLines=New-Object 'System.Collections.Generic.List[string]'
 foreach ($line in $lines) {
  if ($line.Trim() -eq 'import site') { $newLines.Add($obs) }
  $newLines.Add($line)
 }
 $newline=$(if ($oldText.Contains(([string][char]13+[string][char]10))) { ([string][char]13+[string][char]10) } else { [string][char]10 })
 $newText=($newLines -join $newline)+$newline
 $newBytes=$utf8.GetBytes($newText)
 $backup=Join-Path $private ("python_embedded_pth_preimage_"+$oldHash+".bin")
 if (Test-Path -LiteralPath $backup) { Block 'PREIMAGE_BACKUP_ALREADY_EXISTS' }
 [IO.File]::WriteAllBytes($backup,$oldBytes)
 if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $oldHash) {
  Block 'PREIMAGE_BACKUP_MISMATCH'
 }
 $tmp=Join-Path $scripts (".nlit_pth_stage_"+[Guid]::NewGuid().ToString('N')+".tmp")
 try {
  [IO.File]::WriteAllBytes($tmp,$newBytes)
  [IO.File]::Replace($tmp,$pth,$null)
 } finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
 }
}
$newHash=(Get-FileHash -LiteralPath $pth -Algorithm SHA256).Hash.ToLowerInvariant()
$verifyText=$utf8.GetString([IO.File]::ReadAllBytes($pth))
if (!$verifyText.Contains($obs)) { Block 'PTH_OBSERVER_SEARCH_PATH_NOT_WRITTEN' }
Write-Output ('NLIT_EMBEDDED_PTH_INSTALLED old_sha256={0} installed_sha256={1} product_head={2} unchanged_product_source=true existing_agent_touched=false' -f
 $oldHash,$newHash,$testHead)
