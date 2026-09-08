@echo off
setlocal
set "PACKAGE_DIR=%~dp0"
if exist "%PACKAGE_DIR%xianyu-updater.exe" start "" "%PACKAGE_DIR%xianyu-updater.exe" --updater
if exist "%PACKAGE_DIR%xianyu-updater.exe" exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PACKAGE_DIR%scripts\update.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
