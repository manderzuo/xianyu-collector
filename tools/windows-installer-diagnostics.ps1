$ErrorActionPreference = 'Continue'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'

Write-Host "PackageRoot: $PackageRoot"
Write-Host "PowerShell: $($PSVersionTable.PSVersion)"
Write-Host "Docker command: $([bool](Get-Command docker -ErrorAction SilentlyContinue))"
docker version
docker compose version
if (Test-Path -LiteralPath $EnvFile) {
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile ps
} else {
    Write-Host 'Environment file is missing.'
}
