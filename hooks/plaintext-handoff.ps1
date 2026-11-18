# One-shot plaintext task handoff for the DeepSeek child agents (Windows).
# Adapted from Utopia-V/codex-deepseek-subagent (MIT), hooks/plaintext-handoff.ps1.
# See docs/upstream-reference-map.md for the per-symbol source map.

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stage", "hook")]
    [string]$Mode,

    [ValidateSet("deepseek_flash", "deepseek_pro")]
    [string]$AgentType,

    [ValidateSet("FAST", "REACT", "SPEC", "DEEP")]
    [string]$Policy = "FAST",

    [ValidateSet("TEXT_ONLY", "VISION_TRANSLATABLE", "VISION_CRITICAL")]
    [string]$Modality = "TEXT_ONLY",

    [int]$TtlSeconds = 300,

    [string]$StateDirectory,

    [switch]$JsonEnvelope
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$validAgents = @("deepseek_flash", "deepseek_pro")
$validPolicies = @("FAST", "REACT", "SPEC", "DEEP")
$validModalities = @("TEXT_ONLY", "VISION_TRANSLATABLE", "VISION_CRITICAL")
$maxAssignmentChars = 1000000
$maxPacketChars = 200000

$codexHome = if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    $env:CODEX_HOME
} else {
    Join-Path $HOME ".codex"
}
$stateRoot = if ([string]::IsNullOrWhiteSpace($StateDirectory)) {
    Join-Path $codexHome "deepseek-router\handoff"
} else {
    [System.IO.Path]::GetFullPath($StateDirectory)
}
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
$strictUtf8WithoutBom = [System.Text.UTF8Encoding]::new($false, $true)
[Console]::InputEncoding = $utf8WithoutBom
[Console]::OutputEncoding = $utf8WithoutBom

function Stop-Handoff([string]$Message, [int]$Code) {
    [Console]::Error.WriteLine($Message)
    if ($Mode -eq "hook") {
        Write-Json ([ordered]@{ hookSpecificOutput = [ordered]@{ hookEventName = "SubagentStart"; additionalContext = "" } })
        exit 0
    }
    exit $Code
}

function Stop-TransportFailure([string]$Action, [System.Exception]$Exception) {
    Stop-Handoff "Plaintext handoff transport failure while ${Action}: $($Exception.Message)" 12
}

if ($TtlSeconds -lt 1 -or $TtlSeconds -gt 3600) {
    Stop-Handoff "TtlSeconds must be between 1 and 3600." 8
}

function Write-Json([object]$Value) {
    [Console]::Out.Write(($Value | ConvertTo-Json -Compress -Depth 8))
    [Console]::Out.Flush()
}

function Get-JsonProperty([object]$Value, [string]$Name) {
    if ($null -eq $Value) {
        return $null
    }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    $property.Value
}

function Test-OffsetTimestamp([object]$Value, [ref]$Parsed) {
    if ($Value -isnot [string] -or $Value -notmatch '(?:Z|[+-][0-9]{2}:[0-9]{2})$') {
        return $false
    }
    $timestamp = [DateTimeOffset]::MinValue
    $valid = [DateTimeOffset]::TryParse(
        $Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$timestamp
    )
    if ($valid) {
        $Parsed.Value = $timestamp
    }
    $valid
}

function Test-OptionalPacket([object]$Value, [string]$FieldName) {
    if ($null -eq $Value) {
        return $true
    }
    if ($Value -is [System.Array] -or $Value -isnot [System.Management.Automation.PSCustomObject]) {
        return $false
    }
    ($Value | ConvertTo-Json -Compress -Depth 8).Length -le $maxPacketChars
}

function Test-RouteContract([string]$Agent, [string]$ReasoningPolicy) {
    if ($Agent -notin $validAgents) {
        return [pscustomobject]@{ Valid = $false; Error = "Unknown DeepSeek agent type: '$Agent'." }
    }
    if ($ReasoningPolicy -notin $validPolicies) {
        return [pscustomobject]@{ Valid = $false; Error = "Unknown reasoning policy: '$ReasoningPolicy'." }
    }
    if ($Agent -eq "deepseek_flash" -and $ReasoningPolicy -eq "DEEP") {
        return [pscustomobject]@{
            Valid = $false
            Error = "DEEP policy requires deepseek_pro; deepseek_flash cannot accept DEEP."
        }
    }
    [pscustomobject]@{ Valid = $true; Error = $null }
}

function Get-ReasoningContext([string]$Agent, [string]$ReasoningPolicy) {
    $route = Test-RouteContract $Agent $ReasoningPolicy
    if (-not $route.Valid) {
        Stop-Handoff $route.Error 2
    }

    $execution = switch ($ReasoningPolicy) {
        "FAST" {
            "Find the minimum direct evidence needed to answer the bounded assignment. Do not expand into unrelated architecture or equivalent searches; return the supported answer once sufficient."
        }
        "REACT" {
            if ($Agent -eq "deepseek_flash") {
                "Locate the exact change and its constraints, map the assignment's acceptance criteria, then return a precise read-only proposal with affected files, patch or diff, suggested tests, and a clear split between child-verifiable and parent-owned criteria. Do not modify the workspace or claim that a proposed edit or verification was executed."
            } else {
                "Stay within the requested scope. Understand the requested result and relevant context. Implement a complete, robust, idiomatic solution within scope. Run functional verification, check child-verifiable criteria, report parent-owned criteria as unverified, and fix failures. Run QUALITY CLOSURE only when the assignment explicitly requires QUALITY CLOSURE: after the first relevant verification passes, review the resulting diff for integration points, edge/failure paths, regression risks, project conventions/invariants, and introduced workarounds or TODOs. Fix only material issues and rerun affected verification. Do not stop merely because the first functionally passing implementation exists. Do not widen scope into unrelated refactoring, speculative abstractions, optional polish, frameworks, scaffolding, or ceremony. Stop when the requested result and any required closure are verified with no concrete material gap, or report BLOCKED."
            }
        }
        "SPEC" {
            if ($Agent -eq "deepseek_flash") {
                "Trace the bounded path, collect reproducible evidence, form limited hypotheses, and eliminate obvious candidates. Conclude when supported; for concurrency, distributed invariants, fencing, security boundaries, complex architecture, conflicting modules, or edit-dependent verification, return ESCALATE_TO_PRO with a complete Evidence Packet."
            } else {
                "Inspect and trace the behavior, form distinct candidate hypotheses, test them against evidence, eliminate material alternatives, establish the root cause, then give the smallest fix or recommendation and verify it where possible. Separate observations from inferences."
            }
        }
        "DEEP" {
            "Model the system, identify invariants and material failure modes, compare only relevant alternatives, decide, act or recommend, verify, and stop. Depth is information-driven: once the available evidence distinguishes the main alternatives, move to the decision."
        }
    }

    $modelTuning = if ($Agent -eq "deepseek_flash") {
        "Use the supplied evidence directly and obey the assignment's requested output and honesty constraints. Do extra discovery only when a missing fact blocks the answer. Keep the response focused; when the policy requires escalation, return the required Evidence Packet promptly."
    } else {
        ""
    }
    $stopCondition = switch ($ReasoningPolicy) {
        "FAST" { "Stop when direct evidence supports the answer and no unresolved issue can materially change it." }
        "REACT" {
            if ($Agent -eq "deepseek_flash") {
                "Stop when the read-only proposal covers the requested result, maps the acceptance criteria, and clearly surfaces child-verifiable versus parent-owned verification. Do not claim that the implementation or its tests were executed."
            } else {
                "Stop when the requested result is implemented, child-verifiable acceptance criteria are satisfied, and any remaining parent-owned criteria are explicitly surfaced for parent verification. If the assignment explicitly requires QUALITY CLOSURE, complete one bounded review after the first relevant functional verification pass, fix concrete material issues, and rerun affected verification. Do not mistake a partial implementation or a merely runnable artifact for completion; stop when no concrete material gap remains."
            }
        }
        "SPEC" {
            if ($Agent -eq "deepseek_flash") {
                "If the supplied evidence involves concurrency, distributed invariants, fencing, security boundaries, complex architecture, conflicting modules, or edit-dependent verification, stop analysis and return ESCALATE_TO_PRO with the complete Evidence Packet; do not solve it in Flash. Otherwise stop when the bounded root cause is supported and material alternatives are eliminated."
            } else {
                "Stop after one root cause is supported, material alternatives are eliminated, and the fix or recommendation is verified where possible."
            }
        }
        "DEEP" { "Stop when information is sufficient to distinguish the main alternatives and further analysis would add completeness without changing the decision." }
    }
    [pscustomobject]@{
        PolicyContract = $execution
        ModelTuning = $modelTuning
        StopCondition = $stopCondition + " If blocked, return BLOCKED with what is missing, why it matters, and the minimum next step."
    }
}

function Test-HandoffEnvelope([object]$Value) {
    if ($null -eq $Value -or $Value -is [System.Array]) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope must be a JSON object" }
    }

    $schema = Get-JsonProperty $Value "schema"
    if (($schema -isnot [int] -and $schema -isnot [long]) -or $schema -ne 1) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope has an invalid schema" }
    }
    $envelopeAgentType = Get-JsonProperty $Value "agent_type"
    if ($envelopeAgentType -isnot [string] -or $envelopeAgentType -notin $validAgents) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope has an invalid agent type" }
    }
    $handoffID = Get-JsonProperty $Value "handoff_id"
    $parsedGuid = [Guid]::Empty
    if ($handoffID -isnot [string] -or -not [Guid]::TryParseExact($handoffID, "D", [ref]$parsedGuid)) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope has an invalid handoff id" }
    }
    $assignment = Get-JsonProperty $Value "assignment"
    if ($assignment -isnot [string] -or [string]::IsNullOrWhiteSpace($assignment)) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope assignment must not be blank" }
    }
    if ($assignment.Length -gt $maxAssignmentChars) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope assignment exceeds the maximum payload size" }
    }
    $policy = Get-JsonProperty $Value "policy"
    if ($policy -isnot [string] -or $policy -notin $validPolicies) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope has an invalid reasoning policy" }
    }
    $route = Test-RouteContract $envelopeAgentType $policy
    if (-not $route.Valid) {
        return [pscustomobject]@{ Valid = $false; Error = $route.Error }
    }
    $modality = Get-JsonProperty $Value "modality"
    if ($modality -isnot [string] -or $modality -notin $validModalities) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope has an invalid modality" }
    }
    if (-not (Test-OptionalPacket (Get-JsonProperty $Value "visual_context") "visual_context")) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope visual_context must be a JSON object within the payload limit" }
    }
    if (-not (Test-OptionalPacket (Get-JsonProperty $Value "evidence_packet") "evidence_packet")) {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff envelope evidence_packet must be a JSON object within the payload limit" }
    }

    $createdAt = [DateTimeOffset]::MinValue
    if (-not (Test-OffsetTimestamp (Get-JsonProperty $Value "created_at") ([ref]$createdAt))) {
        return [pscustomobject]@{ Valid = $false; Error = "created_at must be a valid timestamp with a UTC offset" }
    }
    $expiresAt = [DateTimeOffset]::MinValue
    if (-not (Test-OffsetTimestamp (Get-JsonProperty $Value "expires_at") ([ref]$expiresAt))) {
        return [pscustomobject]@{ Valid = $false; Error = "expires_at must be a valid timestamp with a UTC offset" }
    }
    if ($expiresAt -le $createdAt) {
        return [pscustomobject]@{ Valid = $false; Error = "expires_at must be later than created_at" }
    }

    [pscustomobject]@{
        Valid = $true
        Error = $null
        Value = $Value
        ExpiresAt = $expiresAt
    }
}

function Read-HandoffEnvelope([string]$Path) {
    try {
        $raw = [System.IO.File]::ReadAllText($Path, $strictUtf8WithoutBom)
        $value = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ Valid = $false; Error = "the handoff state is not valid UTF-8 JSON" }
    }
    Test-HandoffEnvelope $value
}

function Get-StateFiles([string]$Agent, [string]$Pattern) {
    try {
        if (-not [System.IO.Directory]::Exists($stateRoot)) {
            return @()
        }
        @([System.IO.Directory]::GetFiles($stateRoot, "$Agent.$Pattern", [System.IO.SearchOption]::TopDirectoryOnly))
    } catch {
        Stop-TransportFailure "enumerating handoff state" $_.Exception
    }
}

function Move-ToQuarantine([string]$ClaimedPath, [string]$Agent, [string]$AgentID = "unknown") {
    $safeAgentID = if ([string]::IsNullOrWhiteSpace($AgentID)) {
        "unknown"
    } else {
        ($AgentID -replace "[^A-Za-z0-9_-]", "_")
    }
    $failedPath = Join-Path $stateRoot ("{0}.failed.{1}.{2}.json" -f $Agent, $safeAgentID, [Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.File]::Move($ClaimedPath, $failedPath)
    } catch [System.IO.FileNotFoundException] {
        return $null
    } catch {
        Stop-TransportFailure "quarantining an invalid claim" $_.Exception
    }
    $failedPath
}

function Remove-ExpiredClaims([string]$Agent) {
    $now = [DateTimeOffset]::UtcNow
    foreach ($claimedPath in @(Get-StateFiles $Agent "claimed.*.json")) {
        $validation = Read-HandoffEnvelope $claimedPath
        if (-not $validation.Valid) {
            $null = Move-ToQuarantine $claimedPath $Agent
            continue
        }
        if ($validation.ExpiresAt -gt $now) {
            continue
        }
        try {
            [System.IO.File]::Delete($claimedPath)
        } catch {
            Stop-TransportFailure "cleaning an expired claim" $_.Exception
        }
    }
    foreach ($failedPath in @(Get-StateFiles $Agent "failed.*.json")) {
        $validation = Read-HandoffEnvelope $failedPath
        if ($validation.Valid -and $validation.ExpiresAt -le $now) {
            try {
                [System.IO.File]::Delete($failedPath)
            } catch {
                Stop-TransportFailure "cleaning an expired quarantine entry" $_.Exception
            }
        }
    }
}

function Invoke-WithStateLock([string]$Agent, [scriptblock]$Action) {
    try {
        $null = [System.IO.Directory]::CreateDirectory($stateRoot)
    } catch {
        Stop-TransportFailure "creating the state directory" $_.Exception
    }

    $lockPath = Join-Path $stateRoot ".$Agent.lock"
    $lockStream = $null
    try {
        $lockStream = [System.IO.FileStream]::new(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        $nativeCode = $_.Exception.HResult -band 0xFFFF
        if ($nativeCode -in @(32, 33)) {
            Stop-Handoff "A plaintext handoff state transition is already in progress." 13
        }
        Stop-TransportFailure "acquiring the state lock" $_.Exception
    } catch {
        Stop-TransportFailure "acquiring the state lock" $_.Exception
    }

    try {
        & $Action
    } finally {
        if ($null -ne $lockStream) {
            $lockStream.Dispose()
        }
    }
}

function Publish-Handoff([string]$Agent, [object]$Handoff, [bool]$ReplaceExpired) {
    $pendingPath = Join-Path $stateRoot "$Agent.pending.json"
    $temporaryPath = Join-Path $stateRoot (".{0}.staging.{1}.tmp" -f $Agent, [Guid]::NewGuid().ToString("N"))
    try {
        $stream = [System.IO.FileStream]::new(
            $temporaryPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $writer = [System.IO.StreamWriter]::new($stream, $utf8WithoutBom, 4096, $true)
        try {
            $writer.Write(($Handoff | ConvertTo-Json -Compress -Depth 8))
            $writer.Flush()
            $stream.Flush($true)
        } finally {
            $writer.Dispose()
            $stream.Dispose()
        }

        if ($ReplaceExpired) {
            [System.IO.File]::Delete($pendingPath)
        }
        [System.IO.File]::Move($temporaryPath, $pendingPath)
    } catch [System.IO.IOException] {
        if (-not $ReplaceExpired -and [System.IO.File]::Exists($pendingPath)) {
            Stop-Handoff "A $Agent handoff is already pending. Consume or remove it before staging another." 3
        }
        Stop-TransportFailure "publishing a pending handoff" $_.Exception
    } catch {
        Stop-TransportFailure "publishing a pending handoff" $_.Exception
    } finally {
        if ([System.IO.File]::Exists($temporaryPath)) {
            try {
                [System.IO.File]::Delete($temporaryPath)
            } catch {
                Stop-TransportFailure "cleaning a staged handoff temporary file" $_.Exception
            }
        }
    }
}

function Stage-Locked([string]$Agent, [string]$Assignment, [object]$VisualContext, [object]$EvidencePacket) {
    $route = Test-RouteContract $Agent $Policy
    if (-not $route.Valid) {
        Stop-Handoff $route.Error 2
    }
    Remove-ExpiredClaims $Agent
    $pendingPath = Join-Path $stateRoot "$Agent.pending.json"
    if (@(Get-StateFiles $Agent "claimed.*.json").Count -gt 0 -or @(Get-StateFiles $Agent "failed.*.json").Count -gt 0) {
        Stop-Handoff "A $Agent handoff is already claimed or quarantined. Resolve it before staging another." 3
    }

    $now = [DateTimeOffset]::UtcNow
    $replaceExpired = $false
    if ([System.IO.File]::Exists($pendingPath)) {
        $validation = Read-HandoffEnvelope $pendingPath
        if (-not $validation.Valid) {
            Stop-Handoff "The existing $Agent handoff is malformed. Refusing to replace it." 9
        }
        if ($validation.ExpiresAt -gt $now) {
            Stop-Handoff "A $Agent handoff is already pending. Let it be consumed or expire before staging another." 3
        }
        $replaceExpired = $true
    }

    $handoff = [ordered]@{
        schema = 1
        handoff_id = [Guid]::NewGuid().ToString("D")
        agent_type = $Agent
        created_at = $now.ToString("O")
        expires_at = $now.AddSeconds($TtlSeconds).ToString("O")
        assignment = $Assignment
        policy = $Policy
        modality = $Modality
        visual_context = $VisualContext
        evidence_packet = $EvidencePacket
    }
    Publish-Handoff $Agent $handoff $replaceExpired
    $handoff
}

function Run-TargetHookLocked([string]$Agent, [object]$HookInput) {
    Remove-ExpiredClaims $Agent
    $pendingPath = Join-Path $stateRoot "$Agent.pending.json"
    if (@(Get-StateFiles $Agent "claimed.*.json").Count -gt 0 -or @(Get-StateFiles $Agent "failed.*.json").Count -gt 0) {
        Stop-Handoff "A plaintext handoff is already claimed or quarantined for $Agent." 11
    }
    if (-not [System.IO.File]::Exists($pendingPath)) {
        Stop-Handoff "No plaintext handoff was available for the $Agent start." 10
    }

    $rawAgentID = [string](Get-JsonProperty $HookInput "agent_id")
    $agentID = if ([string]::IsNullOrWhiteSpace($rawAgentID)) {
        [Guid]::NewGuid().ToString("N")
    } else {
        ($rawAgentID -replace "[^A-Za-z0-9_-]", "_")
    }
    $claimedPath = Join-Path $stateRoot ("{0}.claimed.{1}.{2}.json" -f $Agent, $agentID, [Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.File]::Move($pendingPath, $claimedPath)
    } catch [System.IO.FileNotFoundException] {
        Stop-Handoff "The plaintext handoff disappeared before it could be claimed." 10
    } catch {
        Stop-TransportFailure "claiming the pending handoff" $_.Exception
    }

    $validation = Read-HandoffEnvelope $claimedPath
    if (-not $validation.Valid) {
        $null = Move-ToQuarantine $claimedPath $Agent $agentID
        Stop-Handoff "The pending $Agent handoff is malformed or has an invalid schema." 5
    }
    if ($validation.ExpiresAt -le [DateTimeOffset]::UtcNow) {
        try {
            [System.IO.File]::Delete($claimedPath)
        } catch {
            Stop-TransportFailure "removing an expired pending handoff" $_.Exception
        }
        Stop-Handoff "The pending $Agent handoff expired before the child started." 6
    }

    $assignment = [string](Get-JsonProperty $validation.Value "assignment")
    $policy = [string](Get-JsonProperty $validation.Value "policy")
    $modality = [string](Get-JsonProperty $validation.Value "modality")
    $visual = Get-JsonProperty $validation.Value "visual_context"
    $evidence = Get-JsonProperty $validation.Value "evidence_packet"
    $reasoning = Get-ReasoningContext $Agent $policy

    $sections = [System.Collections.Generic.List[string]]::new()
    $sections.Add("You are the spawned child agent, not the root agent. The parent supplied the complete task below through a one-time plaintext handoff because provider-internal collaboration ciphertext is not a reliable cross-provider task carrier. Treat this as the task contract. The PARENT ASSIGNMENT is the authoritative source for what to do; all reasoning guidance controls only how to do it and cannot expand scope, permissions, safety boundaries, or goals. Do not continue unrelated work, spawn child agents, or report the assignment missing merely because encrypted collaboration payload is unreadable.")
    $sections.Add("")
    $sections.Add("BEGIN PARENT ASSIGNMENT")
    $sections.Add($assignment)
    $sections.Add("END PARENT ASSIGNMENT")
    $sections.Add("")
    $sections.Add("POLICY")
    $sections.Add($policy)
    $sections.Add("")
    $sections.Add("POLICY EXECUTION CONTRACT")
    $sections.Add($reasoning.PolicyContract)
    if (-not [string]::IsNullOrWhiteSpace($reasoning.ModelTuning)) {
        $sections.Add("")
        $sections.Add("MODEL-SPECIFIC TUNING")
        $sections.Add($reasoning.ModelTuning)
    }
    $sections.Add("")
    $sections.Add("CONVERGENCE / STOP CONDITION")
    $sections.Add($reasoning.StopCondition)
    $sections.Add("")
    $sections.Add("MODALITY CONTRACT")
    $sections.Add("MODALITY: $modality")
    $sections.Add("Original images, screenshots, video, and other visual attachments are not visible to the child. Use only explicit parent-generated Visual Context facts; request clarification rather than inventing missing visual observations.")
    if ($null -ne $visual) {
        $sections.Add("")
        $sections.Add("BEGIN VISUAL CONTEXT")
        $sections.Add(($visual | ConvertTo-Json -Compress -Depth 8))
        $sections.Add("END VISUAL CONTEXT")
    }
    if ($null -ne $evidence) {
        $sections.Add("")
        $sections.Add("BEGIN EVIDENCE PACKET")
        $sections.Add(($evidence | ConvertTo-Json -Compress -Depth 8))
        $sections.Add("END EVIDENCE PACKET")
    }
    $additionalContext = $sections -join "`n"

    try {
        Write-Json ([ordered]@{
            hookSpecificOutput = [ordered]@{
                hookEventName = "SubagentStart"
                additionalContext = $additionalContext
            }
        })
    } catch {
        Stop-TransportFailure "delivering the claimed handoff" $_.Exception
    }

    try {
        [System.IO.File]::Delete($claimedPath)
    } catch {
        Stop-TransportFailure "consuming the claimed handoff" $_.Exception
    }
}

try {
    if ($Mode -eq "stage") {
        $rawInput = [Console]::In.ReadToEnd()
        if ($rawInput.Length -gt 0 -and $rawInput[0] -eq [char]0xFEFF) {
            $rawInput = $rawInput.Substring(1)
        }
        $assignment = $rawInput
        $visualContext = $null
        $evidencePacket = $null
        if ($JsonEnvelope) {
            try {
                $envelope = $rawInput | ConvertFrom-Json -ErrorAction Stop
            } catch {
                Stop-Handoff "Envelope input was invalid JSON." 2
            }
            if ($null -eq $envelope -or $envelope -is [System.Array]) {
                Stop-Handoff "Envelope input must be a JSON object." 2
            }
            $Agent = [string](Get-JsonProperty $envelope "agent_type")
            $assignment = [string](Get-JsonProperty $envelope "assignment")
            $Policy = [string](Get-JsonProperty $envelope "policy")
            $Modality = [string](Get-JsonProperty $envelope "modality")
            $visualContext = Get-JsonProperty $envelope "visual_context"
            $evidencePacket = Get-JsonProperty $envelope "evidence_packet"
        } else {
            $Agent = $AgentType
        }
        if ([string]::IsNullOrWhiteSpace($Agent)) {
            Stop-Handoff "Missing agent type for staging." 8
        }
        if ($Agent -notin $validAgents) {
            Stop-Handoff "Invalid agent type for staging." 8
        }
        if ([string]::IsNullOrWhiteSpace($assignment)) {
            Stop-Handoff "Refusing to stage an empty assignment." 2
        }
        $handoff = Invoke-WithStateLock $Agent { Stage-Locked $Agent $assignment $visualContext $evidencePacket }
        Write-Json ([ordered]@{
            staged = $true
            handoff_id = $handoff.handoff_id
            agent_type = $Agent
            expires_at = $handoff.expires_at
            pending_path = (Join-Path $stateRoot "$Agent.pending.json")
        })
        exit 0
    }

    $rawHookInput = [Console]::In.ReadToEnd()
    if ($rawHookInput.Length -gt 0 -and $rawHookInput[0] -eq [char]0xFEFF) {
        $rawHookInput = $rawHookInput.Substring(1)
    }
    if ([string]::IsNullOrWhiteSpace($rawHookInput)) {
        Stop-Handoff "SubagentStart hook input was empty." 4
    }
    try {
        $hookInput = $rawHookInput | ConvertFrom-Json -ErrorAction Stop
    } catch {
        Stop-Handoff "SubagentStart hook input was invalid JSON." 4
    }
    if ($null -eq $hookInput -or $hookInput -is [System.Array]) {
        Stop-Handoff "SubagentStart hook input must be a JSON object." 4
    }
    $hookAgentType = [string](Get-JsonProperty $hookInput "agent_type")
    if ((Get-JsonProperty $hookInput "hook_event_name") -ne "SubagentStart" -or $hookAgentType -notin $validAgents) {
        exit 0
    }

    Invoke-WithStateLock $hookAgentType { Run-TargetHookLocked $hookAgentType $hookInput }
    exit 0
} catch {
    Stop-TransportFailure "processing the handoff" $_.Exception
}
