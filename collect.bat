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
$baseSha = 'e25bea7aa2d540cb77aead61babeaacaf3f19154'

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
function Redact-TelegramFinal([string]$value) {
    # Bounded outbound Telegram terminal prose only, never raw inbound messages,
    # Bot API requests, browser DOM, unfiltered tool results, or credentials.
    $safe = [string]$value
    if ($safe.Length -gt 48000) {
        $safe = $safe.Substring(0, 48000) + [Environment]::NewLine + '[TELEGRAM_SUMMARY_TRUNCATED]'
        $script:telegramTextTruncated = $true
    }
    $patterns = @(
        @{ p = '(?i)(?:/bot)?[0-9]{6,12}:[A-Za-z0-9_-]{20,}'; r = '[REDACTED_TELEGRAM_TOKEN]' },
        @{ p = '(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{12,}'; r = '$1[REDACTED_BEARER]' },
        @{ p = '(?im)\b((?:bot[_-]?token|access[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|authorization|session[_-]?key|secret)\s*[:=]\s*)[^\r\n]+'; r = '$1[REDACTED_SECRET]' },
        @{ p = '(?i)([?&](?:api_key|access_token|token|secret|password|key)=)[^&#\s]+'; r = '$1[REDACTED_SECRET]' },
        @{ p = '(?i)pair_[A-Za-z0-9_-]{16,}'; r = 'pair_[REDACTED]' },
        @{ p = '(?is)-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----.*?-----END (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'; r = '[REDACTED_PRIVATE_KEY]' }
    )
    foreach ($rule in $patterns) {
        $rx = New-Object System.Text.RegularExpressions.Regex([string]$rule.p)
        $count = $rx.Matches($safe).Count
        if ($count -gt 0) {
            $script:telegramRedactionCount += $count
            $safe = $rx.Replace($safe, [string]$rule.r)
        }
    }
    return $safe
}

function Hash-Text([string]$text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}
function Get-PlannerSCRSummary([string]$response) {
    # Parse only a bounded compact-V9 report_progress JSON line. Never export
    # raw Planner replies, commands, unknown keys, schema values or credentials.
    $progress = $null
    foreach ($line in ($response -split '\r?\n')) {
        $candidate = $line.Trim()
        if ($candidate.Length -lt 16 -or $candidate.Length -gt 131072) { continue }
        if (-not ($candidate.StartsWith('{') -and $candidate.EndsWith('}'))) { continue }
        try { $parsed = $candidate | ConvertFrom-Json -ErrorAction Stop }
        catch { continue }
        if ($parsed -is [pscustomobject] -and [string]$parsed.tool -ceq 'report_progress') {
            $progress = $parsed
            break
        }
    }
    if ($null -eq $progress) {
        return [ordered]@{ progress_found=$false; scr_present=$false; analysis='PROGRESS_NOT_PARSED' }
    }
    $scrField = $progress.PSObject.Properties['capability_resolution']
    if ($null -eq $scrField) {
        return [ordered]@{ progress_found=$true; scr_present=$false; analysis='ABSENT' }
    }
    $scr = $scrField.Value
    $objectValid = ($scr -is [pscustomobject])
    $keys = if ($objectValid) { @($scr.PSObject.Properties.Name) } else { @() }
    $expected = @('schema','capabilities')
    $missing = @($expected | Where-Object { $keys -cnotcontains $_ })
    $extraCount = @($keys | Where-Object { $expected -cnotcontains $_ }).Count
    $schemaPresent = ($keys -ccontains 'schema')
    $schemaMatches = ($schemaPresent -and
        [string]$scr.schema -ceq 'SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1')
    $capPresent = ($keys -ccontains 'capabilities')
    # Do NOT evaluate an array via an if-expression. PowerShell emits an
    # array's elements from a script block, unwrapping a singleton to string.
    $caps = $null
    if ($capPresent) { $caps = $scr.capabilities }
    $isList = ($caps -is [array])
    $values = if ($isList) { @($caps) } else { @() }
    $registry = @('directory.list','directory.inspect_metadata','directory.find',
                  'project.inspect_scope','project.inspect_working_set')
    $known = @()
    $invalid = 0
    foreach ($value in $values) {
        if ($value -is [string] -and $registry -ccontains $value) {
            $known += $value
        } else { $invalid++ }
    }
    $unique = @($known | Sort-Object -Unique)
    $duplicate = $known.Count - $unique.Count
    $shape = [ordered]@{
        progress_found=$true
        scr_present=$true
        analysis='SHAPE_PARSED'
        object_valid=$objectValid
        expected_keys_exact=($objectValid -and $missing.Count -eq 0 -and $extraCount -eq 0)
        missing_expected_keys=@($missing)
        extra_key_count=$extraCount
        schema_present=$schemaPresent
        schema_matches=[bool]$schemaMatches
        capabilities_is_list=$isList
        capability_count=$values.Count
        known_capabilities=@($unique)
        invalid_capability_count=$invalid
        duplicate_count=$duplicate
        schema_valid=($objectValid -and $missing.Count -eq 0 -and $extraCount -eq 0 -and
                      $schemaMatches -and $isList -and $values.Count -ge 1 -and
                      $values.Count -le 12 -and $invalid -eq 0 -and $duplicate -eq 0)
    }
    $canonicalShape = ConvertTo-Json -InputObject $shape -Depth 6 -Compress
    $shape['shape_digest'] = Hash-Text $canonicalShape
    # Digest only: detects changes to rejected proposals without exporting any
    # untrusted string or schema value. This is NOT evidence of valid semantics.
    $shape['proposal_digest'] = Hash-Text (ConvertTo-Json -InputObject $scr -Depth 8 -Compress)
    return $shape
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
# Completed tasks can delete Task Progress. Resolve only the exact Task's
# durable PR-B/C/D identity; do not pick a neighboring Telegram conversation.
if (-not $request -and $task) {
    $prBCDIdentity = Read-Json (Join-Path $root ('localdata\runtime\prerequisite_v3_recovery\' + $task + '.json'))
    if ($prBCDIdentity -and
        $prBCDIdentity.schema -eq 'SMARTAGENT_PREREQ_V3_BCD_LEDGER_V1' -and
        [string]$prBCDIdentity.identity.task_id -ceq $task -and
        [string]$prBCDIdentity.identity.request_id -match '^RR-[A-Za-z0-9_-]{1,156}$') {
        $request = [string]$prBCDIdentity.identity.request_id
    }
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
    'action_id','tool','operation','verification_status','result_status','result_reason','executed','effective_verification_status',
    'execution_status','last_reason','repair_status','diagnostic_reason','error_type',
    'response_sha256','response_chars','valid_decision_count','decision_type',
    'normalization','source_kind','json_error_message_code','json_error_line',
    'json_error_column','legacy_fence_detected','fence_detected',
    'scope_allowed','scope_error_code','scope_source','runtime_fact_ref_count',
    'scope_reason_code','scope_category','scope_path_role','scope_transition','scope_required_field',
    'repair_mode','issue_count','previous_issue_count','current_issue_count',
    'resolved_issue_count','introduced_issue_count','converging','full_admission_passed',
    'project_root_matches_workspace','explicit_path_count','request_paths_grant_write',
    'candidate_count','selected_count','strategy_candidate_count','untried_candidate_count',
    'artifact_candidate_count','artifact_count',
    'catalog_status','registration_status','catalog_path_authority',
    'registration_path_authority','goal_identity_status',
    'authorization_state','authorization_reason_code','authorization_evidence_present',
    'operation_stage','progress_transition','access_state','scope_preflight_status',
    'scope_preflight_reason_code','probe_evidence_present',
    'latest_probe_failure_category','latest_probe_failure_ref',
    'next_failure_evaluation','guidance_is_evidence',
    'failure_category','classification_verified','stderr_present',
    'stderr_sha256','exit_code','timed_out','blocking_evidence_ref',
    'next_evaluation','failure_count','last_failure_category',
    'failure_causal_receipt_count','prerequisite_transition_stage',
    'phase','ccrc_mode',
    'git_executable_status','next_verification','registered_artifact_count',
    'disposition','rewrite_reason_code','target_absence_evidence_ref',
    'original_command_sha256','dispatched_command_sha256',
    'changed_fields','command_unchanged','semantics_preserved',
    'dispatched','evidence_kind','execution_evidence_ref',
    'grant_created','rewritten_scope_admitted','original_scope_denied',
    'project_environment_index_status',
    'catalog_records_examined','catalog_matched_count',
    'catalog_goal_mismatch_count','catalog_invalid_record_count',
    'registration_records_examined','registration_matched_count',
    'index_status','bootstrap_state',
    'ceiling_active','ceiling_valid','ceiling_reason','grants_authorization',
    'observation_proof_admitted','observation_proof_reason',
    'observation_proof_code','task_condition_verified',
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
    'context_snapshot_id','context_digest','evidence_total','omitted_active_blocker_count',
    'critical_context_omitted','diagnostic_count','planner_response_received',
    'goal_condition_id','evidence_classification','next_tool','completion_verified',
    'kind','evidence_ref','authorization_granted',
    'revision','node_count','graph_digest','checkpoint_name','same_blocker','fresh_repair_attempts','elapsed_sec','elapsed_seconds',
    'operation','operation_valid','proposed_operation','proposal_sha256',
    'goal_context_sha256','source_requirement','active_goal_gap_count',
    'prerequisite_transition_stage','terminal_readiness_status',
    'snapshot_source','next_category','verification_status','result_reason'
)
$numericTypes = @('System.Int32','System.Int64','System.Double','System.Decimal','System.Single')
$timeline = New-Object 'System.Collections.Generic.List[object]'
$checkpointNames = New-Object 'System.Collections.Generic.HashSet[string]'
$sourceInfo = New-Object 'System.Collections.Generic.List[object]'
$warnings = New-Object 'System.Collections.Generic.List[string]'
$executorRejections = @()
$executorFinishEvents = @()
$executorTurboDecisions = @()
$executorSandboxEvents = @()
$executorTerminalKind = ''
$executorBoardStatus = 'NOT_FOUND'
$executorExpectedRejections = -1
$executorObservedRejections = 0
$executorTaskEpoch = ''
$executorBoardPath = Join-Path $root ('localdata\runtime\executor_run_blackboard\' + $task + '.json')
if ($task -and [IO.File]::Exists($executorBoardPath)) {
    $executorBoardStatus = 'INVALID'
    try {
        if ((Get-Item -LiteralPath $executorBoardPath).Length -gt 4194304) {
            $warnings.Add('EXECUTOR_BLACKBOARD_TOO_LARGE')
        } else {
            $executorBoard = Read-Json $executorBoardPath
            $identityMatched = (
                $null -ne $executorBoard -and
                [string]$executorBoard.schema -ceq 'SMARTAGENT_EXECUTOR_RUN_BLACKBOARD_V1' -and
                [string]$executorBoard.task_id -ceq $task -and
                $request -and [string]$executorBoard.request_id -ceq $request -and
                -not [string]::IsNullOrWhiteSpace([string]$executorBoard.task_epoch)
            )
            if ($identityMatched -and $null -ne $ledger -and
                $null -ne $ledger.PSObject.Properties['task_epoch'] -and $ledger.task_epoch) {
                $identityMatched = [string]$executorBoard.task_epoch -ceq [string]$ledger.task_epoch
            }
            if ($identityMatched) {
                $executorBoardStatus = 'MATCHED'
                $executorTaskEpoch = Safe-Code $executorBoard.task_epoch
                foreach ($ev in @($executorBoard.events)) {
                    if ($null -eq $ev -or [string]$ev.kind -cnotin @('ADMISSION_REJECTED', 'TOOL_RESULT') -or
                        [string]$ev.runtimeEvidence.status -cne 'REJECTED') { continue }
                    $executorObservedRejections++
                    $diag = $ev.admission_diagnostic
                    $fields = @()
                    foreach ($issue in @($diag.field_issues) | Select-Object -First 16) {
                        if ($null -eq $issue) { continue }
                        $expected = [string]$issue.expected_type
                        $actual = [string]$issue.actual_type
                        $fields += [ordered]@{
                            field=(Safe-Code $issue.field)
                            reason=(Safe-Code $issue.reason)
                            expected_type=$(if ($expected -match '^[A-Za-z0-9_ |.\[\]]{1,64}$') { $expected } else { '[REDACTED_TYPE]' })
                            actual_type=$(if ($actual -match '^[A-Za-z0-9_ |.\[\]]{1,64}$') { $actual } else { '[REDACTED_TYPE]' })
                            actual_value=(Safe-Code $issue.actual_value)
                            allowed_values=(Safe-CodeList @($issue.allowed_values))
                            constraint_code=(Safe-Code $issue.constraint_code)
                        }
                    }
                    $executorRejections += [ordered]@{
                        source='executor_blackboard'
                        event='executor_action_admission_rejected'
                        round=$(if ($null -ne $ev.round) { [int]$ev.round } else { 0 })
                        tool=(Safe-Code $ev.toolName)
                        reason_code=(Safe-Code $ev.runtimeEvidence.reason_code)
                        repair_mode=(Safe-Code $diag.repair_class)
                        admission_stage=(Safe-Code $diag.stage)
                        admission_repair_attempt=$(if ($null -ne $ev.admission_repair_attempt) { [int]$ev.admission_repair_attempt } else { 0 })
                        proposal_sha256=(Safe-Code $ev.arguments_sha256)
                        field_issues=@($fields)
                        executed=$false
                        authorization_granted=$false
                        raw_command_included=$false
                    }
                }
                foreach ($ev in @($executorBoard.events)) {
                    if ($null -eq $ev -or [string]$ev.kind -notin @(
                        'MODEL_DECISION_REJECTED', 'MODEL_FINISH_SCHEMA_REPAIR',
                        'MODEL_FINISH_STATUS_REPAIR', 'MODEL_FINISH_CONTRACT_INVALID'
                    )) { continue }
                    $diag = $ev.finish_diagnostic
                    $diagnosticFields = @()
                    $statusCorrections = @()
                    if ($null -ne $diag) {
                        foreach ($correction in @($diag.corrections) | Select-Object -First 32) {
                            if ($null -eq $correction) { continue }
                            $statusCorrections += [ordered]@{
                                evidence_ref=(Safe-Code $correction.evidence_ref)
                                field=(Safe-Code $correction.field)
                                model_claimed_status=(Safe-Code $correction.model_claimed_status)
                                runtime_actual_status=(Safe-Code $correction.runtime_actual_status)
                            }
                        }
                        $diagnosticFields = @(Safe-CodeList @($diag.allowed_values))
                    }
                    $executorFinishEvents += [ordered]@{
                        round=$(if ($null -ne $ev.round) { [int]$ev.round } else { 0 })
                        event=(Safe-Code $ev.kind)
                        reason_code=(Safe-Code $ev.reason_code)
                        error_code=(Safe-Code $ev.error_code)
                        repair_attempt=$(if ($null -ne $ev.repair_attempt) { [int]$ev.repair_attempt } else { 0 })
                        repair_attempts=$(if ($null -ne $ev.repair_attempts) { [int]$ev.repair_attempts } else { 0 })
                        repair_only=[bool]$ev.repair_only
                        check_index=$(if ($null -ne $diag -and $null -ne $diag.check_index) { [int]$diag.check_index } else { -1 })
                        field=$(if ($null -ne $diag) { Safe-Code $diag.field } else { '' })
                        constraint_code=$(if ($null -ne $diag) { Safe-Code $diag.constraint_code } else { '' })
                        expected_type=$(if ($null -ne $diag) { Safe-Code $diag.expected_type } else { '' })
                        actual_type=$(if ($null -ne $diag) { Safe-Code $diag.actual_type } else { '' })
                        allowed_values=@($diagnosticFields)
                        status_corrections=@($statusCorrections)
                        executed=$false
                        raw_model_response_included=$false
                    }
                }
                # Git46 OS Sandbox evidence: no raw command, environment,
                # token, user data path or full process output is exported.
                foreach ($ev in @($executorBoard.events)) {
                    if ($null -eq $ev -or [string]$ev.kind -cne 'TURBO_SANDBOX_BLOCKED') { continue }
                    $executorSandboxEvents += [ordered]@{
                        schema='SMARTAGENT_TURBO_SANDBOX_RCA_V1'
                        event_id=(Safe-Code $ev.event_id)
                        evidence_ref=(Safe-Code $ev.evidence_ref)
                        action_id=(Safe-Code $ev.action_id)
                        sandbox_mode=(Safe-Code $ev.sandbox_mode)
                        sandbox_backend=(Safe-Code $ev.sandbox_backend)
                        sandbox_identity_verified=[bool]$ev.sandbox_identity_verified
                        sandbox_status=(Safe-Code $ev.sandbox_status)
                        turbo_requested=(Safe-Code $ev.turbo_requested)
                        turbo_effective=(Safe-Code $ev.turbo_effective)
                        acl_mode=(Safe-Code $ev.acl_mode)
                        acl_attestation=(Safe-Code $ev.acl_attestation)
                        process_isolation_attestation=(Safe-Code $ev.process_isolation_attestation)
                        child_process_policy=(Safe-Code $ev.child_process_policy)
                        workspace_root_sha256=(Hash-Text ([string]$ev.workspace_root))
                        turbo_root_sha256=(Hash-Text ([string]$ev.turbo_root))
                        policy_revision=$(if ($null -ne $ev.policy_revision) { [int]$ev.policy_revision } else { 0 })
                        requested_operation=(Safe-Code $ev.requested_operation)
                        scope_verdict=(Safe-Code $ev.scope_verdict)
                        os_execution_result=(Safe-Code $ev.os_execution_result)
                        os_access_denied=[bool]$ev.os_access_denied
                        failure_reason_code=(Safe-Code $ev.failure_reason_code)
                        executed=$false
                        authorization_granted=$false
                        raw_command_included=$false
                        secrets_included=$false
                    }
                }
                foreach ($ev in @($executorBoard.events)) {
                    if ($null -eq $ev -or [string]$ev.kind -cne 'TURBO_DECISION') { continue }
                    $executorTurboDecisions += [ordered]@{
                        schema='SMARTAGENT_TURBO_DECISION_RCA_V1'
                        round=$(if ($null -ne $ev.round) { [int]$ev.round } else { 0 })
                        tool=(Safe-Code $ev.toolName)
                        action_id=(Safe-Code $ev.action_id)
                        command_sha256=(Safe-Code $ev.command_sha256)
                        arguments_sha256=(Safe-Code $ev.arguments_sha256)
                        risk_class=(Safe-Code $ev.risk_class)
                        admission_status=(Safe-Code $ev.admission_status)
                        decision=(Safe-Code $ev.decision)
                        reason_code=(Safe-Code $ev.reason_code)
                        admission_reason_code=(Safe-Code $ev.admission_reason_code)
                        auto_confirm=[bool]$ev.auto_confirm
                        turbo_policy_qualified=[bool]$ev.turbo_policy_qualified
                        policy_revision=$(if ($null -ne $ev.policy_revision) { [int]$ev.policy_revision } else { 0 })
                        workspace_root_sha256=(Hash-Text ([string]$ev.workspace_root))
                        turbo_root_sha256=(Hash-Text ([string]$ev.turbo_root))
                        working_directory_sha256=(Hash-Text ([string]$ev.working_directory))
                        executed=$false
                        authorization_granted=$false
                        raw_command_included=$false
                        raw_path_included=$false
                    }
                }
                if ($null -ne $executorBoard.terminal) {
                    $executorExpectedRejections = [int]$executorBoard.terminal.admission_rejection_count
                    $executorTerminalKind = Safe-Code $executorBoard.terminal.kind
                }
            } else {
                $warnings.Add('EXECUTOR_BLACKBOARD_IDENTITY_OR_SCHEMA_MISMATCH')
            }
        }
    } catch {
        $executorBoardStatus = 'INVALID'
        $executorRejections = @()
        $warnings.Add('EXECUTOR_BLACKBOARD_READ_ERROR')
    }
}
$executorAdmissionSummary = [ordered]@{
    schema='SMARTAGENT_EXECUTOR_ADMISSION_RCA_V1'
    status=$executorBoardStatus
    task_id=(Safe-Code $task)
    request_id=(Safe-Code $request)
    task_epoch=$executorTaskEpoch
    terminal_rejection_count=$executorExpectedRejections
    observed_rejection_count=$executorObservedRejections
    exported_rejection_count=@($executorRejections).Count
    identity_matched=($executorBoardStatus -eq 'MATCHED')
    raw_command_included=$false
    raw_result_included=$false
    terminal_kind=$executorTerminalKind
    finish_diagnostic_event_count=@($executorFinishEvents).Count
    turbo_decision_event_count=@($executorTurboDecisions).Count
    turbo_sandbox_blocked_count=@($executorSandboxEvents).Count
    turbo_auto_confirm_count=@($executorTurboDecisions | Where-Object { $_.auto_confirm }).Count
}
Write-Json (Join-Path $stage 'executor_admission_summary.json') $executorAdmissionSummary
Write-Json (Join-Path $stage 'executor_finish_diagnostics.json') @($executorFinishEvents)
Write-Json (Join-Path $stage 'executor_turbo_decisions.json') @($executorTurboDecisions)
Write-Json (Join-Path $stage 'executor_sandbox_evidence.json') @($executorSandboxEvents)

try {
    foreach ($source in $sources) {
        $path = $source.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $sourceInfo.Add([ordered]@{ source=$source.name; exists=$false })
            continue
        }
        $file = Get-Item -LiteralPath $path
        $scanMode = 'FULL_FILE_STREAM'
        $tailLimit = 0
        # Full streaming scan of normally sized JSONL logs. Only the matching
        # Task/Request rows are retained, not unrelated conversations.
        # Oversized logs are safely bounded and explicitly flagged PARTIAL.
        $lines = New-Object 'System.Collections.Generic.List[string]'
        try {
            if ($file.Length -gt 134217728) {
                $scanMode = 'TAIL_FALLBACK'
                $tailLimit = 25000
                $warnings.Add('SOURCE_FULL_SCAN_SIZE_LIMIT: ' + $source.name)
                foreach ($item in @(Get-Content -LiteralPath $path -Tail 25000 -Encoding UTF8 -ErrorAction Stop)) {
                    $lines.Add([string]$item)
                }
            } else {
                $stream = [IO.File]::Open(
                    $path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                    [IO.FileShare]::ReadWrite
                )
                try {
                    $reader = [IO.StreamReader]::new(
                        $stream, [Text.Encoding]::UTF8, $true
                    )
                    try {
                        while ($null -ne ($item = $reader.ReadLine())) {
                            if (($task -and $item.Contains($task)) -or
                                ($request -and $item.Contains($request))) {
                                $lines.Add($item)
                                if ($lines.Count -ge 3500) {
                                    $warnings.Add('Full scan matching row cap reached: ' + $source.name)
                                    break
                                }
                            }
                        }
                    } finally { $reader.Dispose() }
                } finally { $stream.Dispose() }
            }
        } catch {
            $warnings.Add('Unable to read source: ' + $source.name)
            continue
        }
        $sourceInfo.Add([ordered]@{
            source=$source.name; exists=$true; bytes=$file.Length;
            modified_utc=$file.LastWriteTimeUtc.ToString('o');
            scan_mode=$scanMode; tail_limit=$tailLimit;
            scan_guarantees_unlimited_history=($scanMode -eq 'FULL_FILE_STREAM' -and
                @($warnings | Where-Object { $_ -eq ('Full scan matching row cap reached: ' + $source.name) }).Count -eq 0)
        })
        foreach ($line in $lines) {
            if (-not $line) { continue }
            $matched = ($task -and $line.Contains($task)) -or ($request -and $line.Contains($request))
            if (-not $matched) { continue }
            try { $rec = $line | ConvertFrom-Json -ErrorAction Stop }
            catch { $warnings.Add('Unparseable matching JSONL row in ' + $source.name); continue }
            # A raw string match does not establish Task/Request identity.
            # Never join an event that explicitly belongs to another request.
            if ($task -and $null -ne $rec.PSObject.Properties['task_id'] -and
                $rec.task_id -and [string]$rec.task_id -cne $task) { continue }
            if ($request -and $null -ne $rec.PSObject.Properties['request_id'] -and
                $rec.request_id -and [string]$rec.request_id -cne $request) { continue }
            # RemoteRuntimeLog wraps V9 event as STATUS + stage.
            $envelopeEvent = Safe-Code $rec.event
            $stageName = Safe-Code $rec.stage
            $name = if ($stageName -and $stageName -ne '[REDACTED_NONCODE]' -and
                        $envelopeEvent -eq 'STATUS') { $stageName } else { $envelopeEvent }
            $row = [ordered]@{ source=$source.name; event=$name;
                               envelope_event=$envelopeEvent; stage=$stageName }
            # RemoteWorker stores event name in stage and remaps an inner
            # prerequisite stage to operation_stage; both are meaningful.
            if ($null -ne $rec.PSObject.Properties['operation_stage']) {
                $row.transition_stage = Safe-Code $rec.operation_stage
            }
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
            foreach ($key in @('reason_codes','pending_kinds','action_ids',
                               'issue_codes','issue_fields','contract_names',
                               'decision_codes','pending_action_ids',
                               'admitted_action_ids','changed_fields',
                               'rewrite_rule_ids','planner_failure_action_ids',
                               'strategy_candidate_route_ids','strategy_candidate_statuses',
                               'proposed_fields')) {
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
                $admissionScope = @()
                foreach ($d in @($rec.diagnostics)) {
                    if ($null -eq $d -or -not $d.scope_reason_code) { continue }
                    $admissionScope += [ordered]@{
                        reason=(Safe-Code $d.reason)
                        action_id=(Safe-Code $d.action_id)
                        tool=(Safe-Code $d.tool)
                        scope_reason_code=(Safe-Code $d.scope_reason_code)
                        scope_category=(Safe-Code $d.scope_category)
                        scope_path_role=(Safe-Code $d.scope_path_role)
                        scope_transition=(Safe-Code $d.scope_transition)
                        scope_required_field=(Safe-Code $d.scope_required_field)
                        executed=$false
                        authorization_granted=$false
                    }
                }
                if ($admissionScope.Count -gt 0) {
                    $row.admission_scope_diagnostics = @($admissionScope | Select-Object -First 12)
                }
                $contractDiffs = @()
                foreach ($d in @($rec.diagnostics)) {
                    $unified = $d.unified_contract
                    if ($null -eq $unified -or [string]$unified.schema -cne
                        'SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1') { continue }
                    foreach ($issue in @($unified.issues | Select-Object -First 24)) {
                        if ($null -eq $issue) { continue }
                        $contractDiffs += [ordered]@{
                            contract=(Safe-Code $issue.contract)
                            tool=(Safe-Code $issue.tool)
                            action_id=(Safe-Code $issue.action_id)
                            reason_code=(Safe-Code $issue.reason_code)
                            decision=(Safe-Code $issue.decision)
                            unexpected_keys=(Safe-CodeList @($issue.unexpected_keys))
                            missing_keys=(Safe-CodeList @($issue.missing_keys))
                            field_issues=(Safe-CodeList @($issue.field_issues))
                        }
                    }
                }
                if ($contractDiffs.Count -gt 0) {
                    $row.unified_contract_issue_diffs = @($contractDiffs | Select-Object -First 24)
                }
                # Preserve only known, safe field paths (never the diagnostic
                # detail itself, which can include script source or private paths).
                $knownCommandFields = @('command','operation','verify','success_criteria',
                                        'working_directory','timeout','action_id')
                $commandRepairs = @()
                foreach ($d in @($rec.diagnostics)) {
                    $detailText = [string]$d.detail
                    if ([string]$d.tool -eq 'run_command' -and
                        $detailText -match '^field=(command|operation|verify|success_criteria|working_directory|timeout|action_id)(?:\[[0-9]{1,2}\]\.command)?(?:;|\s|$)') {
                        $fieldName = [string]$Matches[1]
                        if ($knownCommandFields -ccontains $fieldName) {
                            $row.command_reject_field = $fieldName
                        }
                    }
                    $repair = $d.command_repair
                    if ($null -eq $repair -or
                        [string]$repair.schema -cne 'SMARTAGENT_COMMAND_REPAIR_READINESS_V1') {
                        continue
                    }
                    $safeIssues = @()
                    foreach ($issue in @($repair.field_issues)) {
                        if ($null -eq $issue) { continue }
                        $fieldName = Safe-Code $issue.field
                        $codeName = Safe-Code $issue.code
                        if ($fieldName -in @('command','operation','verify','success_criteria',
                                           'working_directory','timeout','action_id','tool',
                                           'depends_on','replaces_action_id')) {
                            $safeIssues += [ordered]@{ field=$fieldName; code=$codeName }
                        }
                    }
                    $commandRepairs += [ordered]@{
                        action_id=(Safe-Code $repair.action_id)
                        mode=(Safe-Code $repair.mode)
                        issue_count=[int]$repair.issue_count
                        field_issues=@($safeIssues | Select-Object -First 16)
                        authorization_granted=$false
                    }
                }
                if ($commandRepairs.Count -gt 0) {
                    $row.command_repair_diagnostics = @($commandRepairs | Select-Object -First 8)
                }
                $scrDiagnostics = @()
                foreach ($d in @($rec.diagnostics)) {
                    if ($null -eq $d.capability_repair) { continue }
                    $repair = $d.capability_repair
                    $shape = $repair.proposal_shape
                    $scrDiagnostics += [ordered]@{
                        reason=(Safe-Code $d.reason)
                        repair_target='report_progress.capability_resolution'
                        object_type_valid=[bool]$shape.object_type_valid
                        key_set_exact=[bool]$shape.key_set_exact
                        schema_matches=[bool]$shape.schema_matches
                        capabilities_is_list=[bool]$shape.capabilities_is_list
                        unknown_or_invalid_count=[int]$shape.unknown_or_invalid_count
                        duplicate_count=[int]$shape.duplicate_count
                        extra_key_count=[int]$shape.extra_key_count
                        previously_selected=(Safe-CodeList @($repair.previously_selected))
                        required_for_submitted_actions=(Safe-CodeList @($repair.required_for_submitted_actions))
                    }
                }
                if ($scrDiagnostics.Count -gt 0) {
                    $row.scr_repair_diagnostics = @($scrDiagnostics | Select-Object -First 8)
                }
                $contextRepairs = @()
                foreach ($d in @($rec.diagnostics)) {
                    if ($null -eq $d.context_request_repair) { continue }
                    $repair = $d.context_request_repair
                    if ([string]$repair.schema -cne 'SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1') {
                        continue
                    }
                    $issues = @()
                    foreach ($issue in @($repair.field_issues)) {
                        if ($null -eq $issue) { continue }
                        $issues += [ordered]@{
                            field=(Safe-Code $issue.field)
                            code=(Safe-Code $issue.code)
                        }
                    }
                    $contextRepairs += [ordered]@{
                        schema='SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1'
                        repair_scope='CONTEXT_REQUEST_SCHEMA'
                        issues=@($issues | Select-Object -First 8)
                        eligible_blocker_count=@($repair.objective_blocker_ids | Where-Object { $_ }).Count
                        blocked_requires_runtime_receipt=$true
                    }
                }
                if ($contextRepairs.Count -gt 0) {
                    $row.context_request_repair = @($contextRepairs | Select-Object -First 8)
                }
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
                $row.planner_scr = Get-PlannerSCRSummary $response
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
        foreach ($field in @('task_id','request_id','runtime_state','interruption_reason',
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
        $reasonText = [string]$ledger.interruption_reason
        $progressSummary.interruption_reason_present = (-not [string]::IsNullOrWhiteSpace($reasonText))
        if ($progressSummary.interruption_reason_present) {
            $progressSummary.interruption_reason_sha256 = Hash-Text $reasonText
            if ($reasonText -notmatch '^[A-Za-z0-9_:.-]{1,160}$') {
                $progressSummary.interruption_reason = '[REDACTED_NONCODE]'
            }
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
    # PR-B/C/D sanitized summary; never include raw commands, package names,
    # full paths, environment variables or model output.
    $prBCDSummary = [ordered]@{ found=$false }
    $prBCDLedger = if ($task) {
        Join-Path $root ('localdata\runtime\prerequisite_v3_recovery\' + $task + '.json')
    } else { '' }
    $prBCD = if ($prBCDLedger) { Read-Json $prBCDLedger } else { $null }
    if ($prBCD -and $prBCD.schema -eq 'SMARTAGENT_PREREQ_V3_BCD_LEDGER_V1') {
        $prBCDSummary = [ordered]@{
            found=$true
            schema='SMARTAGENT_PREREQ_V3_BCD_LEDGER_V1'
            phase=(Safe-Code $prBCD.phase)
            revision=$prBCD.revision
            digest=(Safe-Code $prBCD.digest)
            task_id=(Safe-Code $prBCD.identity.task_id)
            request_id=(Safe-Code $prBCD.identity.request_id)
            missing_count=@($prBCD.missing_ids).Count
            unknown_count=@($prBCD.unknown_ids).Count
            remediation_requested=[bool]$prBCD.remediation_requested
            project_environment_required=[bool]$prBCD.project_environment_required
            project_environment_verified=[bool]$prBCD.project_environment_verified
            project_environment_evidence_present=(-not [string]::IsNullOrWhiteSpace([string]$prBCD.project_environment_evidence_sha256))
            remediation_action=(Safe-Code $prBCD.remediation_action)
            verification_action=(Safe-Code $prBCD.verification_action)
            resumed_action=(Safe-Code $prBCD.resumed_action)
            evidence_digest_present=@{
                remediation=(-not [string]::IsNullOrWhiteSpace([string]$prBCD.remediation_evidence_sha256))
                verification=(-not [string]::IsNullOrWhiteSpace([string]$prBCD.verification_evidence_sha256))
                resumed=(-not [string]::IsNullOrWhiteSpace([string]$prBCD.resume_evidence_sha256))
            }
            transition_codes=@($prBCD.events | ForEach-Object { Safe-Code $_.type } | Select-Object -First 100)
        }
    }
    Write-Json (Join-Path $stage 'pr_bcd_recovery_summary.json') $prBCDSummary
    Write-Json (Join-Path $stage 'timeline.json') @($timeline.ToArray())

    # Minimal Core v10: preserve bounded parser error location, exact decision
    # counts and format recovery reasons. Never include raw DOM content,
    # commands, tool arguments or masked local journal text in the ZIP.
    $minimalDecisionDiagnostics = @($timeline.ToArray() | Where-Object {
        $_.event -in @('minimal_decision_rejected', 'minimal_decision_accepted')
    } | Select-Object -Last 120 | ForEach-Object {
        [ordered]@{
            round=$_.round
            event=$_.event
            diagnostic_reason=$_.diagnostic_reason
            response_sha256=$_.response_sha256
            response_chars=$_.response_chars
            source_kind=$_.source_kind
            candidate_count=$_.candidate_count
            valid_decision_count=$_.valid_decision_count
            normalization=$_.normalization
            decision_type=$_.decision_type
            fence_detected=$_.fence_detected
            legacy_fence_detected=$_.legacy_fence_detected
            json_error_message_code=$_.json_error_message_code
            json_error_line=$_.json_error_line
            json_error_column=$_.json_error_column
        }
    })
    Write-Json (Join-Path $stage 'minimal_core_decision_diagnostics.json') @($minimalDecisionDiagnostics)
    # v10 proposal/decision lineage: only safe field names and operation tokens.
    # Never export raw model replies, commands, requested paths or tool output.
    $minimalLineage = @($timeline.ToArray() | Where-Object {
        $_.event -in @('minimal_action_proposed','minimal_action_admitted',
                       'minimal_action_semantic_rewrite','minimal_core_action_observed',
                       'minimal_goal_action_rejected','minimal_goal_driver_context')
    } | Select-Object -Last 250 | ForEach-Object {
        [ordered]@{
            round=$_.round; event=$_.event; task_epoch=$_.task_epoch
            tool=$_.tool; action_id=$_.action_id
            operation=$_.operation; operation_valid=$_.operation_valid
            proposed_operation=$_.proposed_operation
            proposed_fields=@($_.proposed_fields)
            proposal_sha256=$_.proposal_sha256
            goal_context_sha256=$_.goal_context_sha256
            context_sha256=$_.context_sha256
            snapshot_source=$_.snapshot_source
            next_category=$_.next_category
            prerequisite_transition_stage=$_.prerequisite_transition_stage
            source_requirement=$_.source_requirement
            terminal_readiness_status=$_.terminal_readiness_status
            reason_code=$_.reason_code; repair_mode=$_.repair_mode
            rewrite_reason_code=$_.rewrite_reason_code
            executed=$_.executed; status=$_.status
            result_reason=$_.result_reason; verification_status=$_.verification_status
            evidence_ref=$_.evidence_ref
            raw_command_included=$false; raw_result_included=$false
        }
    })
    Write-Json (Join-Path $stage 'minimal_core_action_lineage.json') @($minimalLineage)

    # Cross-check the task's append-only Minimal Core journal, separately from
    # RemoteRuntimeLog. The first row must bind BOTH Task and Request; only
    # bounded, safe metadata is exported, never its raw file/command/prose.
    $minimalJournalSummary = [ordered]@{
        found=$false; identity_matched=$false; event_count=0;
        task_epoch=''; rejection_count=0; prepared_count=0;
        observed_count=0; parser_rejection_count=0;
        raw_journal_included=$false
    }
    $minimalJournalRows = @()
    if ($task -and $request) {
        $journalPath = Join-Path $root ('localdata\runtime\minimal_core\' + $task + '.jsonl')
        if ([IO.File]::Exists($journalPath)) {
            $minimalJournalSummary.found = $true
            $info = Get-Item -LiteralPath $journalPath
            if ($info.Length -gt 8388608) {
                $warnings.Add('MINIMAL_JOURNAL_TOO_LARGE')
            } else {
                try {
                    $entries = @(Get-Content -LiteralPath $journalPath -Encoding UTF8 -ErrorAction Stop)
                    if ($entries.Count -gt 2048) {
                        $warnings.Add('MINIMAL_JOURNAL_ROW_CAP')
                    } else {
                        $first = $null
                        if ($entries.Count -gt 0) {
                            $first = $entries[0] | ConvertFrom-Json -ErrorAction Stop
                        }
                        if ($null -ne $first -and
                            [string]$first.schema -ceq 'SMARTAGENT_MINIMAL_CORE_JOURNAL_V1' -and
                            [string]$first.event -ceq 'task_started' -and
                            [string]$first.task_id -ceq $task -and
                            [string]$first.request_id -ceq $request) {
                            $minimalJournalSummary.identity_matched = $true
                            $minimalJournalSummary.task_epoch = Safe-Code $first.task_epoch
                            foreach ($line in $entries) {
                                if (-not $line) { continue }
                                $rec = $line | ConvertFrom-Json -ErrorAction Stop
                                if ([string]$rec.schema -cne 'SMARTAGENT_MINIMAL_CORE_JOURNAL_V1') {
                                    throw 'Unexpected journal schema'
                                }
                                $row = [ordered]@{
                                    event=(Safe-Code $rec.event)
                                    round=$(if ($null -ne $rec.round) { [int]$rec.round } else { 0 })
                                    tool=(Safe-Code $rec.tool)
                                    action_id=(Safe-Code $rec.action_id)
                                    reason=(Safe-Code $rec.reason)
                                    repair_mode=(Safe-Code $rec.repair_mode)
                                    proposed_operation=(Safe-Code $rec.proposed_operation)
                                    canonical_operation=(Safe-Code $rec.canonical_operation)
                                    rewrite_reason_code=(Safe-Code $rec.rewrite_reason_code)
                                    proposal_sha256=(Safe-Code $rec.proposal_sha256)
                                    fingerprint=(Safe-Code $rec.fingerprint)
                                    context_sha256=(Safe-Code $rec.context_sha256)
                                    response_sha256=(Safe-Code $rec.response_sha256)
                                    result_sha256=(Safe-Code $rec.result_sha256)
                                    evidence_ref=(Safe-Code $rec.evidence_ref)
                                    status=(Safe-Code $rec.status)
                                    executed=$(if ($null -ne $rec.executed) { [bool]$rec.executed } else { $null })
                                    raw_journal_included=$false
                                }
                                $minimalJournalRows += $row
                            }
                            $minimalJournalSummary.event_count = $minimalJournalRows.Count
                            $minimalJournalSummary.rejection_count = @($minimalJournalRows | Where-Object { $_.event -eq 'action_rejected' }).Count
                            $minimalJournalSummary.prepared_count = @($minimalJournalRows | Where-Object { $_.event -eq 'action_prepared' }).Count
                            $minimalJournalSummary.observed_count = @($minimalJournalRows | Where-Object { $_.event -eq 'action_observed' }).Count
                            $minimalJournalSummary.parser_rejection_count = @($minimalJournalRows | Where-Object { $_.event -eq 'decision_rejected' }).Count
                        } else {
                            $warnings.Add('MINIMAL_JOURNAL_TASK_REQUEST_IDENTITY_MISMATCH')
                        }
                    }
                } catch {
                    $minimalJournalRows = @()
                    $minimalJournalSummary.identity_matched = $false
                    $warnings.Add('MINIMAL_JOURNAL_UNPARSEABLE')
                }
            }
        }
    }
    Write-Json (Join-Path $stage 'minimal_core_journal_summary.json') ([ordered]@{
        metadata=$minimalJournalSummary
        events=@($minimalJournalRows | Select-Object -Last 400)
    })

    # Correlate bounded, non-sensitive Planner decisions with explicit
    # Context digests and fail-closed SCR shape checks. No raw reply survives.
    $decisionDiffs = @()
    $previousSCR = $null
    $priorResponse = ''
    $plannerEvents = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'planner_response_received' -and $null -ne $_.planner_scr
    } | Sort-Object @{ Expression={ [int]$_.round } }, @{ Expression={ [double]$_.timestamp_unix } })
    # Mirrored Remote/WebAgent log sources may contain the same response.
    # Dedupe only identical (round, SHA256), not genuinely different replies.
    $seenPlannerReplies = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($entry in $plannerEvents) {
        $replyKey = ([string]$entry.round + '|' + [string]$entry.response_sha256)
        if (-not $seenPlannerReplies.Add($replyKey)) { continue }
        $scr = $entry.planner_scr
        $proposalHash = [string]$scr.proposal_digest
        $changed = $null
        $shapeChanged = $null
        if ($null -ne $previousSCR -and $scr.scr_present -and $previousSCR.scr_present) {
            $changed = ($proposalHash -cne [string]$previousSCR.proposal_digest)
            $shapeChanged = ([string]$scr.shape_digest -cne [string]$previousSCR.shape_digest)
        }
        $knownCapabilities = @()
        if ($scr.scr_present) {
            $knownCapabilities = @($scr.known_capabilities | Where-Object { $_ -is [string] })
        }
        $decisionDiffs += [ordered]@{
            round=$entry.round
            source=$entry.source
            response_sha256=$entry.response_sha256
            progress_found=[bool]$scr.progress_found
            scr_present=[bool]$scr.scr_present
            schema_valid=$(if ($scr.scr_present) { [bool]$scr.schema_valid } else { $null })
            schema_status=$(if (-not $scr.scr_present) { 'NOT_SUBMITTED' }
                elseif ($scr.schema_valid) { 'VALID' } else { 'INVALID' })
            proposal_changed_from_previous=$changed
            shape_changed_from_previous=$shapeChanged
            schema_matches=[bool]$scr.schema_matches
            expected_keys_exact=[bool]$scr.expected_keys_exact
            known_capabilities=$knownCapabilities
            invalid_capability_count=$scr.invalid_capability_count
            extra_key_count=$scr.extra_key_count
            duplicate_count=$scr.duplicate_count
        }
        $previousSCR = $scr
    }
    Write-Json (Join-Path $stage 'planner_contract_diff.json') @($decisionDiffs)

    $delivery = @($timeline.ToArray() | Where-Object {
        $_.event -in @('planner_context_prepared','planner_context_response_correlated',
                       'minimal_goal_driver_context')
    } | ForEach-Object {
        [ordered]@{
            source=$_.source; event=$_.event; round=$_.round
            context_snapshot_id=$_.context_snapshot_id
            context_digest=$_.context_digest
            evidence_total=$_.evidence_total
            diagnostic_count=$_.diagnostic_count
            critical_context_omitted=$_.critical_context_omitted
            planner_response_received=$_.planner_response_received
            failure_causal_receipt_count=$_.failure_causal_receipt_count
            last_failure_category=$_.last_failure_category
            planner_failure_action_ids=@($_.planner_failure_action_ids)
            prerequisite_transition_stage=$_.prerequisite_transition_stage
            strategy_candidate_count=$_.strategy_candidate_count
            strategy_candidate_route_ids=@($_.strategy_candidate_route_ids)
            strategy_candidate_statuses=@($_.strategy_candidate_statuses)
            snapshot_source=$_.snapshot_source
            next_category=$_.next_category
            active_goal_gap_count=$_.active_goal_gap_count
            source_requirement=$_.source_requirement
            terminal_readiness_status=$_.terminal_readiness_status
            goal_context_sha256=$_.context_sha256
        }
    })
    Write-Json (Join-Path $stage 'planner_context_delivery.json') @($delivery)

    $preflight = @($timeline.ToArray() | Where-Object {
        $_.event -in @('preexecution_strategy_replan_evaluated',
                       'preexecution_strategy_revision_unavailable',
                       'progress_strategy_revision_recorded')
    } | ForEach-Object {
        [ordered]@{
            source=$_.source; round=$_.round; event=$_.event
            status=$_.status; reason=$_.reason; reason_code=$_.reason_code
            revision=$_.revision; authorization_granted=$_.authorization_granted
        }
    })
    Write-Json (Join-Path $stage 'replan_preflight_summary.json') @($preflight)

    $scopePreflights = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'tool_started' -and $_.scope_allowed -eq $false
    } | ForEach-Object {
        [ordered]@{
            round=$_.round; action_id=$_.action_id; tool=$_.tool
            scope_reason_code=$_.scope_reason_code
            scope_category=$_.scope_category
            scope_path_role=$_.scope_path_role
            scope_transition=$_.scope_transition
            scope_required_field=$_.scope_required_field
            scope_allowed=$false
            filesystem_absence_verified=$false
        }
    })
    # Preserve rejected admission-only CWD/Scope checks; they have NO
    # tool_started and must not be described as executed operations.
    $admissionRejections = @($timeline.ToArray() | Where-Object {
        $_.event -in @('protocol_rejected','minimal_goal_action_rejected')
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            event=$_.event; tool=$_.tool
            reason_code=$_.reason_code; repair_mode=$_.repair_mode
            proposed_operation=$_.proposed_operation
            proposal_sha256=$_.proposal_sha256
            goal_context_sha256=$_.goal_context_sha256
            diagnostic_reasons=@($_.diagnostic_reasons)
            diagnostic_markers=@($_.diagnostic_markers)
            field_issues=@($_.field_issues_codes)
            unified_contract_issue_diffs=@($_.unified_contract_issue_diffs)
            admission_scope_diagnostics=@($_.admission_scope_diagnostics)
            command_reject_field=$_.command_reject_field
            executed=$false
            authorization_granted=$false
        }
    })
    # Combine timeline and executor-only Blackboard admission rejections.
    # Neither source can authorize execution or fabricate ActionResult evidence.
    $admissionRejections = @($admissionRejections) + @($executorRejections)
    Write-Json (Join-Path $stage 'action_admission_rejections.json') @($admissionRejections)
    # A denied v10 tool has no ToolResult. Preserve Scope denials without
    # interpreting an unexecuted action as a filesystem observation.
    $minimalScopeDenials = @($admissionRejections | Where-Object {
        $_.event -eq 'minimal_goal_action_rejected' -and
        [string]$_.reason_code -like 'scope_rejected:*'
    } | ForEach-Object {
        [ordered]@{
            round=$_.round; tool=$_.tool; action_id=$null
            scope_reason_code=$_.reason_code
            scope_allowed=$false; executed=$false
            filesystem_absence_verified=$false; authorization_granted=$false
        }
    })
    $scopePreflights = @($scopePreflights) + @($minimalScopeDenials)
    $beforeDispatch = @($admissionRejections | Where-Object {
        @($_.diagnostic_reasons | Where-Object {
            $_ -in @('run_command_working_directory_rejected',
                     'TOOL_SCOPE_REJECTED','scope_preflight_rejected')
        }).Count -gt 0
    } | ForEach-Object {
        $firstScope = @($_.admission_scope_diagnostics | Select-Object -First 1)
        [ordered]@{
            event='ACTION_ADMISSION_REJECTED'
            round=$_.round
            action_id=$(if ($firstScope.Count -gt 0) { $firstScope[0].action_id } else { '' })
            scope_reason_code=$(if ($firstScope.Count -gt 0) { $firstScope[0].scope_reason_code } else { 'ADMISSION_SCOPE_DENIED' })
            scope_category=$(if ($firstScope.Count -gt 0) { $firstScope[0].scope_category } else { 'UNKNOWN' })
            scope_path_role=$(if ($firstScope.Count -gt 0) { $firstScope[0].scope_path_role } else { 'UNKNOWN' })
            scope_allowed=$false
            admission_only=$true
            reason_codes=@($_.diagnostic_reasons)
            command_reject_field=$_.command_reject_field
            filesystem_absence_verified=$false
        }
    })
    $scopePreflights = @($scopePreflights) + @($beforeDispatch)
    Write-Json (Join-Path $stage 'scope_preflight_summary.json') @($scopePreflights)

    $contextRepairEvents = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'protocol_rejected' -and $_.context_request_repair
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            reason_codes=@($_.diagnostic_reasons)
            field_repairs=@($_.context_request_repair)
        }
    })
    Write-Json (Join-Path $stage 'context_request_repair_summary.json') @($contextRepairEvents)

    $commandRepairs = @($timeline.ToArray() | Where-Object {
        $_.event -in @('command_repair_readiness_evaluated',
                       'command_repair_converged') -or
        ($_.event -eq 'protocol_rejected' -and
         ($_.command_repair_diagnostics -or $_.command_reject_field))
    } | ForEach-Object {
        [ordered]@{
            source=$_.source
            event=$_.event; round=$_.round
            action_id=$_.action_id
            repair_mode=$_.repair_mode
            issue_count=$_.issue_count
            previous_issue_count=$_.previous_issue_count
            current_issue_count=$_.current_issue_count
            resolved_issue_count=$_.resolved_issue_count
            introduced_issue_count=$_.introduced_issue_count
            converging=$_.converging
            full_admission_passed=$_.full_admission_passed
            issue_codes=@($_.issue_codes)
            issue_fields=@($_.issue_fields)
            command_reject_field=$_.command_reject_field
            command_repair_diagnostics=@($_.command_repair_diagnostics)
        }
    })
    Write-Json (Join-Path $stage 'command_repair_summary.json') @($commandRepairs)

    # Goal-aligned Strategy Transition: codes/refs only; no full Goal or rationale.
    $goalTransitions = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'goal_transition_accepted' -or
        ($_.event -eq 'protocol_rejected' -and
         @($_.diagnostic_reasons | Where-Object { $_ -like 'goal_transition_*' -or
                     $_ -eq 'goal_alignment_identical_denied_read_without_new_evidence' }).Count -gt 0)
    } | ForEach-Object {
        [ordered]@{
            event=$_.event; round=$_.round; source=$_.source
            kind=$_.kind
            evidence_ref=$_.evidence_ref
            goal_condition_id=$_.goal_condition_id
            evidence_classification=$_.evidence_classification
            next_tool=$_.next_tool
            authorization_granted=$_.authorization_granted
            completion_verified=$_.completion_verified
            diagnostic_reasons=@($_.diagnostic_reasons)
        }
    })
    Write-Json (Join-Path $stage 'goal_transition_summary.json') @($goalTransitions)

    # Typed, bounded Git27 causal records. Never export raw command/stream.
    $transitions = @($timeline.ToArray() | Where-Object {
        $_.event -in @('prerequisite_transition_prepared','prerequisite_transition_evaluated')
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            event=$_.event
            transition_stage=$(if ($_.transition_stage) { $_.transition_stage } else { 'MISSING' })
            progress_transition=$_.progress_transition
            access_state=$_.access_state
            scope_preflight_status=$_.scope_preflight_status
            scope_preflight_reason_code=$_.scope_preflight_reason_code
            probe_evidence_present=$_.probe_evidence_present
            latest_probe_failure_category=$_.latest_probe_failure_category
            latest_probe_failure_ref=$_.latest_probe_failure_ref
            next_failure_evaluation=$_.next_failure_evaluation
            guidance_is_evidence=$false
            authorization_granted=$false
        }
    })
    Write-Json (Join-Path $stage 'prerequisite_transition_summary.json') @($transitions)
    $failures = @($timeline.ToArray() | Where-Object {
        $_.event -in @('tool_failure_causal_receipt_recorded',
                       'minimal_goal_action_rejected')
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            action_id=$_.action_id
            task_epoch=$_.task_epoch
            tool=$_.tool
            operation=$_.operation
            result_status=$_.result_status
            result_reason=$_.result_reason
            executed=$_.executed
            exit_code=$_.exit_code
            timed_out=$_.timed_out
            failure_category=$(if ($_.event -eq 'minimal_goal_action_rejected') {
                $_.repair_mode
            } else { $_.failure_category })
            rejection_reason=$_.reason_code
            proposal_sha256=$_.proposal_sha256
            failure_kind=$(if ($_.event -eq 'minimal_goal_action_rejected') {
                'PRE_DISPATCH_ADMISSION_REJECTION'
            } else { 'EXECUTED_ACTION_FAILURE' })
            classification_verified=$_.classification_verified
            stderr_present=$_.stderr_present
            stderr_sha256=$_.stderr_sha256
            blocking_evidence_ref=$_.blocking_evidence_ref
            next_evaluation=$_.next_evaluation
            authorization_granted=$false
            raw_stderr_included=$false
        }
    })
    Write-Json (Join-Path $stage 'failure_causal_summary.json') @($failures)
    $contracts = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'unified_contract_transaction_resolved'
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            phase=$_.phase
            reason_codes=@($_.reason_codes)
            decision_codes=@($_.decision_codes)
            contract_names=@($_.contract_names)
            pending_action_ids=@($_.pending_action_ids)
            admitted_action_ids=@($_.admitted_action_ids)
            ccrc_mode=$_.ccrc_mode
            failure_count=$_.failure_count
            last_failure_category=$_.last_failure_category
            authorization_granted=$false
            executed=$false
        }
    })
    Write-Json (Join-Path $stage 'unified_contract_summary.json') @($contracts)

    # Three-stage Action lineage: Planner request shape/digest, Runtime
    # rewrite/denial, actual Execution/Evidence. IDs join events, never
    # assume a rejected, held or rewritten proposal was dispatched.
    $actionTrace = @($timeline.ToArray() | Where-Object {
        $_.event -in @('action_resolution_recorded',
                      'action_runtime_rewrite_applied',
                      'action_runtime_rewrite_candidate_rejected',
                      'tool_started','tool_completed',
                      'action_evidence_recorded',
                      'tool_failure_causal_receipt_recorded',
                      'minimal_action_proposed','minimal_action_admitted',
                      'minimal_action_semantic_rewrite',
                      'minimal_core_action_observed','minimal_goal_action_rejected')
    } | ForEach-Object {
        [ordered]@{
            round=$_.round
            action_id=$_.action_id
            tool=$_.tool
            event=$_.event
            operation=$_.operation
            proposed_operation=$_.proposed_operation
            proposal_sha256=$_.proposal_sha256
            proposed_fields=@($_.proposed_fields)
            repair_mode=$_.repair_mode
            reason_code=$_.reason_code
            goal_context_sha256=$_.goal_context_sha256
            disposition=$_.disposition
            original_command_sha256=$_.original_command_sha256
            dispatched_command_sha256=$_.dispatched_command_sha256
            changed_fields=@($_.changed_fields)
            rewrite_reason_code=$_.rewrite_reason_code
            rewrite_rule_ids=@($_.rewrite_rule_ids)
            reason_codes=@($_.reason_codes)
            scope_allowed=$_.scope_allowed
            scope_reason_code=$_.scope_reason_code
            execution_status=$_.execution_status
            result_status=$_.result_status
            result_reason=$_.result_reason
            failure_category=$_.failure_category
            executed=$_.executed
            dispatched=$_.dispatched
            evidence_kind=$_.evidence_kind
            execution_evidence_ref=$_.execution_evidence_ref
            raw_command_included=$false
            raw_result_included=$false
        }
    })
    Write-Json (Join-Path $stage 'action_causal_trace_summary.json') @($actionTrace)
    $coverageIssues = New-Object 'System.Collections.Generic.List[string]'
    if ($executorBoardStatus -eq 'INVALID') {
        $coverageIssues.Add('EXECUTOR_BLACKBOARD_UNVERIFIED')
    }
    if ($executorBoardStatus -eq 'MATCHED' -and (
        $executorExpectedRejections -lt 0 -or
        $executorObservedRejections -ne $executorExpectedRejections -or
        @($executorRejections).Count -ne $executorObservedRejections -or
        @($admissionRejections | Where-Object {
            $_.source -eq 'executor_blackboard'
        }).Count -ne $executorObservedRejections
    )) {
        $coverageIssues.Add('EXECUTOR_ADMISSION_REJECTION_COVERAGE_INCOMPLETE')
    }
    if ($timeline.Count -eq 0 -and $executorBoardStatus -ne 'MATCHED') {
        $coverageIssues.Add('NO_MATCHING_RUNTIME_EVENTS')
    }
    $missingStages = @($transitions | Where-Object {
        $_.transition_stage -in @('MISSING','[REDACTED_NONCODE]')
    }).Count
    if ($missingStages -gt 0) { $coverageIssues.Add('PREREQUISITE_TRANSITION_STAGE_MISSING') }
    $failedIDs = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'action_evidence_recorded' -and
        $_.result_status -in @('FAILED','REJECTED','PARTIAL')
    } | ForEach-Object { Safe-Code $_.action_id } |
        Where-Object { $_ -and $_ -ne '[REDACTED_NONCODE]' } | Sort-Object -Unique)
    $knownIDs = @($failures | ForEach-Object { $_.action_id } |
        Where-Object { $_ } | Sort-Object -Unique)
    $missingIDs = @($failedIDs | Where-Object { $knownIDs -cnotcontains $_ })
    if ($missingIDs.Count -gt 0) { $coverageIssues.Add('ACTION_FAILURE_CAUSAL_RECEIPT_MISSING') }
    $minimalRejectCount = @($timeline.ToArray() | Where-Object {
        $_.event -eq 'minimal_goal_action_rejected'
    }).Count
    $representedMinimalRejectCount = @($admissionRejections | Where-Object {
        $_.event -eq 'minimal_goal_action_rejected'
    }).Count
    if ($minimalRejectCount -ne $representedMinimalRejectCount) {
        $coverageIssues.Add('MINIMAL_ADMISSION_REJECTION_CAUSAL_GAP')
    }
    $minimalProposedCount = @($minimalLineage | Where-Object {
        $_.event -eq 'minimal_action_proposed'
    }).Count
    if ($minimalRejectCount -gt $minimalProposedCount) {
        $coverageIssues.Add('MINIMAL_ACTION_PROPOSAL_LINEAGE_MISSING')
    }
    if ($minimalJournalSummary.found -and -not $minimalJournalSummary.identity_matched) {
        $coverageIssues.Add('MINIMAL_JOURNAL_IDENTITY_OR_READ_UNVERIFIED')
    }
    if ($minimalJournalSummary.identity_matched -and
        $minimalJournalSummary.rejection_count -ne $minimalRejectCount) {
        $coverageIssues.Add('MINIMAL_JOURNAL_RUNTIME_REJECTION_COUNT_MISMATCH')
    }
    if (@($warnings | Where-Object { $_ -like 'Timeline capped*' }).Count -gt 0) {
        $coverageIssues.Add('TIMELINE_ROW_CAP_REACHED')
    }
    if (@($warnings | Where-Object { $_ -like 'Full scan matching row cap reached*' }).Count -gt 0) {
        $coverageIssues.Add('FULL_SCAN_MATCH_ROW_CAP_REACHED')
    }
    if (@($warnings | Where-Object { $_ -like 'SOURCE_FULL_SCAN_SIZE_LIMIT*' }).Count -gt 0) {
        $coverageIssues.Add('SOURCE_LOG_TOO_LARGE_TAIL_ONLY')
    }
    if (@($warnings | Where-Object { $_ -like 'Unable to read source*' }).Count -gt 0) {
        $coverageIssues.Add('SOURCE_LOG_UNREADABLE')
    }
    if (@($warnings | Where-Object { $_ -like 'Unparseable matching JSONL row*' }).Count -gt 0) {
        $coverageIssues.Add('MATCHING_JSONL_ROW_UNPARSEABLE')
    }
    if ($progressSummary.runtime_state -in @('PAUSED','INTERRUPTED') -and
        -not $progressSummary.interruption_reason_present) {
        $coverageIssues.Add('TASK_INTERRUPTION_REASON_MISSING')
    }
    $coverage = [ordered]@{
        schema='SMARTAGENT_RCA_CAUSAL_COVERAGE_V1'
        status=$(if ($coverageIssues.Count -gt 0) { 'PARTIAL' }
            else { 'FULL_AVAILABLE_LOG_SCAN' })
        issues=@($coverageIssues.ToArray())
        transition_count=$transitions.Count
        transition_missing_stage=$missingStages
        failed_action_count=$failedIDs.Count
        failure_receipt_count=$failures.Count
        unique_failure_action_count=$knownIDs.Count
        planner_failure_context_events=@($delivery | Where-Object {
            $_.failure_causal_receipt_count -gt 0
        }).Count
        missing_failure_action_ids=@($missingIDs | Select-Object -First 24)
        admission_rejection_count=$admissionRejections.Count
        executor_blackboard_status=$executorBoardStatus
        executor_terminal_rejection_count=$executorExpectedRejections
        executor_admission_rejection_count=$executorObservedRejections
        executor_admission_rejection_exported_count=@($executorRejections).Count
        minimal_action_proposed_count=$minimalProposedCount
        minimal_admission_rejection_count=$minimalRejectCount
        minimal_admission_rejection_represented_count=$representedMinimalRejectCount
        minimal_action_lineage_event_count=$minimalLineage.Count
        minimal_journal_found=$minimalJournalSummary.found
        minimal_journal_identity_matched=$minimalJournalSummary.identity_matched
        minimal_journal_rejection_count=$minimalJournalSummary.rejection_count
        contract_receipt_count=$contracts.Count
        action_trace_event_count=$actionTrace.Count
        planner_context_delivery_event_count=@($delivery).Count
        log_window_tail_lines=25000
        log_window_bounded=[bool](@($sourceInfo.ToArray() | Where-Object {
            $_.scan_mode -eq 'TAIL_FALLBACK'
        }).Count -gt 0)
        complete_available_log_scan=($coverageIssues.Count -eq 0)
        timeline_row_cap=3500
        raw_tool_output_included=$false
        source_integrity_guaranteed=$false
        authorization_granted=$false
    }
    Write-Json (Join-Path $stage 'rca_causal_coverage.json') $coverage
    if ($coverageIssues.Count -gt 0) {
        $warnings.Add('RCA causal coverage PARTIAL: ' + ($coverageIssues -join ','))
    }

    # RemoteEventStore persists the Telegram OUTBOUND terminal summary after
    # worker completion. Select by BOTH IDs when available; never include any
    # reply_route/chat_id, raw Telegram update, or original event payload.
    $script:telegramRedactionCount = 0
    $script:telegramTextTruncated = $false
    $telegramSummary = [ordered]@{
        schema='SMARTAGENT_TELEGRAM_TERMINAL_RCA_V1'
        found=$false
        source='localdata/runtime/remote_events.json'
        selection='NOT_FOUND'
        request_task_bound=$false
        delivery_confirmed=$false
        summary_present=$false
        summary_truncated=$false
        redaction_count=0
        terminal_event_type=''
        event_id=''
        delivery_state=''
    }
    $telegramMessage = '[TELEGRAM_TERMINAL_NOT_FOUND] No matching Telegram terminal event was persisted for the selected Task/Request.'
    $tgStoreFile = Join-Path $root 'localdata\runtime\remote_events.json'
    try {
        if (Test-Path -LiteralPath $tgStoreFile -PathType Leaf) {
            if ((Get-Item -LiteralPath $tgStoreFile).Length -gt 33554432) {
                $telegramSummary.selection = 'SOURCE_OVERSIZED'
                $warnings.Add('Telegram remote_events.json exceeds 32 MiB; skipped.')
            } else {
                $tgStore = Read-Json $tgStoreFile
                if ($null -eq $tgStore -or $null -eq $tgStore.events) {
                    $telegramSummary.selection = 'SOURCE_UNREADABLE'
                    $warnings.Add('Telegram terminal source malformed or unreadable.')
                } else {
                    $matches = @()
                    foreach ($entry in $tgStore.events.PSObject.Properties) {
                        $ev = $entry.Value
                        if ($null -eq $ev) { continue }
                        if ($ev.event_type -notin @('TASK_COMPLETED','TASK_FAILED','TASK_PAUSED','TASK_INTERRUPTED')) { continue }
                        if ([string]$ev.reply_route.transport -ne 'TELEGRAM') { continue }
                        if ($task -and [string]$ev.task_id -cne $task) { continue }
                        if ($request -and [string]$ev.request_id -cne $request) { continue }
                        $matches += $ev
                    }
                    if ($matches.Count -gt 0) {
                        $chosen = @($matches | Sort-Object -Property @{ Expression={ [double]$_.created_at }; Descending=$true })[0]
                        $p = $chosen.payload
                        $rawSummary = [string]$p.summary
                        if (-not $rawSummary -and $chosen.event_type -eq 'TASK_FAILED') {
                            $rawSummary = [string]$p.error
                        }
                        $telegramSummary.found = $true
                        $telegramSummary.selection = 'MATCHED_TERMINAL_EVENT'
                        $telegramSummary.request_task_bound = [bool]($task -and $request)
                        $telegramSummary.task_id = (Safe-Code $chosen.task_id)
                        $telegramSummary.request_id = (Safe-Code $chosen.request_id)
                        $telegramSummary.event_id = (Safe-Code $chosen.event_id)
                        $telegramSummary.terminal_event_type = (Safe-Code $chosen.event_type)
                        $telegramSummary.status = (Safe-Code $chosen.status)
                        $telegramSummary.delivery_state = (Safe-Code $chosen.delivery_state)
                        $telegramSummary.delivery_confirmed = ([string]$chosen.delivery_state -eq 'DELIVERED')
                        $telegramSummary.delivery_attempts = [int]$chosen.attempts
                        $telegramSummary.created_at_unix = [double]$chosen.created_at
                        $telegramSummary.delivered_at_unix = [double]$chosen.delivered_at
                        $telegramSummary.task_outcome = (Safe-Code $p.task_outcome)
                        $telegramSummary.summary_present = (-not [string]::IsNullOrWhiteSpace($rawSummary))
                        if ($telegramSummary.summary_present) {
                            $telegramSummary.raw_summary_sha256 = Hash-Text $rawSummary
                            $rendered = Redact-TelegramFinal $rawSummary
                            $lines = @(
                                '[TELEGRAM_WORKER_TERMINAL_SUMMARY_REDACTED]',
                                ('event_id: ' + $telegramSummary.event_id),
                                ('event_type: ' + $telegramSummary.terminal_event_type),
                                ('delivery_state: ' + $telegramSummary.delivery_state),
                                ('request_id: ' + $telegramSummary.request_id),
                                ('task_id: ' + $telegramSummary.task_id),
                                '----- SUMMARY -----',
                                $rendered
                            )
                            $telegramMessage = $lines -join [Environment]::NewLine
                        } else {
                            $telegramMessage = '[TELEGRAM_TERMINAL_EMPTY] Event exists but worker summary is empty.'
                        }
                        $telegramSummary.summary_truncated = [bool]$script:telegramTextTruncated
                        $telegramSummary.redaction_count = [int]$script:telegramRedactionCount
                        $terminal = $p.terminal_acceptance
                        if ($null -ne $terminal) {
                            $telegramSummary.terminal_acceptance = [ordered]@{
                                admission_status=(Safe-Code $terminal.admission_status)
                                decision=(Safe-Code $terminal.decision)
                                outcome=(Safe-Code $terminal.outcome)
                                runtime_state=(Safe-Code $terminal.runtime_state)
                                receipt_id=(Safe-Code $terminal.receipt_id)
                            }
                        }
                        $receipt = $p.delivery_receipt
                        if ($null -ne $receipt) {
                            $telegramSummary.delivery_receipt = [ordered]@{
                                receipt_id=(Safe-Code $receipt.receipt_id)
                                transport=(Safe-Code $receipt.transport)
                                receipt_kind=(Safe-Code $receipt.receipt_kind)
                                adapter_confirmed=[bool]$receipt.adapter_confirmed
                                reconciled_observed=[bool]$receipt.reconciled_observed
                                message_count=[int]$receipt.message_count
                                provider_message_id_count=[int]$receipt.provider_message_id_count
                            }
                        }
                        if (-not $telegramSummary.delivery_confirmed) {
                            $warnings.Add('Telegram terminal exists but delivery_state is not DELIVERED.')
                        }
                    } else {
                        $telegramSummary.selection = 'NO_MATCHING_TELEGRAM_TERMINAL_EVENT'
                    }
                }
            }
        } else {
            $telegramSummary.selection = 'SOURCE_MISSING'
        }
    } catch {
        $telegramSummary.selection = 'SOURCE_READ_ERROR'
        $telegramSummary.found = $false
        $telegramSummary.delivery_confirmed = $false
        $telegramMessage = '[TELEGRAM_TERMINAL_UNAVAILABLE] Collector could not read the persisted Telegram event.'
        $warnings.Add('Telegram outbound terminal extraction failed: ' + $_.Exception.GetType().Name)
    }
    Write-Json (Join-Path $stage 'telegram_terminal_summary.json') $telegramSummary
    [IO.File]::WriteAllText(
        (Join-Path $stage 'telegram_final_reply_redacted.txt'),
        [string]$telegramMessage, (New-Object Text.UTF8Encoding $false)
    )

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
                             'source\agent_core\planner_context_snapshot.py',
                             'source\agent_core\failure_causal_receipt.py',
                             'source\agent_core\prerequisite_transition_gate.py',
                             'source\agent_core\unified_contract_transaction.py',
                             'source\agent_core\remote_runtime_log.py',
                             'source\RemoteAgent\telegram_webagent_worker.py',
                             'source\agent_core\auto_rca_upload.py',
                             'source\agent_core\semantic_capability_resolution.py',
                             'source\agent_core\action_readiness.py',
                             'source\agent_core\context_acquisition.py',
                             'source\agent_core\progress_recovery.py',
                             'source\agent_core\goal_strategy_transition.py',
                             'source\agent_core\prerequisite_runtime_v3.py',
                             'source\agent_core\project_environment.py',
                             'source\agent_core\pr_bcd_request_scope.py',
                             'source\agent_core\dependency_adapters.py',
                             'source\agent_core\bp1_observation_terminal.py',
                             'collect.bat',
                             'config\protocol_manifest.json')) {
        $fp = Join-Path $root $relative
        if (Test-Path -LiteralPath $fp -PathType Leaf) {
            $fingerprints[$relative.Replace('\','/')] = (Hash-File $fp)
        }
    }
    $metadata = [ordered]@{
        schema='SMARTAGENT_LIVE_RCA_V3'
        created_utc=(Get-Date).ToUniversalTime().ToString('o')
        expected_base_commit=$baseSha
        installed_git_head=(Safe-Code $head)
        installed_git_branch=(Safe-Code $branch)
        selected_by=$selection
        task_id=(Safe-Code $task)
        request_id=(Safe-Code $request)
        event_count=$timeline.Count
        causal_coverage_schema='SMARTAGENT_RCA_CAUSAL_COVERAGE_V1'
        causal_coverage_status=$coverage.status
        causal_coverage_issues=@($coverage.issues)
        snapshot_count=@($snapshots).Count
        pr_a_ledger_found=[bool]$prASummary.found
        pr_bcd_ledger_found=[bool]$prBCDSummary.found
        telegram_terminal_found=[bool]$telegramSummary.found
        telegram_delivery_confirmed=[bool]$telegramSummary.delivery_confirmed
        telegram_summary_present=[bool]$telegramSummary.summary_present
        telegram_summary_truncated=[bool]$telegramSummary.summary_truncated
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
        'sanitized SCR proposal shape/digest, decision differences and context delivery digests,',
        'preexecution replan evaluation events (status codes only),',
        'scope preflight reason codes and context_request subfield repair codes; no raw paths',
        'command repair modes, field codes and convergence counters; no command text',
        'Goal transition acceptance/rejection codes with condition IDs; no Planner rationale',
        'task progress state, correlated GAR checkpoint and hashed-scope PR-A DAG edges, installed HEAD and SHA256 fingerprints.',
        'Git27: typed Prerequisite, Failure Causality, Contract, Action Admission and Action Trace summaries.',
        'Git45: Turbo decisions with risk class, reason code, policy revision and SHA256 root digests; no raw paths.',
        'Git45: Finish status-only repair diagnostics retain typed REJECTED/FAILED differences; no raw model Finish.',
        'Git27: causal coverage status explicitly reports any missing field, source truncation or parse failure.',
        'Minimal Core v10: schema-only, request-scoped JSON parser diagnostics; no raw model response.',
        'Includes: bounded redacted Telegram worker terminal summary and delivery-state receipt when available.',
        'Excluded: raw planner replies, prompts, full commands, tool outputs, browser DOM, Telegram inbound/raw payloads,',
        'tokens, credentials, paths, environment variables, and unfiltered runtime logs.',
        'Note: collector_process_l4_enabled reflects this CLI process, not a previously started Worker.',
        'Scan: stream entire available JSONL source <=128MiB; fallback to last 25000 lines for larger files.',
        'Task/Request match and 3500-row timeline cap apply; coverage flags any truncated source.',
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
    Write-Host ('[COLLECT] Telegram terminal found: ' + [bool]$telegramSummary.found + '; delivery confirmed: ' + [bool]$telegramSummary.delivery_confirmed)
    Write-Host ('[COLLECT] ZIP: ' + $zip)
    if ($timeline.Count -eq 0) {
        Write-Warning 'No matching events found. Confirm that this is the checkout actually used for Live, or pass Task/Request IDs.'
    }
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
