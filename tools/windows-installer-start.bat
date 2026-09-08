@echo off
setlocal
set "PACKAGE_DIR=%~dp0"
if exist "%PACKAGE_DIR%xianyu-launcher.exe" start "" "%PACKAGE_DIR%xianyu-launcher.exe" --launcher
if exist "%PACKAGE_DIR%xianyu-launcher.exe" exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PACKAGE_DIR%scripts\start.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
