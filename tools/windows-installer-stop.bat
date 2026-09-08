@echo off
setlocal
set "PACKAGE_DIR=%~dp0"
if exist "%PACKAGE_DIR%xianyu-stopper.exe" start "" "%PACKAGE_DIR%xianyu-stopper.exe" --stopper
if exist "%PACKAGE_DIR%xianyu-stopper.exe" exit /b 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PACKAGE_DIR%scripts\stop.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
