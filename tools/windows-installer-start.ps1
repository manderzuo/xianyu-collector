$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'
$UpdateChecker = Join-Path $AppRoot 'deploy\check-xianyu-update.ps1'
$UpdaterExecutable = Join-Path $PackageRoot 'xianyu-updater.exe'
$ChineseUpdaterExecutable = Join-Path $PackageRoot ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
$DbCredentialSync = Join-Path $AppRoot 'deploy\sync-xianyu-db-credentials.ps1'
$ProtocolRegistrar = Join-Path $AppRoot 'deploy\register-xianyu-update-protocol.ps1'
$DockerBootstrap = Join-Path $PackageRoot 'resources\docker-bootstrap.ps1'
$WslBootstrap = Join-Path $PackageRoot 'resources\prepare-wsl.ps1'
$ClientUpdateApplier = Join-Path $PackageRoot 'scripts\apply-client-update.ps1'
$ErrorHelper = Join-Path $AppRoot 'deploy\windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = ''
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$ShortcutPath = Join-Path $Desktop "$ShortcutTitle.lnk"
$LauncherPath = Join-Path $PackageRoot "$ShortcutTitle.exe"
$OldShortcutPath = Join-Path $Desktop 'Xianyu System.lnk'
$IconPath = Join-Path $AppRoot 'assets\xianyu-launcher.ico'

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'The application could not be started.' -ErrorRecord $_ -LogPath $LogPath
    }
    exit 1
}

# A signed client maintenance package is staged by the updater and applied
# before Compose starts. A failed replacement remains queued for retry and
# never prevents the existing application from starting.
if (Test-Path -LiteralPath $ClientUpdateApplier) {
    try { & $ClientUpdateApplier -PackageRoot $PackageRoot } catch { Write-Host "[xianyu] Client maintenance update skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}

# Start the startup log after client replacement. The updater now writes to a
# separate client-update.log, and this ordering also protects compatibility
# with packages that still use a single startup log during the handoff.
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'startup' } else { '' }

# Existing installations receive the WSLg repair through the client package.
# Run the lightweight mode only when guiApplications=false is not already set;
# it then exits without elevation or a WSL restart on subsequent launches.
if (Test-Path -LiteralPath $WslBootstrap) {
    try {
        & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $WslBootstrap -ConfigureWslgOnly -NonInteractive -TargetUserProfile $env:USERPROFILE *> $null
        $wslFixExitCode = $LASTEXITCODE
        if ($wslFixExitCode -ne 0) {
            Write-XianyuLog -LogPath $LogPath -Message "wslg_fix_skipped exit_code=$wslFixExitCode"
        } else {
            Write-XianyuLog -LogPath $LogPath -Message 'wslg_fix_checked'
        }
    } catch {
        Write-XianyuLog -LogPath $LogPath -Message "wslg_fix_skipped error=$($_.Exception.Message)"
    }
}

function Remove-StaleXianyuShortcuts {
    param([string]$KeepPath)

    try {
        $shell = New-Object -ComObject WScript.Shell
        Get-ChildItem -LiteralPath $Desktop -Filter '*.lnk' -File -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.FullName -eq $KeepPath) { return }
            try {
                $shortcut = $shell.CreateShortcut($_.FullName)
                $identity = "$($shortcut.TargetPath)`n$($shortcut.Arguments)`n$($shortcut.IconLocation)"
                if ($identity -match '(?is)xianyu' -and $identity -match '(?is)(start-xianyu\.ps1|[\\/]start\.bat)') {
                    Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
                }
            } catch { }
        }
    } catch { }
}

function Update-DesktopShortcut {
    try {
        if (Test-Path -LiteralPath $OldShortcutPath) { Remove-Item -LiteralPath $OldShortcutPath -Force }
        Remove-StaleXianyuShortcuts -KeepPath $ShortcutPath
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        if (Test-Path -LiteralPath $LauncherPath) {
            $shortcut.TargetPath = $LauncherPath
            $shortcut.Arguments = ''
        } else {
            $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'start.ps1')`""
        }
        $shortcut.WorkingDirectory = $PackageRoot
        $shortcut.Description = $ShortcutTitle
        if (Test-Path -LiteralPath $IconPath) { $shortcut.IconLocation = "$IconPath,0" }
        $shortcut.Save()
    } catch {
        Write-Host "[xianyu] Desktop shortcut update skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw 'Environment is missing. Run install.bat first.'
}
Write-XianyuLog -LogPath $LogPath -Message "startup_begin package_root=$PackageRoot"
Update-DesktopShortcut
if (Test-Path -LiteralPath $ProtocolRegistrar) {
    try { & $ProtocolRegistrar -ProjectRoot $AppRoot } catch { Write-Host "[xianyu] Update protocol registration skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}
& $DockerBootstrap

if (Test-Path -LiteralPath $DbCredentialSync) {
    & $DbCredentialSync -ProjectRoot $AppRoot
}

Write-XianyuLog -LogPath $LogPath -Message 'docker_compose_up_start'
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $dockerOutput = & docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --no-build 2>&1
    $dockerExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousPreference
}
foreach ($line in $dockerOutput) { Write-Host $line }
Write-XianyuLog -LogPath $LogPath -Message "docker_compose_up_end exit_code=$dockerExitCode"
if ($dockerExitCode -ne 0) { throw "Docker Compose could not start the application (exit code $dockerExitCode)." }
$frontendPort = ((Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^FRONTEND_PORT=' }) -replace '^FRONTEND_PORT=', '').Trim()
if ($frontendPort -match '^\d+$') { Start-Process "http://127.0.0.1:$frontendPort" }
Write-XianyuLog -LogPath $LogPath -Message "startup_completed frontend_port=$frontendPort"

# Start the update check after the existing stack is available. It is a
# background process so a slow registry or a failed update check never blocks
# the local application from opening. Use an argument array instead of a
# hand-built quoted command line so paths with spaces remain valid.
foreach ($candidate in @($UpdaterExecutable, $ChineseUpdaterExecutable)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try {
        $updateProcess = Start-Process -FilePath $candidate -ArgumentList '--updater' -WorkingDirectory $PackageRoot -WindowStyle Normal -PassThru
        Write-XianyuLog -LogPath $LogPath -Message "update_gui_started path=$candidate pid=$($updateProcess.Id)"
    } catch {
        Write-XianyuLog -LogPath $LogPath -Message "update_gui_start_failed error=$($_.Exception.ToString())"
    }
    break
}
if (-not (Test-Path -LiteralPath $UpdaterExecutable) -and -not (Test-Path -LiteralPath $ChineseUpdaterExecutable) -and (Test-Path -LiteralPath $UpdateChecker)) {
    $checkArguments = @('-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', $UpdateChecker)
    try {
        $checkProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $checkArguments -WindowStyle Hidden -PassThru
        Write-XianyuLog -LogPath $LogPath -Message "update_check_started_compat pid=$($checkProcess.Id)"
    } catch {
        Write-XianyuLog -LogPath $LogPath -Message "update_check_start_failed error=$($_.Exception.ToString())"
    }
}
Stop-XianyuLogSession
