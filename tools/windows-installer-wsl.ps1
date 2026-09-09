param(
    [switch]$Elevated,
    [switch]$NonInteractive,
    [switch]$ConfigureWslgOnly,
    [string]$TargetUserProfile = ''
)

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

function Quote-Argument([string]$Value) {
    return '"' + ($Value -replace '"', '\\"') + '"'
}

function Set-WslgDisabled([string]$ProfilePath) {
    $profileRoot = [IO.Path]::GetFullPath($ProfilePath).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $profileRoot -PathType Container)) {
        Fail "The target Windows user profile was not found: $profileRoot"
    }

    $configPath = Join-Path $profileRoot '.wslconfig'
    $oldText = if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        [IO.File]::ReadAllText($configPath)
    } else { '' }
    $oldNormalized = $oldText -replace "`r`n|`r", "`n"
    $lines = if ($oldText.Length -gt 0) { @([regex]::Split($oldText, '\r\n|\n|\r')) } else { @() }
    $newLines = New-Object System.Collections.Generic.List[string]
    $wsl2Start = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '(?i)^\s*\[wsl2\]\s*$') { $wsl2Start = $i; break }
    }

    if ($wsl2Start -ge 0) {
        $wsl2End = $lines.Count
        for ($i = $wsl2Start + 1; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match '(?i)^\s*\[[^\]]+\]\s*$') { $wsl2End = $i; break }
        }
        $settingIndex = -1
        for ($i = $wsl2Start + 1; $i -lt $wsl2End; $i++) {
            if ($lines[$i] -match '(?i)^\s*guiApplications\s*=') { $settingIndex = $i; break }
        }
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($i -eq $settingIndex) { [void]$newLines.Add('guiApplications=false') }
            else { [void]$newLines.Add([string]$lines[$i]) }
            if ($i -eq ($wsl2End - 1) -and $settingIndex -lt 0) {
                [void]$newLines.Add('guiApplications=false')
            }
        }
    } else {
        foreach ($line in $lines) { [void]$newLines.Add([string]$line) }
        if ($newLines.Count -gt 0 -and $newLines[$newLines.Count - 1] -ne '') { [void]$newLines.Add('') }
        [void]$newLines.Add('[wsl2]')
        [void]$newLines.Add('guiApplications=false')
    }

    $newText = ($newLines -join "`r`n")
    $newText = [regex]::Replace($newText, '(\r\n|\n|\r)+$', '') + "`r`n"
    $newNormalized = $newText -replace "`r`n|`r", "`n"
    if ($oldNormalized -eq $newNormalized) {
        Write-Host '[xianyu] WSLg is already disabled for this Windows user.' -ForegroundColor Cyan
        return $false
    }

    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $backupPath = "$configPath.xianyu-backup-$(Get-Date -Format yyyyMMddHHmmss).bak"
        Copy-Item -LiteralPath $configPath -Destination $backupPath -Force
        Write-Host "[xianyu] Backed up existing WSL config: $backupPath" -ForegroundColor Cyan
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($configPath, $newText, $utf8NoBom)
    Write-Host '[xianyu] Disabled WSLg GUI applications to prevent the RDP ActiveX popup.' -ForegroundColor Cyan
    return $true
}

function Test-WslgDisabled([string]$ProfilePath) {
    $configPath = Join-Path ([IO.Path]::GetFullPath($ProfilePath).TrimEnd('\')) '.wslconfig'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return $false }
    $inWsl2 = $false
    foreach ($line in [IO.File]::ReadAllLines($configPath)) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $inWsl2 = $Matches[1].Trim() -ieq 'wsl2'
            continue
        }
        if ($inWsl2 -and $line -match '^\s*guiApplications\s*=\s*false\s*(?:[#;].*)?$') { return $true }
    }
    return $false
}

$requestedProfile = "$TargetUserProfile".Trim()
if (-not $requestedProfile) { $requestedProfile = "$env:USERPROFILE".Trim() }
if (-not $requestedProfile) { Fail 'Could not determine the Windows user profile for .wslconfig.' }

if ($ConfigureWslgOnly -and (Test-WslgDisabled $requestedProfile)) {
    Write-Host '[xianyu] WSLg GUI applications are already disabled.' -ForegroundColor Cyan
    exit 0
}

if (-not (Test-IsAdministrator)) {
    $argumentList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Quote-Argument $PSCommandPath), '-Elevated', '-TargetUserProfile', (Quote-Argument $requestedProfile))
    if ($NonInteractive) { $argumentList += '-NonInteractive' }
    if ($ConfigureWslgOnly) { $argumentList += '-ConfigureWslgOnly' }
    try {
        $child = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentList -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        exit $child.ExitCode
    } catch {
        Fail 'Administrator permission is required to configure WSL and Windows optional features.'
    }
}

if ($ConfigureWslgOnly) {
    $wslgChanged = Set-WslgDisabled $requestedProfile
    if ($wslgChanged) {
        $wslCommand = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
        if ($wslCommand) {
            Write-Host '[xianyu] Restarting WSL so the WSLg setting takes effect.' -ForegroundColor Cyan
            & wsl.exe --shutdown
            if ($LASTEXITCODE -ne 0) {
                Write-Host '[xianyu] WSL shutdown returned a non-zero code; the setting will apply on the next WSL start.' -ForegroundColor Yellow
            }
        } else {
            Write-Host '[xianyu] wsl.exe was not found; the WSLg setting will apply when WSL is installed.' -ForegroundColor Yellow
        }
    }
    exit 0
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

$wslgChanged = Set-WslgDisabled $requestedProfile
if ($wslgChanged) {
    Write-Host '[xianyu] Restarting WSL so the WSLg setting takes effect.' -ForegroundColor Cyan
    & wsl.exe --shutdown
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[xianyu] WSL shutdown returned a non-zero code; Docker Desktop will retry the restart during bootstrap.' -ForegroundColor Yellow
    }
}

Write-Host '[xianyu] WSL 2 prerequisites are ready.' -ForegroundColor Green
exit 0
