@echo off
setlocal
set "PACKAGE_DIR=%~dp0"
if exist "%PACKAGE_DIR%xianyu-diagnostics.exe" start "" "%PACKAGE_DIR%xianyu-diagnostics.exe" --diagnostics
if exist "%PACKAGE_DIR%xianyu-diagnostics.exe" exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PACKAGE_DIR%scripts\diagnostics.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
