@echo off
REM Double-click to bring up portable Postgres + Redis for local development.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\services.ps1" start
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\services.ps1" status
pause
