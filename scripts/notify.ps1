param(
    [ValidateSet("agentStop", "subagentStop", "notification")]
    [string]$HookEvent = "agentStop"
)

$ErrorActionPreference = "Stop"
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ($HookEvent -eq "subagentStop") {
    if (-not $env:COPILOT_NOTIFY_SUBAGENTS -or $env:COPILOT_NOTIFY_SUBAGENTS -eq "0") {
        exit 0
    }
    if ($env:COPILOT_NOTIFY_SUBAGENTS -ne "1") {
        throw "copilot-notify: COPILOT_NOTIFY_SUBAGENTS must be 0 or 1."
    }
}

# Child agentStop events share the root transcript; subagentStop owns their alert.
if ($HookEvent -eq "agentStop" -and $payload.transcriptPath) {
    $sessionHeader = Get-Content -LiteralPath $payload.transcriptPath -TotalCount 1 |
        ConvertFrom-Json
    if ($sessionHeader.type -ne "session.start" -or -not $sessionHeader.data.sessionId) {
        throw "copilot-notify: invalid transcript session header."
    }
    if (-not $payload.sessionId) {
        throw "copilot-notify: agentStop payload is missing sessionId."
    }
    if ($payload.sessionId -ne $sessionHeader.data.sessionId) {
        exit 0
    }
}

$copilotHome = if ($env:COPILOT_HOME) {
    $env:COPILOT_HOME
} else {
    Join-Path $HOME ".copilot"
}
$workspaceFile = Join-Path $copilotHome "session-state/$($payload.sessionId)/workspace.yaml"
$sessionTitle = "Untitled session"

if (Test-Path -LiteralPath $workspaceFile) {
    $nameLine = Get-Content -LiteralPath $workspaceFile |
        Where-Object { $_ -match '^name: ' } |
        Select-Object -First 1

    if ($nameLine) {
        $value = $nameLine.Substring(6)
        if ($value.StartsWith('"') -and $value.EndsWith('"')) {
            $sessionTitle = $value | ConvertFrom-Json
        } elseif ($value.StartsWith("'") -and $value.EndsWith("'")) {
            $sessionTitle = $value.Substring(1, $value.Length - 2).Replace("''", "'")
        } else {
            $sessionTitle = $value
        }
    }
}

$sessionTitle = $sessionTitle.Replace("`r", " ").Replace("`n", " ")
if ($sessionTitle.Length -gt 64) {
    $sessionTitle = $sessionTitle.Substring(0, 61) + "..."
}

$shortId = if ($payload.sessionId) {
    $payload.sessionId.Substring(0, [Math]::Min(8, $payload.sessionId.Length))
} else {
    ""
}
$title = "[Windows] $sessionTitle"
if ($shortId) {
    $title += " [#$shortId]"
}

$shortAgentId = if ($payload.agentId) {
    $payload.agentId.Substring(0, [Math]::Min(8, $payload.agentId.Length))
} else {
    ""
}

if ($HookEvent -eq "subagentStop") {
    $agentLabel = if ($payload.agentDisplayName) {
        $payload.agentDisplayName
    } elseif ($payload.agentName) {
        $payload.agentName
    } elseif ($payload.agentType) {
        $payload.agentType
    } else {
        "Subagent"
    }
    $action = "Subagent complete: $agentLabel"
    if ($shortAgentId) {
        $action += " [#$shortAgentId]"
    }
} else {
    $action = switch ($payload.notification_type) {
        "permission_prompt" {
            if ($payload.title) { $payload.title } else { "Permission needed" }
        }
        "elicitation_dialog" {
            if ($payload.title) { $payload.title } else { "Input needed" }
        }
        default { "Agent finished responding" }
    }
}
$message = "$action."
if ($payload.cwd) {
    $message += "`nGo to: $($payload.cwd)"
}

$notifier = Join-Path $PSScriptRoot "..\windows-helper\bin\copilot-notify.exe"
$tag = if ($shortAgentId) {
    $shortAgentId
} elseif ($shortId) {
    $shortId
} else {
    "copilot"
}

& $notifier --bell $title $message $tag

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
