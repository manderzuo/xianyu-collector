$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'

if (-not (Test-Path $EnvFile)) {
    Write-Host '[xianyu] Local environment is missing. Nothing to stop.' -ForegroundColor Yellow
    exit 0
}

docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile stop
exit $LASTEXITCODE
