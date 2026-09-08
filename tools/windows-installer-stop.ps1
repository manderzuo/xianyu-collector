$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'
$ErrorHelper = Join-Path $AppRoot 'deploy\windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'shutdown' } else { '' }

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'The application could not be stopped.' -ErrorRecord $_ -LogPath $LogPath
    }
    exit 1
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host '[xianyu] Environment is missing. Nothing to stop.' -ForegroundColor Yellow
    Stop-XianyuLogSession
    exit 0
}
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $dockerOutput = & docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile stop 2>&1
    $dockerExitCode = $LASTEXITCODE
} finally { $ErrorActionPreference = $previousPreference }
foreach ($line in $dockerOutput) { Write-Host $line }
if ($dockerExitCode -ne 0) { throw "Docker Compose stop failed with exit code $dockerExitCode." }
Stop-XianyuLogSession
exit 0
