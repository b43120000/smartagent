[CmdletBinding()]
param([string]$ProjectRoot=(Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference='Stop'
function Get-PortableHash([string]$Path) {
  $stream=[IO.File]::OpenRead($Path); $sha=[Security.Cryptography.SHA256]::Create()
  try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
  finally {$stream.Dispose();$sha.Dispose()}
}
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'install_smart_agent\ensure_python.ps1') -ProjectRoot $root
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
$payload=Join-Path $root '.venv\portable-git'; $manifest=Join-Path $payload 'manifest.json'
$m=Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
if($m.schema -ne 'SMARTAGENT_PORTABLE_GIT_V1' -or $m.architecture -ne 'x64' -or $m.archive_sha256 -notmatch '^[a-f0-9]{64}$' -or $m.binary_sha256 -notmatch '^[a-f0-9]{64}$'){throw 'PORTABLE_GIT_MANIFEST_INVALID'}
$gitRoot=Join-Path $root '.venv\git'; $git=Join-Path $gitRoot 'cmd\git.exe'
$mutex=New-Object Threading.Mutex($false,'Local\SmartAgentPortableGit')
if(!$mutex.WaitOne([TimeSpan]::FromMinutes(3))){throw 'PORTABLE_GIT_LOCK_TIMEOUT'}
try {
  if(!(Test-Path -LiteralPath $git)){
    if(Test-Path -LiteralPath $gitRoot){throw 'PORTABLE_GIT_PARTIAL_DIRECTORY: preserve and inspect existing directory'}
    $archive=Join-Path $payload 'git.zip'
    if((Get-PortableHash $archive) -ne $m.archive_sha256){throw 'PORTABLE_GIT_ARCHIVE_HASH_MISMATCH'}
    $tmp=Join-Path $root ('.venv\git-stage-'+[guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
      Add-Type -AssemblyName System.IO.Compression.FileSystem
      $zip=[IO.Compression.ZipFile]::OpenRead($archive)
      try {
        foreach($entry in $zip.Entries){
          $destination=[IO.Path]::GetFullPath((Join-Path $tmp $entry.FullName))
          if([IO.Path]::IsPathRooted($entry.FullName) -or $entry.FullName.Contains(':') -or !$destination.StartsWith($tmp+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'PORTABLE_GIT_ZIP_TRAVERSAL'}
        }
        [IO.Compression.ZipFileExtensions]::ExtractToDirectory($zip,$tmp)
      } finally {$zip.Dispose()}
      $candidate=Join-Path $tmp 'cmd\git.exe'
      if((Get-PortableHash $candidate) -ne $m.binary_sha256){throw 'PORTABLE_GIT_BINARY_HASH_MISMATCH'}
      & $candidate --version
      if($LASTEXITCODE -ne 0){throw 'PORTABLE_GIT_PROBE_FAILED'}
      Move-Item -LiteralPath $tmp -Destination $gitRoot
    } finally {
      $resolved=[IO.Path]::GetFullPath($tmp)
      if(!$resolved.StartsWith($root+'\.venv\git-stage-',[StringComparison]::OrdinalIgnoreCase)){throw 'UNSAFE_STAGE_CLEANUP'}
      if(Test-Path -LiteralPath $resolved){Remove-Item -LiteralPath $resolved -Recurse -Force}
    }
  }
  if((Get-PortableHash $git) -ne $m.binary_sha256){throw 'PORTABLE_GIT_BINARY_HASH_MISMATCH'}
  & $git --version
  if($LASTEXITCODE -ne 0){throw 'PORTABLE_GIT_PROBE_FAILED'}
} finally {$mutex.ReleaseMutex() | Out-Null; $mutex.Dispose()}
$python=Join-Path $root '.venv\Scripts\python.exe'
& $python -I -m pip --version
if($LASTEXITCODE -ne 0){throw 'PORTABLE_PIP_MISSING'}
$env:PYTHONPATH=(Join-Path $root 'source')+';'+$env:PYTHONPATH
& $python -B -m agent_core.runtime_environment --root $root
if($LASTEXITCODE -ne 0){throw 'ENVIRONMENT_LEDGER_REFRESH_FAILED'}
