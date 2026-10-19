[CmdletBinding()]
param([string]$ProjectRoot = $PSScriptRoot)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\'); $venv=Join-Path $root '.venv'; $python=Join-Path $venv 'Scripts\python.exe'
# A normal, user-managed runtime is never replaced. A prior interrupted
# portable extraction is repaired only when it lacks pyvenv.cfg and fails validation.
if((Test-Path -LiteralPath $python -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg'))){exit 0}
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
  if(Test-Path -LiteralPath $python -PathType Leaf){
    if(Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg')){exit 0}
    & $python -I -B -c "import agent_core, PIL, playwright, greenlet" 2>$null
    if($LASTEXITCODE -eq 0){
      if((Test-Path $marker) -and ((Get-Content -Raw $marker).Trim() -ne $m.archive_sha256)){throw 'PORTABLE_RUNTIME_UPDATE_REQUIRES_NEW_DIRECTORY'}
      exit 0
    }
  }
  if((Test-Path -LiteralPath (Join-Path $venv 'Scripts')) -or (Test-Path -LiteralPath (Join-Path $venv 'Lib'))){throw 'PORTABLE_RUNTIME_FRESH_DIRECTORY_REQUIRED'}
  $tmp=Join-Path $root ('.venv.portable-'+[guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    $archive=Join-Path $tmp 'runtime.zip'; $out=[IO.File]::Open($archive,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
    try {foreach($part in $parts){$p=Join-Path $payload $part.name;if(!(Test-Path $p)){throw 'PORTABLE_RUNTIME_PART_MISSING'};if((Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() -ne $part.sha256){throw 'PORTABLE_RUNTIME_HASH_MISMATCH'};$input=[IO.File]::OpenRead($p);try{$input.CopyTo($out)}finally{$input.Dispose()}}}finally{$out.Dispose()}
    if((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant() -ne $m.archive_sha256){throw 'PORTABLE_RUNTIME_ARCHIVE_HASH_MISMATCH'}
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($archive);try{foreach($e in $zip.Entries){if([IO.Path]::IsPathRooted($e.FullName) -or $e.FullName.Contains(':')){throw 'PORTABLE_RUNTIME_ZIP_TRAVERSAL'};$dest=[IO.Path]::GetFullPath((Join-Path $tmp $e.FullName));if(!$dest.StartsWith($tmp+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'PORTABLE_RUNTIME_ZIP_TRAVERSAL'}}[IO.Compression.ZipFileExtensions]::ExtractToDirectory($zip,$tmp)}finally{$zip.Dispose()}
    $candidate=Join-Path $tmp '.venv';$candidatePy=Join-Path $candidate 'Scripts\python.exe';if(!(Test-Path $candidatePy)){throw 'PORTABLE_RUNTIME_LAYOUT_INVALID'}
    & $candidatePy -I -B -c "import PIL, playwright, greenlet; print('PORTABLE_RUNTIME_IMPORTS_OK')";if($LASTEXITCODE -ne 0){throw 'PORTABLE_RUNTIME_IMPORT_VALIDATION_FAILED'}
    # A fresh payload directory has no runtime children. Never replace a
    # partial or user-managed runtime; install exactly Lib then Scripts.
    New-Item -ItemType Directory -Force -Path $venv | Out-Null
    Move-Item -LiteralPath (Join-Path $candidate 'Lib') -Destination $venv
    Move-Item -LiteralPath (Join-Path $candidate 'Scripts') -Destination $venv
    & $python -I -B -c "import agent_core, PIL, playwright, greenlet";if($LASTEXITCODE -ne 0){throw 'PORTABLE_RUNTIME_FINAL_VALIDATION_FAILED'}
    [IO.File]::WriteAllText($marker,$m.archive_sha256+"`n",(New-Object Text.UTF8Encoding($false)))
  } finally {Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue}
} finally {$mutex.ReleaseMutex()|Out-Null;$mutex.Dispose()}
