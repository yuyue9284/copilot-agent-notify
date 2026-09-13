param([string]$Python)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "gadget\bin\Release"
$files = @("CopilotSessions.exe", "CopilotSessions.exe.config",
           "session_probe.py", "activity.py", "outer_progress.py")
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

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "Copilot Sessions.lnk"))
$shortcut.TargetPath = Join-Path $destination "CopilotSessions.exe"
$shortcut.Arguments = '--python "' + $python + '"'
$shortcut.WorkingDirectory = $destination
$shortcut.IconLocation = $shortcut.TargetPath + ",0"
$shortcut.Description = "Live Copilot sessions from Windows and WSL"
$shortcut.Save()
Write-Host "Installed Copilot Sessions on your desktop. Double-click it to open the gadget."
