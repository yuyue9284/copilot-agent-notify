param([string]$Python)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "gadget\bin\Release"
$files = @("CopilotSessions.exe", "CopilotSessions.exe.config",
           "session_probe.py", "activity.py", "outer_progress.py",
           "copilot-notify.exe")
foreach ($name in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $name))) {
        throw "Missing prebuilt application file: $name. Run windows-helper\build-gadget.ps1 first."
    }
}

$probe = "import os, sys; sys.exit('Windows Python 3.9+ is required') if os.name != 'nt' or sys.version_info < (3, 9) else print(sys.executable)"
if ($Python) {
    $python = & $Python -c $probe
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = & py -3 -c $probe
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = & python -c $probe
} else {
    throw "Install Windows Python 3.9+ before installing the gadget."
}
if ($LASTEXITCODE -ne 0) {
    throw "Windows Python 3.9+ is required."
}
$python = "$python".Trim()
if (-not [IO.Path]::IsPathRooted($python) -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python did not resolve to an absolute native Windows executable."
}

$destination = Join-Path $env:LOCALAPPDATA "CopilotAgentNotify\gadget"
New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($name in $files) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $destination $name)
}
# Replace the former Python host for users retaining its old launch path.
Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot) "scripts\session_gadget.py") `
    -Destination (Join-Path $destination "session_gadget.py")

$target = Join-Path $destination "CopilotSessions.exe"
$notifier = Join-Path $destination "copilot-notify.exe"
$arguments = '--python "' + $python + '"'
$description = "Live Copilot sessions from Windows and WSL"
$appId = "CopilotAgentNotify.CopilotSessions"
$programs = [Environment]::GetFolderPath("Programs")
$shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Copilot Sessions.lnk"),
    (Join-Path $programs "Copilot Sessions.lnk")
)
foreach ($shortcut in $shortcuts) {
    & $notifier --install-shortcut $shortcut $target $arguments $destination `
        $target $appId $description
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot register the Copilot Sessions shortcut and notification identity."
    }
}
Write-Host "Installed Copilot Sessions on your desktop. Double-click it to open the gadget."
