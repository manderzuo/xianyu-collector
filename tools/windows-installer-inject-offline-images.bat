@echo off
setlocal
set "PACKAGE_ROOT=%~dp0"
set "REPAIR_SCRIPT=%PACKAGE_ROOT%resources\repair-offline-runtime.ps1"
if not exist "%REPAIR_SCRIPT%" (
  echo [xianyu] Offline repair script is missing.
  pause
  exit /b 2
)
echo [xianyu] Importing the bundled offline Docker images. No network pull will be used.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPAIR_SCRIPT%" -PackageRoot "%PACKAGE_ROOT%" -ForceReimport -PauseOnError
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo [xianyu] Offline image injection failed. See app\logs\repair-runtime-*.log.
  pause
  exit /b %EXIT_CODE%
)
echo [xianyu] Offline images injected and services started successfully.
pause
exit /b 0
