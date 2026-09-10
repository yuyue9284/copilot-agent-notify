param(
    [ValidateSet("sessionStart", "userPromptSubmitted", "agentStop", "sessionEnd")]
    [string]$HookEvent = "sessionStart"
)

$ErrorActionPreference = "Stop"
if (-not $env:ZELLIJ_SESSION_NAME -or -not $env:ZELLIJ_PANE_ID -or $env:COPILOT_NOTIFY_ICONS -eq "0") {
    exit 0
}
$pythonArgs = @()
if ($env:OS -eq "Windows_NT" -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $python = (Get-Command py).Source
    $pythonArgs = @("-3")
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $python = (Get-Command python3).Source
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
} else {
    throw "copilot-notify-icons: Python 3.9+ is required; install it or set COPILOT_NOTIFY_ICONS=0."
}
$payload = [Console]::In.ReadToEnd()
# Windows PowerShell 5.1 otherwise transcodes piped Unicode JSON through ASCII.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$payload | & $python @pythonArgs (Join-Path $PSScriptRoot "activity.py") hook $HookEvent
exit $LASTEXITCODE
