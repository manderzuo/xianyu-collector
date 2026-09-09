$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PackageRoot = Split-Path -Parent $ProjectRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$ShortcutPath = Join-Path $Desktop "$ShortcutTitle.lnk"
$OldShortcutPath = Join-Path $Desktop 'Xianyu System.lnk'
$IconPath = Join-Path $ProjectRoot 'assets\xianyu-launcher.ico'
$ProtocolRegistrar = Join-Path $PSScriptRoot 'register-xianyu-update-protocol.ps1'
$UpdateChecker = Join-Path $PSScriptRoot 'check-xianyu-update.ps1'
$UpdaterExecutable = Join-Path $PackageRoot 'xianyu-updater.exe'
$ChineseUpdaterExecutable = Join-Path $PackageRoot ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
$FrontendRefreshMarker = Join-Path $ProjectRoot 'updates\frontend-restart.pending'

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
        $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'start-xianyu.ps1')`""
        $shortcut.WorkingDirectory = $ProjectRoot
        $shortcut.Description = $ShortcutTitle
        if (Test-Path -LiteralPath $IconPath) { $shortcut.IconLocation = "$IconPath,0" }
        $shortcut.Save()
    } catch {
        Write-Host "[xianyu] Desktop shortcut update skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not (Test-Path $EnvFile)) {
    Write-Host '[xianyu] Local environment is missing. Run install-xianyu.bat first.' -ForegroundColor Red
    exit 1
}

& (Join-Path $PSScriptRoot 'sync-xianyu-db-credentials.ps1') -ProjectRoot $ProjectRoot
Update-DesktopShortcut
if (Test-Path -LiteralPath $ProtocolRegistrar) {
    try { & $ProtocolRegistrar -ProjectRoot $ProjectRoot } catch { Write-Host "[xianyu] Update protocol registration skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}

docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path -LiteralPath $FrontendRefreshMarker) {
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d --force-recreate --no-build --pull never --no-deps frontend
    if ($LASTEXITCODE -eq 0) { Remove-Item -LiteralPath $FrontendRefreshMarker -Force -ErrorAction SilentlyContinue }
}

$frontendPort = ((Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^FRONTEND_PORT=' }) -replace '^FRONTEND_PORT=', '').Trim()
if ($frontendPort -match '^\d+$') {
    Start-Process "http://127.0.0.1:$frontendPort/?_xianyu_start=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
}

foreach ($candidate in @($UpdaterExecutable, $ChineseUpdaterExecutable)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    Start-Process -FilePath $candidate -ArgumentList '--updater' -WorkingDirectory $PackageRoot -WindowStyle Normal | Out-Null
    break
}
if (-not (Test-Path -LiteralPath $UpdaterExecutable) -and -not (Test-Path -LiteralPath $ChineseUpdaterExecutable) -and (Test-Path -LiteralPath $UpdateChecker)) {
    & $UpdateChecker
}
