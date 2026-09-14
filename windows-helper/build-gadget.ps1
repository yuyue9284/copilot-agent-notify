param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$framework = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319"
$msbuild = Join-Path $framework "MSBuild.exe"
if (-not (Test-Path -LiteralPath $msbuild)) {
    $framework = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319"
    $msbuild = Join-Path $framework "MSBuild.exe"
}
if (-not (Test-Path -LiteralPath $msbuild)) {
    throw "Install .NET Framework 4.8 with its MSBuild/C# build tools before building the gadget."
}
$project = Join-Path $PSScriptRoot "gadget\CopilotSessions.csproj"
& $msbuild $project /nologo /verbosity:minimal "/p:Configuration=$Configuration"
if ($LASTEXITCODE -ne 0) {
    throw "Gadget build failed (exit $LASTEXITCODE). See MSBuild diagnostics above."
}
$output = Join-Path $PSScriptRoot "gadget\bin\$Configuration"
foreach ($name in @("CopilotSessions.exe", "CopilotSessions.exe.config",
                    "session_probe.py", "activity.py", "outer_progress.py",
                    "copilot-notify.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $output $name))) {
        throw "Build output is incomplete: $name is missing from $output."
    }
}
Write-Host "Built ready-to-run gadget: $output\CopilotSessions.exe"
