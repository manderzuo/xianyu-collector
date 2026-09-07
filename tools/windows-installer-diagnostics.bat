@echo off
setlocal
set "PACKAGE_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PACKAGE_DIR%scripts\diagnostics.ps1"
pause
exit /b %ERRORLEVEL%
