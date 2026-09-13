@echo off
setlocal
set "gadget=%~dp0..\windows-helper\gadget\bin\Release\CopilotSessions.exe"
if not exist "%gadget%" (
    echo Copilot Sessions has not been built.
    echo Run windows-helper\build-gadget.ps1 from Windows PowerShell, then retry.
    echo Windows Python 3.9+ is also required.
    exit /b 1
)
start "" "%gadget%" %*
