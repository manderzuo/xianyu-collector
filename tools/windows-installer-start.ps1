$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'
$UpdateChecker = Join-Path $AppRoot 'deploy\check-xianyu-update.ps1'
$DockerBootstrap = Join-Path $PackageRoot 'resources\docker-bootstrap.ps1'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$ShortcutPath = Join-Path $Desktop "$ShortcutTitle.lnk"
$OldShortcutPath = Join-Path $Desktop 'Xianyu System.lnk'
$IconPath = Join-Path $AppRoot 'assets\xianyu-launcher.ico'

function Update-DesktopShortcut {
    try {
        if (Test-Path -LiteralPath $OldShortcutPath) { Remove-Item -LiteralPath $OldShortcutPath -Force }
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
        $shortcut.Arguments = "/c `"$(Join-Path $PackageRoot 'start.bat')`""
        $shortcut.WorkingDirectory = $PackageRoot
        $shortcut.Description = $ShortcutTitle
        if (Test-Path -LiteralPath $IconPath) { $shortcut.IconLocation = "$IconPath,0" }
        $shortcut.Save()
    } catch {
        Write-Host "[xianyu] Desktop shortcut update skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host '[xianyu] Environment is missing. Run install.bat first.' -ForegroundColor Red
    exit 1
}
Update-DesktopShortcut
try { & $DockerBootstrap } catch { Write-Host "[xianyu] $($_.Exception.Message)" -ForegroundColor Red; exit 1 }

# Check the Tencent-hosted release manifest before starting the local stack.
# The checker is deliberately best-effort: a temporary network or registry
# failure must not prevent an already-installed local version from starting.
if (Test-Path -LiteralPath $UpdateChecker) {
    try { & $UpdateChecker } catch { Write-Host "[xianyu] Update check skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}

docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$frontendPort = ((Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^FRONTEND_PORT=' }) -replace '^FRONTEND_PORT=', '').Trim()
if ($frontendPort -match '^\d+$') { Start-Process "http://127.0.0.1:$frontendPort" }
