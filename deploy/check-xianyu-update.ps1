param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot '.env'
$GuiScript = Join-Path $PSScriptRoot 'update-xianyu-gui.ps1'

if (-not (Test-Path -LiteralPath $EnvFile) -or -not (Test-Path -LiteralPath $GuiScript)) { exit 0 }

# Keep database credentials aligned before the update window starts. This also
# repairs installations that reused a MySQL volume created by an older package.
$credentialSync = Join-Path $PSScriptRoot 'sync-xianyu-db-credentials.ps1'
if (Test-Path -LiteralPath $credentialSync) {
    & $credentialSync -ProjectRoot $ProjectRoot
}

$envMap = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $envMap[$Matches[1]] = $Matches[2] }
}
if (-not $Force -and "$($envMap['UPDATE_CHECK_ON_START'])".ToLowerInvariant() -eq 'false') { exit 0 }

& $GuiScript -ProjectRoot $ProjectRoot -Force:$Force
exit $LASTEXITCODE
