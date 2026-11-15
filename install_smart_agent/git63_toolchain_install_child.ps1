# Runs EXCLUSIVELY inside the dedicated Git63 Installer AppContainer.
# Parent supplies pinned, catalog-selected scalar variables, never user script.
$ErrorActionPreference='Stop'
$Target=Join-Path (Join-Path $StoreRoot $Tool) $Version
$Stage=Join-Path $StoreRoot ('.stage-'+[Guid]::NewGuid().ToString('N'))
$Zip=$Stage+'.zip'
$ChecksumFile=$Stage+'.sha256'
$PublisherChecksumVerified=$false
$SourceAlias=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($AliasesJsonBase64))
$Aliases=@($SourceAlias | ConvertFrom-Json)
$Denied=$false
try {
 [IO.File]::WriteAllText($OutsideDenialProbe,'BLOCKED_EXPECTED')
 throw 'GIT63_KERNEL_OUTSIDE_WRITE_ALLOWED'
} catch [System.UnauthorizedAccessException] {
 $Denied=$true
}
if(-not $Denied){throw 'GIT63_KERNEL_OUTSIDE_WRITE_NOT_DENIED'}
if(Test-Path -LiteralPath $Target){throw 'GIT63_EXISTING_TARGET_REPROBE_REQUIRED'}
try {
 [IO.Directory]::CreateDirectory($Stage) | Out-Null
 [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
 Invoke-WebRequest -Uri $SourceUrl -OutFile $Zip -UseBasicParsing -MaximumRedirection 5 -ErrorAction Stop | Out-Null
 if((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ine $ArchiveSHA256){
  throw 'GIT63_ARCHIVE_SHA256_MISMATCH'
 }
 if($ChecksumUrl){
  Invoke-WebRequest -Uri $ChecksumUrl -OutFile $ChecksumFile -UseBasicParsing -MaximumRedirection 5 -ErrorAction Stop | Out-Null
  if((Get-Item -LiteralPath $ChecksumFile).Length -gt 65536){throw 'GIT63_PUBLISHER_CHECKSUM_OVERSIZED'}
  $checksumText=Get-Content -LiteralPath $ChecksumFile -Raw -Encoding UTF8 -ErrorAction Stop
  $matches64=[regex]::Matches($checksumText,'(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])')
  if($matches64.Count -ne 1 -or $matches64[0].Value -ine $ArchiveSHA256){
   throw 'GIT63_PUBLISHER_CHECKSUM_MISMATCH'
  }
  $PublisherChecksumVerified=$true
 }
 Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
 Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
 $Reader=[IO.Compression.ZipFile]::OpenRead($Zip)
 try {
  if($Reader.Entries.Count -gt 200000){throw 'GIT63_ARCHIVE_ENTRIES_EXCEEDED'}
  [int64]$Total=0
  foreach($Entry in $Reader.Entries){
   $Raw=[string]$Entry.FullName
   $Parts=$Raw.Replace('\','/').TrimEnd('/').Split('/')
   if(-not $Raw -or $Raw.StartsWith('/') -or $Raw.Contains(':') -or
      @($Parts | Where-Object {$_ -in @('','.', '..')}).Count -ne 0){
    throw 'GIT63_ARCHIVE_PATH_UNSAFE'
   }
   $Mode=($Entry.ExternalAttributes -shr 16) -band 0xF000
   if($Mode -eq 0xA000){throw 'GIT63_ARCHIVE_SYMLINK_FORBIDDEN'}
   $Total+=[int64]$Entry.Length
   if($Total -gt 8589934592){throw 'GIT63_ARCHIVE_EXPANSION_LIMIT'}
   $Dest=[IO.Path]::GetFullPath((Join-Path $Stage ([string]::Join('\',$Parts))))
   if(-not $Dest.StartsWith($Stage+'\', [StringComparison]::OrdinalIgnoreCase)){
    throw 'GIT63_ARCHIVE_PATH_ESCAPE'
   }
   if($Raw.EndsWith('/')){[IO.Directory]::CreateDirectory($Dest)|Out-Null;continue}
   [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Dest))|Out-Null
   $InputStream=$Entry.Open()
   try{
    $OutputStream=[IO.File]::Open($Dest,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$InputStream.CopyTo($OutputStream)}finally{$OutputStream.Dispose()}
   }finally{$InputStream.Dispose()}
  }
 } finally {$Reader.Dispose()}
 foreach($Alias in $Aliases){
  $Exe=[IO.Path]::GetFullPath((Join-Path $Stage ([string]$Alias.relative)))
  if(-not $Exe.StartsWith($Stage+'\', [StringComparison]::OrdinalIgnoreCase) -or
     -not [IO.File]::Exists($Exe)){throw 'GIT63_INSTALLED_EXECUTABLE_MISSING'}
 }
 [IO.Directory]::CreateDirectory((Join-Path $StoreRoot $Tool))|Out-Null
 [IO.Directory]::Move($Stage,$Target)
 [ordered]@{
  schema='SMARTAGENT_GIT63_INSTALL_RECEIPT_V1';status='INSTALLED'
  tool=$Tool;version=$Version;archive_sha256=$ArchiveSHA256
  kernel_outside_write_denied=$true
  publisher_checksum_verified=$PublisherChecksumVerified
 }|ConvertTo-Json -Compress
} finally {
 try{Remove-Item -LiteralPath $Zip -Force -ErrorAction Stop}catch{}
 try{Remove-Item -LiteralPath $ChecksumFile -Force -ErrorAction Stop}catch{}
 if(Test-Path -LiteralPath $Stage){
  try{Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction Stop}catch{}
 }
}
