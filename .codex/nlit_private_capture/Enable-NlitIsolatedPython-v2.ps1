# NLIT V2: reversible adapter for TEST bundled Python 3.13 _pth (isolated=1).
# No global Python configuration, original product, source files or manifests touched.
[CmdletBinding()]
param(
 [ValidateSet('Probe','Enable','Verify','Restore')][string]$Mode='Probe',
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$ProductionInstall,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$BackupDir,
 [switch]$TestProcessesStopped
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Block([string]$why) { throw ('NLIT_PYTHON_ADAPTER_BLOCKED:'+$why) }
function Canon([string]$p) {
 if (-not [IO.Path]::IsPathRooted($p)) { Block 'RELATIVE_ROOT' }
 if (!(Test-Path -LiteralPath $p -PathType Container)) { Block 'MISSING_ROOT' }
 return (Resolve-Path -LiteralPath $p).ProviderPath.TrimEnd('\','/')
}
function Overlap([string]$a,[string]$b) {
 $a=$a.TrimEnd('\','/')+'\'
 $b=$b.TrimEnd('\','/')+'\'
 return $a.StartsWith($b,[StringComparison]::OrdinalIgnoreCase) -or
        $b.StartsWith($a,[StringComparison]::OrdinalIgnoreCase)
}
function CheckLinks([string]$p) {
 # StrictMode-safe: FileInfo has Directory, DirectoryInfo has Parent.
 # Do not dereference .Parent on FileInfo: this broke even read-only Probe.
 [System.IO.FileSystemInfo]$node=Get-Item -LiteralPath $p -Force
 while ($null -ne $node) {
  if (($node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
   Block 'REPARSE_POINT'
  }
  if ($node -is [System.IO.FileInfo]) {
   $node=$node.Directory
  } elseif ($node -is [System.IO.DirectoryInfo]) {
   $node=$node.Parent
  } else {
   Block 'UNEXPECTED_FILESYSTEM_INFO_TYPE'
  }
 }
}
function Sha([byte[]]$b) {
 $h=[Security.Cryptography.SHA256]::Create()
 try { return [BitConverter]::ToString($h.ComputeHash($b)).Replace('-','').ToLowerInvariant() }
 finally { $h.Dispose() }
}
$test=Canon $TestInstall
$prod=Canon $ProductionInstall
$obs=Canon $ObserverDir
$backup=Canon $BackupDir
foreach ($p in @($test,$prod,$obs,$backup)) { CheckLinks $p }
if ((Overlap $test $prod) -or (Overlap $test $obs) -or
    (Overlap $test $backup) -or (Overlap $prod $obs) -or
    (Overlap $prod $backup) -or (Overlap $obs $backup)) { Block 'ROOTS_NOT_DISJOINT' }
if ($obs -match '[\r\n"]' -or $obs.StartsWith('#')) { Block 'INVALID_PTH_OBSERVER_PATH' }
$python=Join-Path $test '.venv\Scripts\python.exe'
$pth=Join-Path $test '.venv\Scripts\python313._pth'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { Block 'TEST_PYTHON_MISSING' }
if (!(Test-Path -LiteralPath $pth -PathType Leaf)) { Block 'TEST_PYTHON313_PTH_MISSING' }
CheckLinks $python
CheckLinks $pth
$save=Join-Path $backup 'nlit_python313._pth.original.bin'
$stateFile=Join-Path $backup 'nlit_python313._pth.state.json'
$utf8=New-Object System.Text.UTF8Encoding($false,$true)
$current=[IO.File]::ReadAllBytes($pth)
try { $text=$utf8.GetString($current) } catch { Block 'PTH_NOT_STRICT_UTF8' }
if ($text.StartsWith([char]0xfeff)) { Block 'PTH_BOM_UNSUPPORTED' }
if (@($text -split '\r?\n' | Where-Object { $_ -ceq 'import site' }).Count -ne 1) {
 Block 'IMPORT_SITE_REQUIRED'
}
function VerifyImport {
 $old=$env:SMARTAGENT_REMOTE_WORKER
 try {
  $env:SMARTAGENT_REMOTE_WORKER='0'
  $probe=& $python -c "import sys,pathlib,sitecustomize,private_capture; p=pathlib.Path(sys.argv[1]).resolve(); assert sys.flags.isolated==1 and sys.flags.ignore_environment==1; assert pathlib.Path(private_capture.__file__).resolve()==p/'private_capture.py'; assert pathlib.Path(sitecustomize.__file__).resolve()==p/'sitecustomize.py'; print('NLIT_BUNDLED_ISOLATED_IMPORT_PASS')" $obs 2>&1
  if ($LASTEXITCODE -ne 0 -or !($probe -match 'NLIT_BUNDLED_ISOLATED_IMPORT_PASS')) {
   Block 'BUNDLED_OBSERVER_IMPORT_FAILED'
  }
 } finally { $env:SMARTAGENT_REMOTE_WORKER=$old }
}
if ($Mode -eq 'Probe') {
 if (Test-Path -LiteralPath $stateFile) { Block 'STATE_ALREADY_PRESENT' }
 Write-Output ('NLIT_PYTHON_PTH_PROBE_PASS original_sha256='+(Sha $current))
 return
}
if ($Mode -eq 'Enable') {
 if ((Test-Path -LiteralPath $save) -or (Test-Path -LiteralPath $stateFile)) {
  Block 'REFUSE_OVERWRITE_EXISTING_BACKUP'
 }
 if (@($text -split '\r?\n' | Where-Object { $_ -eq $obs }).Count -ne 0) {
  Block 'OBSERVER_ALREADY_PRESENT'
 }
 $eol=if ($text.Contains(([char]13).ToString()+([char]10).ToString())) {
  ([char]13).ToString()+([char]10).ToString()
 } else { ([char]10).ToString() }
 $append=$text
 if (!$append.EndsWith(([char]10).ToString())) { $append+=$eol }
 $append+=$obs+$eol
 $bytes=$utf8.GetBytes($append)
 if ($bytes.Length -le $current.Length) { Block 'NO_APPEND' }
 $stream=[IO.File]::Open($save,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
 try { $stream.Write($current,0,$current.Length);$stream.Flush($true) }
 finally { $stream.Dispose() }
 $state=[ordered]@{
  schema='NLIT_ISOLATED_PYTHON_PTH_ADAPTER_V1'
  original_sha256=(Sha $current)
  enabled_sha256=(Sha $bytes)
  original_bytes=$current.Length
  enabled_bytes=$bytes.Length
  observer_root=$obs
  product_source_changed=$false
 }
 [IO.File]::WriteAllText($stateFile,($state|ConvertTo-Json -Depth 4),$utf8)
 [IO.File]::WriteAllBytes($pth,$bytes)
 if ((Sha ([IO.File]::ReadAllBytes($pth))) -ne $state.enabled_sha256) {
  Block 'ENABLE_SHA_MISMATCH'
 }
 VerifyImport
 Write-Output ('NLIT_PYTHON_PTH_ENABLE_PASS original_sha256='+$state.original_sha256+' enabled_sha256='+$state.enabled_sha256)
 return
}
if (!(Test-Path -LiteralPath $save -PathType Leaf) -or
    !(Test-Path -LiteralPath $stateFile -PathType Leaf)) { Block 'ADAPTER_STATE_MISSING' }
$state=Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($state.schema -ne 'NLIT_ISOLATED_PYTHON_PTH_ADAPTER_V1' -or
    $state.observer_root -ne $obs) { Block 'ADAPTER_STATE_MISMATCH' }
$original=[IO.File]::ReadAllBytes($save)
if ((Sha $original) -ne $state.original_sha256) { Block 'BACKUP_SHA_MISMATCH' }
if ((Sha $current) -ne $state.enabled_sha256) { Block 'ENABLED_PTH_CHANGED' }
if ($Mode -eq 'Verify') {
 VerifyImport
 Write-Output ('NLIT_PYTHON_PTH_VERIFY_PASS enabled_sha256='+$state.enabled_sha256)
 return
}
if ($Mode -eq 'Restore') {
 if (!$TestProcessesStopped) { Block 'STOP_DEDICATED_TEST_PROCESSES_FIRST' }
 [IO.File]::WriteAllBytes($pth,$original)
 if ((Sha ([IO.File]::ReadAllBytes($pth))) -ne $state.original_sha256) {
  Block 'RESTORE_SHA_MISMATCH'
 }
 $state | Add-Member -NotePropertyName restored -NotePropertyValue $true -Force
 [IO.File]::WriteAllText($stateFile,($state|ConvertTo-Json -Depth 4),$utf8)
 Write-Output ('NLIT_PYTHON_PTH_RESTORE_PASS original_sha256='+$state.original_sha256)
 return
}
Block 'INVALID_MODE'
