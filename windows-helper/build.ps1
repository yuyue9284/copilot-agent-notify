$ErrorActionPreference = "Stop"

$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$windowsMetadata = Join-Path ${env:ProgramFiles(x86)} `
    "Windows Kits\10\UnionMetadata\10.0.26100.0\Windows.winmd"
$windowsRuntime = Join-Path $env:WINDIR `
    "Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll"
$systemRuntime = Join-Path ${env:ProgramFiles(x86)} `
    "Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\Facades\System.Runtime.dll"
$source = Join-Path $PSScriptRoot "CopilotNotify.cs"
$outputDirectory = Join-Path $PSScriptRoot "bin"
$output = Join-Path $outputDirectory "copilot-notify.exe"

New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

& $compiler `
    /nologo `
    /optimize+ `
    /target:exe `
    "/out:$output" `
    "/reference:$windowsMetadata" `
    "/reference:$windowsRuntime" `
    "/reference:$systemRuntime" `
    $source

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Output $output
