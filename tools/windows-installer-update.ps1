$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$Handler = Join-Path $PackageRoot 'app\deploy\invoke-xianyu-update.ps1'
$ErrorHelper = Join-Path $AppRoot 'deploy\windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'manual-update' } else { '' }

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'The manual updater could not be opened.' -ErrorRecord $_ -LogPath $LogPath
    }
    exit 1
}

if (-not (Test-Path -LiteralPath $Handler)) {
    throw 'The update handler is missing. Copy the complete installer package again.'
}

& $Handler 'xianyu-update://install?source=manual'
$handlerExitCode = $LASTEXITCODE
if ($handlerExitCode -ne 0) { throw "The update handler exited with code $handlerExitCode." }
Stop-XianyuLogSession
exit 0
