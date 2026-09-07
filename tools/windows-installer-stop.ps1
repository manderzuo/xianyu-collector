$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host '[xianyu] Environment is missing. Nothing to stop.' -ForegroundColor Yellow
    exit 0
}
docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile stop
exit $LASTEXITCODE
