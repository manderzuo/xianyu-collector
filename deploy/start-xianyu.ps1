$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'

if (-not (Test-Path $EnvFile)) {
    Write-Host '[xianyu] Local environment is missing. Run install-xianyu.bat first.' -ForegroundColor Red
    exit 1
}

& (Join-Path $PSScriptRoot 'check-xianyu-update.ps1')

docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$frontendPort = ((Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^FRONTEND_PORT=' }) -replace '^FRONTEND_PORT=', '').Trim()
if ($frontendPort -match '^\d+$') {
    Start-Process "http://127.0.0.1:$frontendPort"
}
