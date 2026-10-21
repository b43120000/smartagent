[CmdletBinding()]
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference='Stop'
function Get-PortableHash([string]$Path) {
  $stream=[IO.File]::OpenRead($Path); $sha=[Security.Cryptography.SHA256]::Create()
  try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
  finally {$stream.Dispose();$sha.Dispose()}
}
function Test-SmartAgentImports([string]$PythonPath,[string]$ProjectRoot) {
  if(!(Test-Path -LiteralPath $PythonPath -PathType Leaf)){return $false}
  $priorRoot=[Environment]::GetEnvironmentVariable('SMARTAGENT_IMPORT_CHECK_ROOT','Process')
  $priorPreference=$ErrorActionPreference
  try {
    [Environment]::SetEnvironmentVariable('SMARTAGENT_IMPORT_CHECK_ROOT',$ProjectRoot,'Process')
    $ErrorActionPreference='Continue'
    & $PythonPath -I -B -c "import os,sys; sys.path.insert(0,os.path.join(os.environ['SMARTAGENT_IMPORT_CHECK_ROOT'],'source')); import agent_core, PIL, playwright, greenlet" *> $null
    return $LASTEXITCODE -eq 0
  } finally {
    $ErrorActionPreference=$priorPreference
    [Environment]::SetEnvironmentVariable('SMARTAGENT_IMPORT_CHECK_ROOT',$priorRoot,'Process')
  }
}
function Resolve-PortableCleanupPath([string]$Path,[string]$Root) {
  $full=[IO.Path]::GetFullPath($Path).TrimEnd('\')
  $base=[IO.Path]::GetFullPath($Root).TrimEnd('\')
  if(!([IO.Path]::GetDirectoryName($full).Equals($base,[StringComparison]::OrdinalIgnoreCase)) -or
      [IO.Path]::GetFileName($full) -notmatch '^\.venv\.portable-[a-f0-9]{32}$'){
    throw "PORTABLE_RUNTIME_CLEANUP_PATH_INVALID:$full"
  }
  return $full
}
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\'); $venv=Join-Path $root '.venv'; $python=Join-Path $venv 'Scripts\python.exe'
$pyvenv=Join-Path $venv 'pyvenv.cfg'
# A user-managed venv is never modified, but it is usable only when the exact
# isolated SmartAgent import set succeeds from its installed location.
if(Test-Path -LiteralPath $pyvenv -PathType Leaf){
  if(!(Test-SmartAgentImports $python $root)){
    throw 'EXISTING_VENV_INCOMPATIBLE: .venv has pyvenv.cfg but required SmartAgent imports are unavailable; the existing environment was preserved.'
  }
  exit 0
}
$payload=Join-Path $root '.venv\portable-runtime'; $manifest=Join-Path $payload 'manifest.json'
if(!(Test-Path -LiteralPath $manifest)){ throw 'PORTABLE_RUNTIME_MISSING: install payload is absent.' }
$mutex=New-Object Threading.Mutex($false,'Local\SmartAgentPortableRuntime')
if(!$mutex.WaitOne([TimeSpan]::FromMinutes(5))){ throw 'PORTABLE_RUNTIME_LOCK_TIMEOUT' }
try {
  $m=Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
  if($m.schema -ne 'SMARTAGENT_PORTABLE_RUNTIME_V1' -or $m.architecture -ne 'x64' -or $m.archive_sha256 -notmatch '^[a-f0-9]{64}$'){throw 'PORTABLE_RUNTIME_MANIFEST_INVALID'}
  $parts=@($m.parts); if(!$parts){throw 'PORTABLE_RUNTIME_MANIFEST_INVALID'}
  foreach($part in $parts){if([IO.Path]::GetFileName($part.name) -ne $part.name -or $part.name -notmatch '^runtime\.zip\.part\d{3}$' -or $part.sha256 -notmatch '^[a-f0-9]{64}$'){throw 'PORTABLE_RUNTIME_MANIFEST_INVALID'}}
  $marker=Join-Path $venv 'Scripts\.portable_runtime_archive_sha256'
  $pending=Join-Path $venv '.portable_install_pending.json'
  $pendingRecord=$null
  if(Test-Path -LiteralPath $pending -PathType Leaf){
    try {$pendingRecord=Get-Content -Raw -LiteralPath $pending | ConvertFrom-Json}
    catch {throw 'PORTABLE_RUNTIME_PENDING_MARKER_INVALID'}
    if($pendingRecord.schema -ne 'SMARTAGENT_PORTABLE_INSTALL_PENDING_V1' -or
       $pendingRecord.archive_sha256 -ne $m.archive_sha256){
      throw 'PORTABLE_RUNTIME_PENDING_MARKER_MISMATCH'
    }
    if(Test-Path -LiteralPath $pyvenv){throw 'PORTABLE_RUNTIME_PENDING_MARKER_CONFLICTS_WITH_USER_VENV'}
    $partial=@('Lib','Scripts') | Where-Object {Test-Path -LiteralPath (Join-Path $venv $_)}
    if($partial){
      $recovery=Join-Path $venv ('recovery-'+[guid]::NewGuid().ToString('N'))
      New-Item -ItemType Directory -Path $recovery | Out-Null
      foreach($name in $partial){
        Move-Item -LiteralPath (Join-Path $venv $name) -Destination (Join-Path $recovery $name)
      }
    }
  }
  if(Test-Path -LiteralPath $python -PathType Leaf){
    if(Test-SmartAgentImports $python $root){
      if((Test-Path $marker) -and ((Get-Content -Raw $marker).Trim() -ne $m.archive_sha256)){throw 'PORTABLE_RUNTIME_UPDATE_REQUIRES_NEW_DIRECTORY'}
      exit 0
    }
  }
  if((Test-Path -LiteralPath (Join-Path $venv 'Scripts')) -or (Test-Path -LiteralPath (Join-Path $venv 'Lib'))){throw 'PORTABLE_RUNTIME_FRESH_DIRECTORY_REQUIRED'}
  $tmp=Resolve-PortableCleanupPath (Join-Path $root ('.venv.portable-'+[guid]::NewGuid().ToString('N'))) $root
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    $archive=Join-Path $tmp 'runtime.zip'; $out=[IO.File]::Open($archive,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
    try {foreach($part in $parts){$p=Join-Path $payload $part.name;if(!(Test-Path $p)){throw 'PORTABLE_RUNTIME_PART_MISSING'};if((Get-PortableHash $p) -ne $part.sha256){throw 'PORTABLE_RUNTIME_HASH_MISMATCH'};$input=[IO.File]::OpenRead($p);try{$input.CopyTo($out)}finally{$input.Dispose()}}}finally{$out.Dispose()}
    if((Get-PortableHash $archive) -ne $m.archive_sha256){throw 'PORTABLE_RUNTIME_ARCHIVE_HASH_MISMATCH'}
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($archive);try{foreach($e in $zip.Entries){if([IO.Path]::IsPathRooted($e.FullName) -or $e.FullName.Contains(':')){throw 'PORTABLE_RUNTIME_ZIP_TRAVERSAL'};$dest=[IO.Path]::GetFullPath((Join-Path $tmp $e.FullName));if(!$dest.StartsWith($tmp+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'PORTABLE_RUNTIME_ZIP_TRAVERSAL'}}[IO.Compression.ZipFileExtensions]::ExtractToDirectory($zip,$tmp)}finally{$zip.Dispose()}
    $candidate=Join-Path $tmp '.venv';$candidatePy=Join-Path $candidate 'Scripts\python.exe';if(!(Test-Path $candidatePy)){throw 'PORTABLE_RUNTIME_LAYOUT_INVALID'}
    & $candidatePy -I -B -c "import PIL, playwright, greenlet; print('PORTABLE_RUNTIME_IMPORTS_OK')";if($LASTEXITCODE -ne 0){throw 'PORTABLE_RUNTIME_IMPORT_VALIDATION_FAILED'}
    # A fresh payload directory has no runtime children. Never replace a
    # partial or user-managed runtime; install exactly Lib then Scripts.
    New-Item -ItemType Directory -Force -Path $venv | Out-Null
    if(!(Test-Path -LiteralPath $pending -PathType Leaf)){
      $pendingTemp=$pending+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
      $pendingPayload=[ordered]@{
        schema='SMARTAGENT_PORTABLE_INSTALL_PENDING_V1'
        archive_sha256=[string]$m.archive_sha256
        install_id=[guid]::NewGuid().ToString('N')
      } | ConvertTo-Json -Compress
      try {
        [IO.File]::WriteAllText($pendingTemp,$pendingPayload,(New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $pendingTemp -Destination $pending
      } finally {
        if(Test-Path -LiteralPath $pendingTemp -PathType Leaf){Remove-Item -LiteralPath $pendingTemp -Force}
      }
    }
    if(Test-Path -LiteralPath $pyvenv){throw 'PORTABLE_RUNTIME_WILL_NOT_REPLACE_USER_VENV'}
    Move-Item -LiteralPath (Join-Path $candidate 'Lib') -Destination $venv
    if($env:SMARTAGENT_TEST_PORTABLE_FAIL_AFTER_LIB -eq '1'){throw 'PORTABLE_RUNTIME_TEST_FAIL_AFTER_LIB'}
    Move-Item -LiteralPath (Join-Path $candidate 'Scripts') -Destination $venv
    if(!(Test-SmartAgentImports $python $root)){throw 'PORTABLE_RUNTIME_FINAL_VALIDATION_FAILED'}
    [IO.File]::WriteAllText($marker,$m.archive_sha256+"`n",(New-Object Text.UTF8Encoding($false)))
    if((Get-Content -Raw -LiteralPath $marker).Trim() -ne $m.archive_sha256){throw 'PORTABLE_RUNTIME_ARCHIVE_MARKER_FAILED'}
    Remove-Item -LiteralPath $pending -Force
  } finally {
    $cleanup=Resolve-PortableCleanupPath $tmp $root
    if(Test-Path -LiteralPath $cleanup){Remove-Item -LiteralPath $cleanup -Recurse -Force -ErrorAction SilentlyContinue}
  }
} finally {$mutex.ReleaseMutex()|Out-Null;$mutex.Dispose()}
