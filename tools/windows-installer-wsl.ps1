param([switch]$Elevated, [switch]$NonInteractive)

$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Fail([string]$Message, [int]$Code = 1) {
    Write-Host "[xianyu] ERROR: $Message" -ForegroundColor Red
    exit $Code
}

if (-not (Test-IsAdministrator)) {
    $argumentList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated')
    if ($NonInteractive) { $argumentList += '-NonInteractive' }
    try {
        $child = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentList -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        exit $child.ExitCode
    } catch {
        Fail 'Administrator permission is required to configure WSL and Windows optional features.'
    }
}

$processor = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1
$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object -First 1
$hypervisorPresent = $computerSystem.HypervisorPresent -eq $true
if ($processor.VirtualizationFirmwareEnabled -eq $false) {
    Write-Host '[xianyu] Firmware virtualization check returned disabled.' -ForegroundColor Yellow
    if ($hypervisorPresent) { Write-Host '[xianyu] An active hypervisor was detected, so the firmware check is treated as unreliable.' -ForegroundColor Yellow }
}
if ($processor.SecondLevelAddressTranslationExtensions -eq $false) {
    Write-Host '[xianyu] SLAT check returned disabled; WSL/Docker runtime validation will be used instead.' -ForegroundColor Yellow
}

$restartNeeded = $false
foreach ($featureName in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
    if ($feature.State -ne 'Enabled') {
        Write-Host "[xianyu] Enabling Windows feature: $featureName" -ForegroundColor Cyan
        $result = Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart
        if ($result.RestartNeeded) { $restartNeeded = $true }
    }
}

$bcdOutput = & bcdedit.exe /enum '{current}' 2>&1
if ($bcdOutput -match 'hypervisorlaunchtype\s+Off') {
    Write-Host '[xianyu] Enabling hypervisor launch at boot.' -ForegroundColor Cyan
    & bcdedit.exe /set hypervisorlaunchtype auto | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'Could not enable hypervisor launch. Run the installer as administrator.' }
    $restartNeeded = $true
}

if ($restartNeeded) {
    Write-Host '[xianyu] Windows needs to restart before WSL 2 can be updated.' -ForegroundColor Yellow
    if ($NonInteractive) {
        Write-Host '[xianyu] Restart Windows, then open the installer again to continue.' -ForegroundColor Yellow
        exit 3010
    }
    $answer = Read-Host 'Restart this computer now? (Y/N)'
    if ($answer -match '^[Yy]$') {
        shutdown.exe /r /t 10 /c 'Xianyu setup requires a restart to enable WSL 2.' | Out-Null
        Write-Host '[xianyu] The computer will restart in 10 seconds. Run install.bat after Windows starts.' -ForegroundColor Yellow
    } else {
        Write-Host '[xianyu] Restart later, then run install.bat again.' -ForegroundColor Yellow
    }
    exit 3010
}

$wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
if (-not $wsl) { Fail 'wsl.exe was not found after enabling WSL. Restart Windows and run install.bat again.' 22 }

Write-Host '[xianyu] Updating WSL.' -ForegroundColor Cyan
& wsl.exe --update
if ($LASTEXITCODE -ne 0) {
    Write-Host '[xianyu] WSL update did not complete. Docker Desktop may still offer the update during startup.' -ForegroundColor Yellow
}

& wsl.exe --set-default-version 2 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'Could not set WSL 2 as the default version. Restart Windows and run install.bat again.' 23 }

Write-Host '[xianyu] WSL 2 prerequisites are ready.' -ForegroundColor Green
exit 0
