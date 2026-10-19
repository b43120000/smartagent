[CmdletBinding()]
param([string]$ProjectRoot=(Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot);$manifest=Join-Path $root '.venv\portable-runtime\manifest.json';$python=Join-Path $root '.venv\Scripts\python.exe'
if(!(Test-Path $manifest)){throw 'PORTABLE_RUNTIME_MANIFEST_MISSING'}
if(!(Test-Path $python)){throw 'PORTABLE_RUNTIME_NOT_EXTRACTED'}
$m=Get-Content -Raw $manifest|ConvertFrom-Json
foreach($part in @($m.parts)){ $file=Join-Path (Split-Path $manifest) $part.name;if(!(Test-Path $file)){throw 'PORTABLE_RUNTIME_PART_MISSING'};if((Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant() -ne $part.sha256){throw 'PORTABLE_RUNTIME_PART_HASH_MISMATCH'} }
& $python -I -B -c "import agent_core, PIL, greenlet, playwright; print('PORTABLE_RUNTIME_VALIDATED')";if($LASTEXITCODE -ne 0){throw 'PORTABLE_RUNTIME_IMPORT_FAILURE'}
