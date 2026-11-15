# Git50 P3: non-elevated, protected-copy execution entrance.
# NEVER runs Planner script in this host process. Only native verified
# AppContainer LaunchPowerShellCaptured may execute the user's command.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$WorkingDirectory,
    [string]$SelectedTurboRoot='',
    [Parameter(Mandatory=$true)][int]$ExpectedRevision,
    [Parameter(Mandatory=$true)][string]$TaskId,
    [Parameter(Mandatory=$true)][string]$ActionId,
    [Parameter(Mandatory=$true)][string]$CommandBase64,
    [int]$TimeoutSeconds = 30,
    [int]$MaxOutputBytes = 1048576,
    [ValidateSet("","git","python")][string]$NativeVersionProbe = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300 -or
    $MaxOutputBytes -lt 1 -or $MaxOutputBytes -gt 2097152) {
    throw 'GIT50_BROKER_LIMITS_INVALID'
}
if ([string]::IsNullOrWhiteSpace($TaskId) -or
    [string]::IsNullOrWhiteSpace($ActionId) -or
    $TaskId.Length -gt 128 -or $ActionId.Length -gt 128 -or
    $TaskId -notmatch '^[A-Za-z0-9._-]+$' -or
    $ActionId -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'GIT50_BROKER_ACTION_ID_INVALID'
}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$rootOfSource = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$secure = Join-Path $ProjectRoot 'localdata\secure\windows_security'
$expectedBrokerDir = Join-Path $secure 'turbo_broker_code'
if (-not [IO.Path]::GetFullPath($PSScriptRoot).Equals(
    [IO.Path]::GetFullPath($expectedBrokerDir),[StringComparison]::OrdinalIgnoreCase)) {
    throw 'GIT50_BROKER_NOT_RUNNING_FROM_PROTECTED_CODE_ROOT'
}
$modePath = Join-Path $secure 'acl_mode.json'
$policyPath = Join-Path $secure 'workspace_access_policy.json'
$legacyJournal = Join-Path $secure 'turbo_os_activation.json'
$policyPreview = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$rootsPreview = @($policyPreview.turbo_roots)
if ($rootsPreview.Count -gt 1) {
    if (-not [IO.Path]::IsPathRooted($SelectedTurboRoot)) {
        throw 'GIT62_NATIVE_SELECTED_ROOT_REQUIRED'
    }
    $chosen=[IO.Path]::GetFullPath($SelectedTurboRoot).TrimEnd('\')
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        $key=[BitConverter]::ToString($sha.ComputeHash(
          [Text.Encoding]::UTF8.GetBytes($chosen.ToUpperInvariant()))).Replace('-','').ToLowerInvariant()
    } finally {$sha.Dispose()}
    $journalPath=Join-Path $secure ('turbo_root_activations\activation-'+$key+'.json')
} else {
    $journalPath=$legacyJournal
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf) -and
        $rootsPreview.Count -eq 1) {
        $chosen=[IO.Path]::GetFullPath([string]$rootsPreview[0]).TrimEnd('\')
        $sha=[Security.Cryptography.SHA256]::Create()
        try {$key=[BitConverter]::ToString($sha.ComputeHash(
             [Text.Encoding]::UTF8.GetBytes($chosen.ToUpperInvariant()))).Replace('-','').ToLowerInvariant()}
        finally{$sha.Dispose()}
        $journalPath=Join-Path $secure ('turbo_root_activations\activation-'+$key+'.json')
    }
}
$pendingSwitchPath = Join-Path $secure 'turbo_existing_root_switch.json'
foreach($item in @($modePath,$policyPath,$journalPath)) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw 'GIT50_OS_BROKER_PROTECTED_STATE_MISSING'
    }
}
function Load-State([string]$Path) {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop)
}
function Get-Pinned-Native-Toolchain {
    # The protected security profile is checked on EVERY run. Do not use the
    # controller PATH, even if it already contains Git/Python.
    $profile=Load-State (Join-Path $secure 'security_profile.json')
    if ([string]$profile.schema -notin @(
        'SMARTAGENT_WINDOWS_SECURITY_V1','SMARTAGENT_WINDOWS_SECURITY_V2')) {
        throw 'GIT52_NATIVE_TOOLCHAIN_PROFILE_INVALID'
    }
    $verified=New-Object 'System.Collections.Generic.List[string]'
    $toolNames=New-Object 'System.Collections.Generic.List[string]'
    foreach ($candidate in @(
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
        (Join-Path $ProjectRoot '.venv\git\cmd\git.exe')
    )) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $full=[IO.Path]::GetFullPath($candidate)
        $relative=$full.Substring($ProjectRoot.Length).TrimStart([char]92)
        $walk=$ProjectRoot
        foreach ($part in $relative.Split([char]92)) {
            if ([string]::IsNullOrEmpty($part)) { continue }
            $walk=Join-Path $walk $part
            if ((Get-Item -LiteralPath $walk -Force).Attributes -band
                [IO.FileAttributes]::ReparsePoint) {
                throw 'GIT52_NATIVE_TOOLCHAIN_REPARSE_FORBIDDEN'
            }
        }
        $pins=@($profile.trusted_executables | Where-Object {
            -not [string]::IsNullOrWhiteSpace([string]$_.path) -and
            [IO.Path]::GetFullPath([string]$_.path).Equals(
                $full,[StringComparison]::OrdinalIgnoreCase) -and
            [string]$_.kind -ceq 'TURBO_NATIVE_TOOLCHAIN'
        })
        if ($pins.Count -eq 0) { continue }

        if ($pins.Count -ne 1 -or [string]$pins[0].sha256 -notmatch '^[a-fA-F0-9]{64}$') {
            throw 'GIT52_NATIVE_TOOLCHAIN_PIN_INVALID'
        }
        $digest=(Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
        if (-not $digest.Equals([string]$pins[0].sha256,
                                [StringComparison]::OrdinalIgnoreCase)) {
            throw 'GIT52_NATIVE_TOOLCHAIN_SHA256_MISMATCH'
        }
        $verified.Add([IO.Path]::GetDirectoryName($full))
        $toolNames.Add([IO.Path]::GetFileNameWithoutExtension($full).ToLowerInvariant())
    }
    # Git63: every task uses the same immutable managed-tool pointers. Re-hash
    # each executable before admitting it to the AppContainer child PATH.
    $javaHome=''
    $androidSdkRoot=''
    $store=Join-Path $ProjectRoot 'localdata\toolchains'
    $ledgerFile=Join-Path $ProjectRoot 'localdata\metadata\environment_ledger.json'
    if(Test-Path -LiteralPath $ledgerFile -PathType Leaf){
        $ledger=Load-State $ledgerFile
        if($ledger.schema -ne 'SMARTAGENT_ENVIRONMENT_LEDGER_V1'){
            throw 'GIT63_NATIVE_LEDGER_SCHEMA_INVALID'
        }
        if($ledger.PSObject.Properties.Name -contains 'managed_tools'){
            if($ledger.PSObject.Properties.Name -notcontains 'managed_sources'){
                throw 'GIT63_NATIVE_MANAGED_SOURCE_MISSING'
            }
            foreach($record in @($ledger.managed_tools.PSObject.Properties)){
                $entry=$record.Value
                $tool=[string]$entry.tool
                $version=[string]$entry.version
                $alias=[string]$entry.alias
                $key=$tool+':'+$version
                if($tool -cnotmatch '^[a-z][a-z0-9_-]{0,31}$' -or
                   $version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$' -or
                   $alias -cnotmatch '^[a-z][a-z0-9_-]{0,31}$' -or
                   $ledger.managed_sources.PSObject.Properties.Name -cnotcontains $key){
                    throw 'GIT63_NATIVE_MANAGED_IDENTITY_INVALID'
                }
                $source=$ledger.managed_sources.PSObject.Properties[$key].Value
                if([string]$entry.archive_sha256 -ine [string]$source.archive_sha256 -or
                   $source.executables.PSObject.Properties.Name -cnotcontains $alias){
                    throw 'GIT63_NATIVE_MANAGED_SOURCE_MISMATCH'
                }
                $home=[IO.Path]::GetFullPath((Join-Path (Join-Path $store $tool) $version)).TrimEnd('\')
                $full=[IO.Path]::GetFullPath([string]$entry.executable)
                $relative=[string]$source.executables.PSObject.Properties[$alias].Value
                $expected=[IO.Path]::GetFullPath((Join-Path $home $relative))
                if(-not $full.Equals($expected,[StringComparison]::OrdinalIgnoreCase) -or
                   -not $full.StartsWith($home+'\', [StringComparison]::OrdinalIgnoreCase) -or
                   [string]$entry.executable_sha256 -cnotmatch '^[0-9a-fA-F]{64}$'){
                    throw 'GIT63_NATIVE_MANAGED_PATH_INVALID'
                }
                $walk=$store
                foreach($part in $full.Substring($store.Length).TrimStart([char]92).Split([char]92)){
                    if(-not $part){continue}
                    $walk=Join-Path $walk $part
                    if((Get-Item -LiteralPath $walk -Force -ErrorAction Stop).Attributes -band
                       [IO.FileAttributes]::ReparsePoint){
                        throw 'GIT63_NATIVE_MANAGED_REPARSE_FORBIDDEN'
                    }
                }
                if((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash -ine
                   [string]$entry.executable_sha256){
                    throw 'GIT63_NATIVE_MANAGED_SHA256_MISMATCH'
                }
                $verified.Add([IO.Path]::GetDirectoryName($full))
                $toolNames.Add($alias)
                if($alias -ceq 'java'){
                    $javaHome=[IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($full))
                }
                if($alias -ceq 'adb'){
                    $androidSdkRoot=[IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($full))
                }
            }
        }
    }
    return @{
        path=([string]::Join(';',[string[]]$verified.ToArray()))
        tools=@($toolNames.ToArray())
        trusted_count=$verified.Count
        java_home=$javaHome
        android_sdk_root=$androidSdkRoot
    }
}

function Assert-Trusted-State {
    if (Test-Path -LiteralPath $pendingSwitchPath) {
        throw 'GIT50_BROKER_SWITCH_INCOMPLETE_NO_NATIVE_ACTION'
    }
    $mode = Load-State $modePath
    $policy = Load-State $policyPath
    $activation = Load-State $journalPath
    if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
        $mode.mode -ne 'on' -or
        -not [IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd('\').Equals(
            $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_ACL_NOT_ON'
    }
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
        $policy.turbo_requested -ne $true -or
        [int]$policy.revision -ne $ExpectedRevision) {
        throw 'GIT50_BROKER_POLICY_STALE_OR_DISABLED'
    }
    # Global revision is checked above as command CAS, not a sibling's grant.
    $epoch=$ExpectedRevision
    if($policy.PSObject.Properties.Name -contains 'turbo_root_epochs') {
        $matching=@($policy.turbo_root_epochs | Where-Object {
            $_.root -and [IO.Path]::GetFullPath([string]$_.root).TrimEnd('\').Equals(
                [IO.Path]::GetFullPath([string]$activation.turbo_root).TrimEnd('\'),
                [StringComparison]::OrdinalIgnoreCase)
        })
        if($matching.Count -ne 1 -or [int]$matching[0].epoch -lt 1){
            throw 'GIT62_BROKER_ROOT_EPOCH_UNTRUSTED'
        }
        $epoch=[int]$matching[0].epoch
    }
    if ($activation.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
        $activation.status -ne 'ACTIVE_PROVISIONED' -or
        [int]$activation.policy_revision -ne $epoch -or
        -not [IO.Path]::GetFullPath([string]$activation.install_root).TrimEnd('\').Equals(
            $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_OS_ACTIVATION_STALE_OR_INVALID'
    }
    $workspace = [IO.Path]::GetFullPath($Workspace).TrimEnd('\')
    $root = [IO.Path]::GetFullPath([string]$activation.turbo_root).TrimEnd('\')
    $activatedWorkspace = [IO.Path]::GetFullPath([string]$activation.workspace_root).TrimEnd('\')
    if ($activation.PSObject.Properties.Name -contains 'existing_root_import' -and
        $activation.existing_root_import -eq $true -and
        ($activation.PSObject.Properties.Name -notcontains 'existing_import_committed' -or
         $activation.existing_import_committed -ne $true)) {
        throw 'GIT62_BROKER_IMPORT_NOT_COMMITTED'
    }
    if (-not $activatedWorkspace.Equals($workspace,[StringComparison]::OrdinalIgnoreCase) -or
        -not @($policy.writable_workspaces | Where-Object {
            [IO.Path]::GetFullPath([string]$_).TrimEnd('\').Equals(
                $workspace,[StringComparison]::OrdinalIgnoreCase)
        }).Count -or
        -not @($policy.turbo_roots | Where-Object {
            [IO.Path]::GetFullPath([string]$_).TrimEnd('\').Equals(
                $root,[StringComparison]::OrdinalIgnoreCase)
        }).Count -or
        ($SelectedTurboRoot -and
         -not [IO.Path]::GetFullPath($SelectedTurboRoot).TrimEnd('\').Equals(
             $root,[StringComparison]::OrdinalIgnoreCase))) {
        throw 'GIT50_BROKER_SCOPE_POLICY_MISMATCH'
    }
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw 'GIT50_BROKER_ROOT_NOT_DIRECTORY'
    }
    $rawCwd = $WorkingDirectory.Replace('/','\')
    if (@($rawCwd.Split('\') | Where-Object {$_ -eq '..'}).Count -gt 0) {
        throw 'GIT50_BROKER_CWD_PARENT_TRAVERSAL'
    }
    $cwd = [IO.Path]::GetFullPath($WorkingDirectory).TrimEnd('\')
    if (-not $cwd.Equals($root,[StringComparison]::OrdinalIgnoreCase) -and
        -not $cwd.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_CWD_OUTSIDE_ROOT'
    }
    if (-not (Test-Path -LiteralPath $cwd -PathType Container)) {
        throw 'GIT50_BROKER_CWD_NOT_DIRECTORY'
    }
    $current = $root
    foreach ($part in @('') + @($cwd.Substring($root.Length).TrimStart([char]92).Split('\'))) {
        if (-not [string]::IsNullOrWhiteSpace($part)) {
            $current = Join-Path $current $part
        }
        if ((Get-Item -LiteralPath $current -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
            throw 'GIT50_BROKER_REPARSE_CWD_FORBIDDEN'
        }
    }
    $sentinel = [IO.Path]::GetFullPath([string]$activation.negative_sentinel_path)
    $parent = [IO.Path]::GetDirectoryName($root)
    if (-not [IO.Path]::GetDirectoryName($sentinel).Equals(
        $parent,[StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($sentinel).StartsWith('.git50-os-denial-') -or
        -not (Test-Path -LiteralPath $sentinel -PathType Leaf) -or
        [IO.File]::ReadAllText($sentinel) -cne 'OUTSIDE_SENTINEL') {
        throw 'GIT50_BROKER_NEGATIVE_SENTINEL_UNVERIFIED'
    }
    if ([string]$activation.package_sid -notmatch '^S-1-15-2-') {
        throw 'GIT50_BROKER_PACKAGE_SID_INVALID'
    }
    return @{
        mode=$mode; policy=$policy; activation=$activation
        workspace=$workspace; root=$root; cwd=$cwd; sentinel=$sentinel
    }
}
# Broker execution and ACL OFF revocation use the same global mutex.
$hash=[Security.Cryptography.SHA256]::Create()
try {
    $digest=[BitConverter]::ToString(
        $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant()))
    ).Replace('-','')
} finally { $hash.Dispose() }
$mutex=New-Object Threading.Mutex($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
try {
    try { $held=$mutex.WaitOne([TimeSpan]::FromSeconds(305)) }
    catch [Threading.AbandonedMutexException] { $held=$true }
    if (-not $held) { throw 'GIT50_BROKER_MUTEX_TIMEOUT' }
    $state=Assert-Trusted-State
    if (-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
    }
    $moniker=[string]$state.activation.moniker
    $sid=[SmartAgentGit47AppContainerProbe]::DeriveSid($moniker)
    if (-not $sid.Equals([string]$state.activation.package_sid,
                         [StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_CHILD_SID_NOT_BOUND_TO_ACTIVATION'
    }
    # Independently confirm the *real* AppContainer cannot read the known
    # outside sentinel, and can write a disposable marker inside. This is a
    # kernel denial probe, NOT a text/Planner/JSON assertion.
    $outsideLiteral=$state.sentinel.Replace("'","''")
    # Git62: each OS process must be denied a write handle in EVERY other
    # nominated Turbo Root, not just the parent sentinel.
    $crossRootProof=''
    foreach($other in @($state.policy.turbo_roots)){
        $alternate=[IO.Path]::GetFullPath([string]$other).TrimEnd('\')
        if($alternate.Equals([string]$state.root,[StringComparison]::OrdinalIgnoreCase)){
            continue
        }
        if(-not (Test-Path -LiteralPath $alternate -PathType Container) -or
           ((Get-Item -LiteralPath $alternate -Force).Attributes -band
               [IO.FileAttributes]::ReparsePoint)){
            throw 'GIT62_CROSS_ROOT_PROBE_TARGET_UNAVAILABLE'
        }
        $candidate=(Join-Path $alternate ('.git62-crossroot-denial-'+
             [Guid]::NewGuid().ToString('N'))).Replace("'","''")
        $block=@'
try {
    $crossHandle=[IO.File]::Open('__CROSS_ROOT_PATH__',
        [IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    $crossHandle.Dispose()
    [IO.File]::Delete('__CROSS_ROOT_PATH__')
    exit 96
} catch [System.UnauthorizedAccessException] {
    # The distinct Root denies this SID.
} catch {exit 97}
'@
        $crossRootProof+=$block.Replace('__CROSS_ROOT_PATH__',$candidate)
    }
    $proof=@"
`$readDenied=`$false
try { [void][IO.File]::ReadAllText('$outsideLiteral') }
catch [System.UnauthorizedAccessException] { `$readDenied=`$true }
if (-not `$readDenied) { exit 91 }
# Open the existing outside sentinel for WRITE access, but NEVER write data.
# A successful handle is a kernel escape even if no bytes were modified.
try {
    `$outsideHandle=[IO.File]::Open('$outsideLiteral',[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite)
    `$outsideHandle.Dispose()
    exit 92
} catch [System.UnauthorizedAccessException] {
$crossRootProof
    [IO.File]::WriteAllText('git50_scope_proof.tmp','OS_DENIED')
    exit 0
}
"@
    $os=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        $moniker,$proof,$state.root,[uint32]30000) | ConvertFrom-Json
    if ($os.sandbox_identity_verified -ne $true -or
        $os.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        $os.internet_client_token_verified -ne $true -or
        [int]$os.exit_code -ne 0 -or
        [IO.File]::ReadAllText((Join-Path $state.root 'git50_scope_proof.tmp')) -cne 'OS_DENIED') {
        throw 'GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED'
    }
    Remove-Item -LiteralPath (Join-Path $state.root 'git50_scope_proof.tmp') -Force
    $state=Assert-Trusted-State
    try {
        $script=[Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($CommandBase64))
    } catch { throw 'GIT50_BROKER_COMMAND_BASE64_INVALID' }
    if ([string]::IsNullOrWhiteSpace($script) -or $script.Length -gt 8192) {
        throw 'GIT50_BROKER_SCRIPT_LENGTH_INVALID'
    }
    $toolchain=Get-Pinned-Native-Toolchain
    # Git59: a native child-token probe is only permitted for a literal
    # --version invocation of the operator-pinned and hash-checked executable.
    # All other scripts retain their existing behavior. This host-side code
    # NEVER executes a child without the AppContainer CreateProcess launcher.
    $git59ProbeExecutable=''
    if ($NativeVersionProbe -ne '') {
        if (@($toolchain.tools | Where-Object {$_ -ceq $NativeVersionProbe}).Count -ne 1) {
            throw 'GIT59_PINNED_VERSION_EXECUTABLE_NOT_ENROLLED'
        }
        $relative = if ($NativeVersionProbe -ceq 'git') {
            '.venv\git\cmd\git.exe'
        } else {
            '.venv\Scripts\python.exe'
        }
        $git59ProbeExecutable=[IO.Path]::GetFullPath((Join-Path $ProjectRoot $relative))
        $literalCommand="& '" + $git59ProbeExecutable + "' --version"
        if (-not $script.Trim().Equals($literalCommand,
                      [StringComparison]::OrdinalIgnoreCase)) {
            throw 'GIT59_NATIVE_VERSION_COMMAND_NOT_EXACT_PINNED_LITERAL'
        }
    }
    # The native command is the *only* path that receives untrusted script.
    # The elevated/setup PowerShell code must never invoke the command.
    # Environment is injected into the native child only, never the OS PATH.
    # P0: stage markers are generated by the protected Broker and consumed
    # solely as diagnostic metadata, never as an authorization assertion.
    $envSetup='$script:smartagentStage=''ENV_SETUP''; '+
        '$script:smartagentSubstage=''ENV_APPLY''; '+
        '[void]$script:smartagentTrace.Add(''ENV_APPLY.START''); '
    if(-not [string]::IsNullOrWhiteSpace([string]$toolchain.java_home)){
        $literal=([string]$toolchain.java_home).Replace("'","''")
        $envSetup+="[Environment]::SetEnvironmentVariable('JAVA_HOME','$literal');"
    }
    if(-not [string]::IsNullOrWhiteSpace([string]$toolchain.android_sdk_root)){
        $literal=([string]$toolchain.android_sdk_root).Replace("'","''")
        $envSetup+="[Environment]::SetEnvironmentVariable('ANDROID_HOME','$literal');"
        $envSetup+="[Environment]::SetEnvironmentVariable('ANDROID_SDK_ROOT','$literal');"
    }
    $managedRoot=(Join-Path $ProjectRoot 'localdata\toolchains').Replace("'","''")
    $envSetup+="[Environment]::SetEnvironmentVariable('SMARTAGENT_TOOLCHAIN_ROOT','$managedRoot');"
    $gradleHome=(Join-Path $state.root '.gradle').Replace("'","''")
    $envSetup+="[Environment]::SetEnvironmentVariable('GRADLE_USER_HOME','$gradleHome');"
    # Git63: PWD synchronization belongs to the admitted user TASK,
    # not to every AppContainer verification / installer probe.
    # The broker has already verified state.cwd is the exact authorized root.
    $cwdLiteral=([string]$state.cwd).Replace("'","''")
    # The native CreateProcessW call has already bound its OS CWD to the
    # exact admitted directory. Avoid a redundant PowerShell provider rebind
    # if Get-Location already agrees: re-entering a root can need parent
    # traversal rights that an AppContainer intentionally does not possess.
    # If it does NOT agree, Set-Location is still mandatory and fail-closed.
    # Fixed-command step trace. No path values, environment contents or
    # arbitrary Planner text is copied into the diagnostic event names.
    # Every step records START before the potentially failing operation.
    $taskCwdSetup=@'
$script:smartagentStage='CWD_SETUP'
$script:smartagentSubstage='CWD_GET_LOCATION_INITIAL'
[void]$script:smartagentTrace.Add('CWD_GET_LOCATION_INITIAL.START')
$script:initialProviderCwd=(Get-Location).Path
[void]$script:smartagentTrace.Add('CWD_GET_LOCATION_INITIAL.PASS')
$script:smartagentSubstage='CWD_SET_LOCATION'
if(-not [string]::Equals($script:initialProviderCwd,'__CWD_LITERAL__',[StringComparison]::OrdinalIgnoreCase)) {
    [void]$script:smartagentTrace.Add('CWD_SET_LOCATION.START')
    Set-Location -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop
    [void]$script:smartagentTrace.Add('CWD_SET_LOCATION.PASS')
} else {
    [void]$script:smartagentTrace.Add('CWD_SET_LOCATION.SKIP')
}
$script:smartagentSubstage='CWD_GET_LOCATION_VERIFY'
[void]$script:smartagentTrace.Add('CWD_GET_LOCATION_VERIFY.START')
$script:verifiedProviderCwd=(Get-Location).Path
[void]$script:smartagentTrace.Add('CWD_GET_LOCATION_VERIFY.PASS')
$script:smartagentSubstage='CWD_GET_NATIVE_CURRENT_DIRECTORY'
[void]$script:smartagentTrace.Add('CWD_GET_NATIVE_CURRENT_DIRECTORY.START')
$script:nativeProcessCwd=[Environment]::CurrentDirectory
[void]$script:smartagentTrace.Add('CWD_GET_NATIVE_CURRENT_DIRECTORY.PASS')
$script:smartagentSubstage='CWD_COMPARE'
[void]$script:smartagentTrace.Add('CWD_COMPARE.START')
if(-not [string]::Equals($script:verifiedProviderCwd,'__CWD_LITERAL__',[StringComparison]::OrdinalIgnoreCase) -or
   -not [string]::Equals($script:nativeProcessCwd,'__CWD_LITERAL__',[StringComparison]::OrdinalIgnoreCase)){
    throw 'GIT63_NATIVE_TASK_CWD_MISMATCH'
}
[void]$script:smartagentTrace.Add('CWD_COMPARE.PASS')
'@.Replace('__CWD_LITERAL__',$cwdLiteral)
    $envSetup+="[void]"+'$script:smartagentTrace.Add(''ENV_APPLY.PASS''); '
    $envSetup+='$script:smartagentStage=''COMMAND_EXECUTE''; '+
        '$script:smartagentSubstage=''COMMAND_DISPATCH''; '+
        '[void]$script:smartagentTrace.Add(''COMMAND_DISPATCH.START''); '
    $nativeScript=$taskCwdSetup+[Environment]::NewLine+$envSetup+
        [Environment]::NewLine+$script+[Environment]::NewLine+
        '[void]$script:smartagentTrace.Add(''COMMAND_DISPATCH.FINISH'');'
    $nativeRaw=[SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured(
        $moniker,$nativeScript,$state.cwd,[uint32]($TimeoutSeconds*1000),
        [int]$MaxOutputBytes,[string]$toolchain.path)
    $native=$nativeRaw | ConvertFrom-Json -ErrorAction Stop

    # P2: the protected C# Broker sampled a previously verified Win32 process
    # handle while retaining the KILL_ON_JOB_CLOSE job. This is historical
    # RUNNING evidence, NOT a current RUNNING status after command exit.
    $nativePid=[int64]$native.pid
    $nativeRunningSamples=[int]$native.native_running_sample_count
    $nativeRunningAt=[double]$native.native_last_running_observed_at
    $nativeRunningEvidence=@{}
    if ($nativePid -le 0 -or $nativeRunningSamples -lt 0 -or
        $nativeRunningSamples -gt 150 -or
        ($nativeRunningSamples -eq 0 -and $nativeRunningAt -ne 0) -or
        ($nativeRunningSamples -gt 0 -and
         ($nativeRunningAt -le 0 -or
          $native.native_running_evidence_source -cne
             'NATIVE_WIN32_WAIT_FOR_SINGLE_OBJECT'))) {
        throw 'GIT63_NATIVE_RUNNING_EVIDENCE_INVALID'
    }
    if ($nativeRunningSamples -gt 0) {
        $nativeRunningEvidence=[ordered]@{
            schema='SMARTAGENT_PROCESS_LIVENESS_V1'
            execution_id=$ActionId
            process_state='RUNNING'
            liveness_verified=$true
            process_identity_verified=$true
            evidence_source='NATIVE_WIN32_WAIT_FOR_SINGLE_OBJECT'
            pid=$nativePid
            observed_at=$nativeRunningAt
            running_sample_count=$nativeRunningSamples
            child_process_policy='JOB_KILL_ON_CLOSE'
            terminal_eligible=$false
        }
    }

    # P0: exact PowerShell stage is diagnostic only. A sandboxed user command
    # can produce arbitrary text, so the stage never grants permission or
    # upgrades failed/unknown task evidence to SUCCEEDED.
    $nativeFailureStage=''
    $nativeExceptionType=''
    $nativeFailureStageSource='NONE'
    if ([int]$native.exit_code -eq 95) {
        $nativeFailureStage='OUTPUT_CAPTURE'
        $nativeFailureStageSource='WRAPPER_RESERVED_EXIT_CODE'
    } elseif ([int]$native.exit_code -ne 0) {
        try {
            $text=[Text.Encoding]::UTF8.GetString(
                [Convert]::FromBase64String([string]$native.stderr_base64))
            $match=[regex]::Match($text,
                '^SMARTAGENT_NATIVE_FAILURE_STAGE=(CWD_SETUP|ENV_SETUP|COMMAND_EXECUTE);([A-Za-z][A-Za-z0-9.]*Exception):')
            if ($match.Success) {
                $nativeFailureStage=$match.Groups[1].Value
                $nativeExceptionType=$match.Groups[2].Value
                $nativeFailureStageSource='SANDBOX_WRAPPER_DIAGNOSTIC'
            }
        } catch { }
    }
    if ($native.sandbox_identity_verified -ne $true -or
        $native.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
        $native.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        $native.os_process_started -ne $true -or
        $native.output_is_authoritative -ne $false -or
        $native.internet_client_token_verified -ne $true -or
        $native.internet_client_capability_sid -ne 'S-1-15-3-1') {
        throw 'GIT50_BROKER_KERNEL_PROCESS_RECEIPT_INVALID'
    }
    # The Git/Python executable is launched a SECOND TIME, directly by
    # CreateProcessW with explicit AppContainer security capabilities. Unlike
    # the first PowerShell-wrapped version command, the returned kernel token
    # receipt proves the executable process identity itself, independently.
    $git59Child=@{}
    $git59Verified=$false
    if ($NativeVersionProbe -ne '') {
        try {
            $git59Raw=[SmartAgentGit47AppContainerProbe]::LaunchVersionCaptured(
                $moniker,$git59ProbeExecutable,$state.cwd,
                [uint32]($TimeoutSeconds*1000),[string]$toolchain.path)
            $git59Os=$git59Raw | ConvertFrom-Json -ErrorAction Stop
            if ($git59Os.schema -ne 'SMARTAGENT_GIT47_APP_CONTAINER_PROCESS_V1' -or
                $git59Os.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
                $git59Os.sandbox_identity_verified -ne $true -or
                $git59Os.internet_client_token_verified -ne $true -or
                $git59Os.internet_client_capability_sid -ne 'S-1-15-3-1' -or
                $git59Os.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
                $git59Os.os_process_started -ne $true -or
                [int64]$git59Os.pid -le 0) {
                throw 'GIT59_NATIVE_CHILD_TOKEN_UNVERIFIED'
            }
            $git59Verified=$true
            $git59Child=[ordered]@{
                schema='SMARTAGENT_GIT59_DIRECT_NATIVE_CHILD_V1'
                status='OS_TOKEN_VERIFIED'
                executable_name=$NativeVersionProbe
                executable_path=$git59ProbeExecutable
                sandbox_backend='WIN32_APPCONTAINER'
                sandbox_identity_verified=$true
                internet_client_token_verified=$true
                internet_client_capability_sid='S-1-15-3-1'
                child_process_policy='JOB_KILL_ON_CLOSE'
                os_process_started=$true
                pid=[int64]$git59Os.pid
                exit_code=[int64]$git59Os.exit_code
                git60_child_stdout_base64=[string]$git59Os.git60_child_stdout_base64
                git60_child_stderr_base64=[string]$git59Os.git60_child_stderr_base64
                git60_child_stdout_bytes=[int64]$git59Os.git60_child_stdout_bytes
                git60_child_stderr_bytes=[int64]$git59Os.git60_child_stderr_bytes
                git60_child_capture_complete=[bool]$git59Os.git60_child_capture_complete
            }
        } catch {
            # Preserve the first shell result; an independently unverifiable
            # native process never receives a success token attestation.
            $reason='GIT59_NATIVE_CHILD_CREATEPROCESS_FAILED'
            if ($_.Exception -is [TimeoutException]) {
                $reason='GIT59_NATIVE_CHILD_EXECUTION_TIMEOUT'
            } elseif ([string]$_.Exception.Message -eq
                       'GIT59_NATIVE_CHILD_TOKEN_UNVERIFIED') {
                $reason='GIT59_NATIVE_CHILD_TOKEN_UNVERIFIED'
            }
            $git59Child=[ordered]@{
                status='UNVERIFIED'
                reason_code=$reason
            }
        }
    }
    $fresh=Assert-Trusted-State
    $response=[ordered]@{
        schema='SMARTAGENT_TURBO_OS_EXECUTION_RECEIPT_V1'
        task_id=$TaskId
        action_id=$ActionId
        policy_revision=$ExpectedRevision
        workspace_root=[string]$state.workspace
        turbo_root=[string]$state.root
        working_directory=[string]$state.cwd
        acl_mode='on'
        sandbox_backend='WIN32_APPCONTAINER'
        sandbox_identity_verified=$true
        child_process_policy='JOB_KILL_ON_CLOSE'
        kernel_outside_read_denied=$true
        kernel_outside_write_denied=$true
        internet_client_token_verified=$true
        internet_client_capability_sid='S-1-15-3-1'
        native_toolchain_status=$(if($toolchain.trusted_count -gt 0){'PINNED'}else{'NOT_ENROLLED'})
        native_trusted_tool_count=[int]$toolchain.trusted_count
        native_trusted_tools=@($toolchain.tools)
        native_direct_child_probe_kind=[string]$NativeVersionProbe
        native_direct_child_attestation=$git59Child
        native_executable_identity_verified=[bool]$git59Verified
        native_process_pid=$nativePid
        native_running_sample_count=$nativeRunningSamples
        native_last_running_evidence=$nativeRunningEvidence
        native_failure_stage=$nativeFailureStage
        native_exception_type=$nativeExceptionType
        native_failure_stage_source=$nativeFailureStageSource
        process_started=$true
        executed=$true
        command_exit_code=[int64]$native.exit_code
        stdout_base64=[string]$native.stdout_base64
        stderr_base64=[string]$native.stderr_base64
        stdout_bytes=[int64]$native.stdout_bytes
        stderr_bytes=[int64]$native.stderr_bytes
        stdout_truncated=[bool]$native.stdout_truncated
        stderr_truncated=[bool]$native.stderr_truncated
        output_is_authoritative=$false
        authorization_scope='THIS_SINGLE_APP_CONTAINER_ACTION'
        production_turbo_full_access_attested=$false
        status='EXECUTED_IN_OS_APPCONTAINER'
    }
    # Only a direct OS-spawn receipt can mark *this action* executed; the
    # global Turbo setting is not promoted by this JSON response.
    $response | ConvertTo-Json -Compress -Depth 8
}
finally {
    if ($held) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
