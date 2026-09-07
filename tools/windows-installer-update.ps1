$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$Handler = Join-Path $PackageRoot 'app\deploy\invoke-xianyu-update.ps1'

if (-not (Test-Path -LiteralPath $Handler)) {
    Write-Host '[xianyu] ERROR: The update handler is missing. Copy the complete installer package again.' -ForegroundColor Red
    exit 1
}

& $Handler 'xianyu-update://install?source=manual'
exit $LASTEXITCODE
