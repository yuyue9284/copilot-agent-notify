@echo off
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0session_gadget.py"
) else (
    python "%~dp0session_gadget.py"
)
if errorlevel 1 pause
