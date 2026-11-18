@echo off
REM Wrapper that bypasses the PowerShell execution policy for this session only.
REM Usage: dev.cmd services | test | test-pg | api | worker | replay | ocr | setup
setlocal
set ROOT=%~dp0
if "%~1"=="" goto :usage

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tasks.ps1" %*
goto :eof

:usage
echo Usage: dev.cmd [setup^|services^|test^|test-pg^|api^|worker^|replay^|ocr^|demo]
