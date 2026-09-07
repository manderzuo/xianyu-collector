$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'
$UpdateChecker = Join-Path $AppRoot 'deploy\check-xianyu-update.ps1'
$DockerBootstrap = Join-Path $PackageRoot 'resources\docker-bootstrap.ps1'

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host '[xianyu] Environment is missing. Run install.bat first.' -ForegroundColor Red
    exit 1
}
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
