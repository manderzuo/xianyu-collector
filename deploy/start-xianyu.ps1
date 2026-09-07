$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$ShortcutPath = Join-Path $Desktop "$ShortcutTitle.lnk"
$OldShortcutPath = Join-Path $Desktop 'Xianyu System.lnk'
$IconPath = Join-Path $ProjectRoot 'assets\xianyu-launcher.ico'

function Update-DesktopShortcut {
    try {
        if (Test-Path -LiteralPath $OldShortcutPath) { Remove-Item -LiteralPath $OldShortcutPath -Force }
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
Update-DesktopShortcut

& (Join-Path $PSScriptRoot 'check-xianyu-update.ps1')

docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$frontendPort = ((Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^FRONTEND_PORT=' }) -replace '^FRONTEND_PORT=', '').Trim()
if ($frontendPort -match '^\d+$') {
    Start-Process "http://127.0.0.1:$frontendPort"
}
