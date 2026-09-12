$ErrorActionPreference = "Stop"

$probe = 'import sys; assert sys.version_info >= (3, 9); print(sys.executable)'
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = & py -3 -c $probe
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = & python -c $probe
} else {
    throw "Install Windows Python 3.9+ before installing the gadget."
}
if ($LASTEXITCODE -ne 0) {
    throw "Windows Python 3.9+ is required."
}
$pythonw = Join-Path (Split-Path $python.Trim()) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "pythonw.exe is missing from the Windows Python installation."
}

$source = Join-Path (Split-Path $PSScriptRoot) "scripts"
$destination = Join-Path $env:LOCALAPPDATA "CopilotAgentNotify\gadget"
New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($name in @("session_gadget.py", "session_probe.py", "activity.py", "outer_progress.py")) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $destination $name)
}
$ui = Join-Path $destination "gadget-ui"
New-Item -ItemType Directory -Path $ui -Force | Out-Null
foreach ($name in @("SessionWindow.cs", "SessionWindow.xaml", "app.manifest", "app.config",
                   "sessions.ico", "sessions.png")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "gadget\$name") -Destination (Join-Path $ui $name)
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop "Copilot Sessions.lnk"))
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + (Join-Path $destination "session_gadget.py") + '"'
$shortcut.WorkingDirectory = $destination
$shortcut.IconLocation = (Join-Path $ui "sessions.ico") + ",0"
$shortcut.Description = "Live Copilot sessions from Windows and WSL"
$shortcut.Save()
Write-Host "Installed Copilot Sessions on your desktop. Double-click it to open the gadget."
