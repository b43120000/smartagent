@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "SA_COLLECT_SCRIPT=%~f0"
set "SA_COLLECT_ROOT=%~dp0"
set "SA_COLLECT_TASK=%~1"
set "SA_COLLECT_REQUEST=%~2"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:SA_COLLECT_SCRIPT); $m = '# POWERSHELL_BEGIN'; $p = $s.LastIndexOf($m, [StringComparison]::Ordinal); if ($p -lt 0) { throw 'Collector script marker missing' }; & ([ScriptBlock]::Create($s.Substring($p + $m.Length)))"
set "SA_COLLECT_EXIT=%ERRORLEVEL%"
if not "%SA_COLLECT_EXIT%"=="0" echo [COLLECT] Failed. Run: collect.bat TASK-... RR-...
exit /b %SA_COLLECT_EXIT%
# POWERSHELL_BEGIN
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($env:SA_COLLECT_ROOT)
$task = [string]$env:SA_COLLECT_TASK
$request = [string]$env:SA_COLLECT_REQUEST
$baseSha = '2f91c3a0d88b6db16b5563c22792996a9103cb9d'

function Assert-Id([string]$id, [string]$kind) {
    if ($id -and $id -notmatch '^[A-Za-z0-9_-]{1,160}$') {
        throw "Invalid $kind identifier; only letters, digits, hyphen and underscore are allowed."
    }
}
function Read-Json([string]$path) {
    if (-not [IO.File]::Exists($path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop) }
    catch { return $null }
}
function Safe-Code($value) {
    if ($null -eq $value) { return $null }
    $s = [string]$value
    if ($s -match '^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}$') { return $s }
    return '[REDACTED_NONCODE]'
}
function Safe-CodeList($items) {
    $result = @()
    foreach ($v in @($items)) {
        if ($null -ne $v -and $v -isnot [pscustomobject]) { $result += (Safe-Code $v) }
    }
    return ,@($result | Select-Object -First 40)
}
function Hash-Text([string]$text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}
function Hash-File([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}
function Write-Json([string]$path, $object) {
    $json = ConvertTo-Json -InputObject $object -Depth 16
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding $false))
}

Assert-Id $task 'task'
Assert-Id $request 'request'
if (-not (Test-Path -LiteralPath (Join-Path $root 'source\WebAgent\protocol_loop.py') -PathType Leaf)) {
    throw 'Place collect.bat at the root of the SmartAgent checkout used by the Live runtime.'
}
$progressDir = Join-Path $root 'localdata\runtime\task_progress'
$selection = 'explicit'
if (-not $task -and -not $request) {
    $latest = Get-ChildItem -LiteralPath $progressDir -Filter 'TASK-*.json' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) { throw 'No Task Progress ledger found. Run collect.bat TASK-... RR-... with the IDs from the Live notification.' }
    $task = $latest.BaseName
    $selection = 'newest_task_progress'
}
Assert-Id $task 'task'
$ledgerPath = if ($task) { Join-Path $progressDir ($task + '.json') } else { '' }
$ledger = if ($ledgerPath) { Read-Json $ledgerPath } else { $null }
if (-not $request -and $null -ne $ledger -and $ledger.request_id) {
    $request = [string]$ledger.request_id
}
Assert-Id $request 'request'
if (-not $task -and -not $request) { throw 'Need a Task ID or Request ID.' }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmss')
$outputDir = Join-Path $root 'localdata\diagnostics\gar_live_rca'
[IO.Directory]::CreateDirectory($outputDir) | Out-Null
$stage = Join-Path $outputDir ('stage_' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($stage) | Out-Null
$sources = @(
    @{ name = 'webagent_runtime'; path = (Join-Path $root 'localdata\logs\webagent_runtime.jsonl') },
    @{ name = 'remote_runtime'; path = (Join-Path $root 'localdata\logs\remote_runtime.jsonl') },
    @{ name = 'tri_one_runtime_events'; path = (Join-Path $root 'localdata\runtime\runtime_events.jsonl') },
    @{ name = 'webgpt_outbound'; path = (Join-Path $root 'localdata\runtime\webgpt_outbound_turns.jsonl') }
)
$scalarKeys = @(
    'action_id','tool','operation','verification_status','result_status','effective_verification_status',
    'execution_status','last_reason','repair_status','diagnostic_reason','error_type',
    'pid','round','attempt','request_id','task_id','task_epoch',
    'execution_state','protocol_state','status','level','decision',
    'reason','reason_code','classification','marker','evidence_marker_class',
    'rejected_turns','rejected_rounds','rejected_turns_total',
    'rejected_turns_without_new_evidence','local_rejected_total',
    'max_global_rejections','max_without_evidence','max_total','max_same_reason',
    'evidence_generation','rebase_count','replan_count','fresh_session_count',
    'local_repair_exhausted','terminal_precondition_advisory',
    'unadmitted_progress_repair_advisory','preserved_action_count',
    'executed_action_count','recorded_result_count','evidence_ref_count',
    'action_history_present','condition_history_present',
    'current_step','total_steps','repair_scope','previous_ack_id',
    'revision','node_count','graph_digest','checkpoint_name','same_blocker','fresh_repair_attempts','elapsed_sec','elapsed_seconds'
)
$numericTypes = @('System.Int32','System.Int64','System.Double','System.Decimal','System.Single')
$timeline = New-Object 'System.Collections.Generic.List[object]'
$checkpointNames = New-Object 'System.Collections.Generic.HashSet[string]'
$sourceInfo = New-Object 'System.Collections.Generic.List[object]'
$warnings = New-Object 'System.Collections.Generic.List[string]'

try {
    foreach ($source in $sources) {
        $path = $source.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $sourceInfo.Add([ordered]@{ source=$source.name; exists=$false })
            continue
        }
        $file = Get-Item -LiteralPath $path
        $sourceInfo.Add([ordered]@{
            source=$source.name; exists=$true; bytes=$file.Length;
            modified_utc=$file.LastWriteTimeUtc.ToString('o'); tail_limit=25000
        })
        try { $lines = @(Get-Content -LiteralPath $path -Tail 25000 -Encoding UTF8 -ErrorAction Stop) }
        catch { $warnings.Add('Unable to read source: ' + $source.name); continue }
        foreach ($line in $lines) {
            if (-not $line) { continue }
            $matched = ($task -and $line.Contains($task)) -or ($request -and $line.Contains($request))
            if (-not $matched) { continue }
            try { $rec = $line | ConvertFrom-Json -ErrorAction Stop }
            catch { $warnings.Add('Unparseable matching JSONL row in ' + $source.name); continue }
            # RemoteRuntimeLog wraps V9 event as STATUS + stage.
            $envelopeEvent = Safe-Code $rec.event
            $stageName = Safe-Code $rec.stage
            $name = if ($stageName -and $stageName -ne '[REDACTED_NONCODE]' -and
                        $envelopeEvent -eq 'STATUS') { $stageName } else { $envelopeEvent }
            $row = [ordered]@{ source=$source.name; event=$name;
                               envelope_event=$envelopeEvent; stage=$stageName }
            $t = $rec.timestamp
            if ($t -is [ValueType] -and $t -isnot [bool]) {
                $row.timestamp_unix = [double]$t
                try { $row.timestamp_utc = [DateTimeOffset]::FromUnixTimeMilliseconds(
                    [long]([double]$t * 1000)).UtcDateTime.ToString('o') }
                catch { $warnings.Add('Invalid Unix timestamp in ' + $source.name) }
            } elseif ([string]$t -match '^[0-9TZtz+:.-]{8,50}$') {
                $row.timestamp_utc = [string]$t
            }
            foreach ($key in $scalarKeys) {
                $prop = $rec.PSObject.Properties[$key]
                if ($null -eq $prop -or $null -eq $prop.Value) { continue }
                $v = $prop.Value
                if ($v -is [bool] -or ($v.GetType().FullName -in $numericTypes)) {
                    $row[$key] = $v
                } elseif ($v -is [string]) {
                    $row[$key] = Safe-Code $v
                }
            }
            foreach ($key in @('reason_codes','pending_kinds','action_ids')) {
                $prop = $rec.PSObject.Properties[$key]
                if ($null -ne $prop -and $null -ne $prop.Value) {
                    $row[$key] = Safe-CodeList $prop.Value
                }
            }
            if ($null -ne $rec.PSObject.Properties['diagnostics'] -and $rec.diagnostics) {
                $reasons = @()
                $markers = @()
                foreach ($d in @($rec.diagnostics)) {
                    if ($null -ne $d.reason) { $reasons += (Safe-Code $d.reason) }
                    if ($null -ne $d.marker) { $markers += (Safe-Code $d.marker) }
                }
                $row.diagnostic_reasons = @($reasons | Select-Object -First 30)
                $row.diagnostic_markers = @($markers | Select-Object -First 30)
                $row.diagnostic_count = @($rec.diagnostics).Count
                $issues = @()
                foreach ($d in @($rec.diagnostics)) {
                    foreach ($issue in @($d.field_issues)) {
                        if ($null -eq $issue) { continue }
                        $o = [ordered]@{}
                        foreach ($field in @('field','code','reason','expected_status',
                                             'actual_status','issue')) {
                            if ($null -ne $issue.PSObject.Properties[$field]) {
                                $o[$field] = Safe-Code $issue.$field
                            }
                        }
                        if ($o.Count) { $issues += $o }
                    }
                }
                $row.field_issues_codes = @($issues | Select-Object -First 30)
            }
            if ($name -eq 'planner_response_received' -and $null -ne $rec.response) {
                $response = [string]$rec.response
                $row.response_chars = $response.Length
                $row.response_sha256 = Hash-Text $response
                $row.tool_names = @([regex]::Matches($response, '"tool"\s*:\s*"([A-Za-z][A-Za-z0-9_]*)"') |
                    ForEach-Object { Safe-Code $_.Groups[1].Value } | Select-Object -First 40)
                $row.operation_names = @([regex]::Matches($response, '"operation"\s*:\s*"([A-Za-z][A-Za-z0-9_]*)"') |
                    ForEach-Object { Safe-Code $_.Groups[1].Value } | Select-Object -First 40)
                $row.progress_decisions = @([regex]::Matches($response, '"decision"\s*:\s*"([A-Za-z_]+)"') |
                    ForEach-Object { Safe-Code $_.Groups[1].Value } | Select-Object -First 10)
                $row.transport_fence_count = ([regex]::Matches($response, ([string][char]96 + [char]96 + [char]96 + 'smartagent_tool'))).Count
            }
            if ($name -eq 'gar_checkpoint_persisted' -and $rec.checkpoint_name) {
                $checkpoint = [string]$rec.checkpoint_name
                if ($checkpoint -match '^[a-f0-9]{40}\.json$') { [void]$checkpointNames.Add($checkpoint) }
            }
            $timeline.Add($row)
            if ($timeline.Count -ge 3500) {
                $warnings.Add('Timeline capped at 3500 matching rows.')
                break
            }
        }
    }

    $progressSummary = [ordered]@{ found=($null -ne $ledger) }
    if ($ledger) {
        foreach ($field in @('task_id','request_id','runtime_state','runtime_reason',
                             'current_step','total_steps','next_phase','outcome')) {
            $prop = $ledger.PSObject.Properties[$field]
            if ($null -ne $prop -and $null -ne $prop.Value) {
                $v = $prop.Value
                if ($v -is [string]) { $progressSummary[$field] = Safe-Code $v }
                elseif ($v -is [ValueType]) { $progressSummary[$field] = $v }
            }
        }
        if ($ledger.steps) {
            $progressSummary.step_statuses = @($ledger.steps | ForEach-Object { Safe-Code $_.status })
        }
    }
    Write-Json (Join-Path $stage 'task_progress_summary.json') $progressSummary

    $snapshots = @()
    $snapDir = Join-Path $root 'localdata\runtime\gar_snapshots'
    foreach ($fileName in $checkpointNames) {
        $snapshot = Read-Json (Join-Path $snapDir $fileName)
        if ($null -eq $snapshot) { continue }
        $snapshots += [ordered]@{
            checkpoint_file=$fileName
            schema=(Safe-Code $snapshot.schema)
            verification_preconditions=(Safe-CodeList $snapshot.verification_preconditions)
            blockers=(Safe-CodeList $snapshot.blockers)
            recovery_budget=[ordered]@{
                rejected_rounds=$snapshot.recovery_budget.rejected_rounds
                rebases=$snapshot.recovery_budget.rebases
                replans=$snapshot.recovery_budget.replans
                fresh_sessions=$snapshot.recovery_budget.fresh_sessions
                local_repair_exhausted=$snapshot.recovery_budget.local_repair_exhausted
                local_rejected_total=$snapshot.recovery_budget.local_rejected_total
            }
            pending_states=@($snapshot.pending_operations | ForEach-Object { Safe-Code $_.state })
            evidence_count=@($snapshot.checkpoint.verified_evidence).Count
        }
    }
    Write-Json (Join-Path $stage 'gar_snapshot_summaries.json') $snapshots
    $prAFile = if ($task) { Join-Path $root ('localdata\runtime\prerequisite_v3\' + $task + '.json') } else { '' }
    $prA = if ($prAFile) { Read-Json $prAFile } else { $null }
    $prASummary = [ordered]@{ found=$false }
    if ($prA -and $prA.schema -eq 'SMARTAGENT_PREREQ_V3_GRAPH_LEDGER_V1' -and $prA.nodes) {
        $nodeStates = [ordered]@{}
        $nodeKinds = [ordered]@{}
        foreach ($node in $prA.nodes.PSObject.Properties) {
            $kind = Safe-Code $node.Value.kind
            $state = Safe-Code $node.Value.state
            if (-not $nodeKinds.Contains($kind)) { $nodeKinds[$kind] = 0 }
            if (-not $nodeStates.Contains($state)) { $nodeStates[$state] = 0 }
            $nodeKinds[$kind]++
            $nodeStates[$state]++
        }
        $prANodes = @()
        foreach ($nodeProp in $prA.nodes.PSObject.Properties) {
            $node = $nodeProp.Value
            $prANodes += [ordered]@{
                id=(Safe-Code $nodeProp.Name)
                kind=(Safe-Code $node.kind)
                state=(Safe-Code $node.state)
                depends_on=(Safe-CodeList @($node.depends_on))
                action_id=(Safe-Code $node.action_id)
                scope_id=(Safe-Code $node.scope_id)
                evidence_present=(-not [string]::IsNullOrWhiteSpace([string]$node.evidence_ref_digest))
            }
        }
        $prASummary = [ordered]@{
            found=$true
            nodes=@($prANodes | Select-Object -First 512)
            schema='SMARTAGENT_PREREQ_V3_GRAPH_LEDGER_V1'
            revision=$prA.revision
            digest=(Safe-Code $prA.digest)
            task_id=(Safe-Code $prA.identity.task_id)
            request_id=(Safe-Code $prA.identity.request_id)
            node_count=@($prA.nodes.PSObject.Properties).Count
            event_count=@($prA.events).Count
            node_kinds=$nodeKinds
            node_states=$nodeStates
            raw_paths_included=$false
        }
    }
    Write-Json (Join-Path $stage 'pr_a_requirement_ledger_summary.json') $prASummary
    Write-Json (Join-Path $stage 'timeline.json') @($timeline.ToArray())

    $head = ''
    $branch = ''
    try {
        $head = [string]((& git -C $root rev-parse HEAD 2>$null) | Select-Object -First 1)
        $branch = [string]((& git -C $root branch --show-current 2>$null) | Select-Object -First 1)
    } catch { $warnings.Add('Git version inspection unavailable.') }
    $fingerprints = [ordered]@{}
    foreach ($relative in @('source\WebAgent\protocol_loop.py',
                             'source\agent_core\bounded_repair.py',
                             'source\agent_core\goal_anchored_recovery.py',
                             'config\protocol_manifest.json')) {
        $fp = Join-Path $root $relative
        if (Test-Path -LiteralPath $fp -PathType Leaf) {
            $fingerprints[$relative.Replace('\','/')] = (Hash-File $fp)
        }
    }
    $metadata = [ordered]@{
        schema='SMARTAGENT_LIVE_RCA_V2'
        created_utc=(Get-Date).ToUniversalTime().ToString('o')
        expected_base_commit=$baseSha
        installed_git_head=(Safe-Code $head)
        installed_git_branch=(Safe-Code $branch)
        selected_by=$selection
        task_id=(Safe-Code $task)
        request_id=(Safe-Code $request)
        event_count=$timeline.Count
        snapshot_count=@($snapshots).Count
        pr_a_ledger_found=[bool]$prASummary.found
        collector_process_l4_enabled=(@('0','false','no','off') -notcontains [string]$env:SMARTAGENT_GAR_L4_ENABLED)
        log_sources=$sourceInfo.ToArray()
        source_sha256=$fingerprints
        warnings=$warnings.ToArray()
    }
    Write-Json (Join-Path $stage 'manifest.json') $metadata
    $notes = @(
        'SmartAgent GAR Live RCA / sanitized diagnostic bundle',
        'Read-only collection. Runtime/Source/Task state was not modified.',
        'Data: request-scoped timeline (event codes, counters, model tool/operation names only),',
'task progress state, correlated GAR checkpoint and hashed-scope PR-A DAG edges, installed HEAD and SHA256 fingerprints.'
        'Excluded: raw planner replies, prompts, full commands, tool outputs, browser DOM, Telegram payloads,',
        'tokens, credentials, paths, environment variables, and unfiltered runtime logs.',
        'Note: collector_process_l4_enabled reflects this CLI process, not a previously started Worker.',
        'Scan-window limit: most recent 25000 lines per known JSONL source.',
        'If the wrong task was auto-selected, rerun collect.bat TASK-... RR-... .',
        'Review ZIP contents before sharing.'
    )
    [IO.File]::WriteAllLines((Join-Path $stage 'README.txt'), [string[]]$notes)
    $shortId = if ($task) { $task } elseif ($request) { $request } else { 'UNKNOWN' }
    $zip = Join-Path $outputDir ('GAR_LIVE_RCA_' + $stamp + '_' + $shortId + '.zip')
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal -Force
    Write-Host ('[COLLECT] Task: ' + $task)
    Write-Host ('[COLLECT] Request: ' + $request)
    Write-Host ('[COLLECT] Matched events: ' + $timeline.Count)
    Write-Host ('[COLLECT] GAR snapshots: ' + @($snapshots).Count)
    Write-Host ('[COLLECT] ZIP: ' + $zip)
    if ($timeline.Count -eq 0) {
        Write-Warning 'No matching events found. Confirm that this is the checkout actually used for Live, or pass Task/Request IDs.'
    }
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
