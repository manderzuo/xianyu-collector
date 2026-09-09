param(
    [switch]$Headless
)

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$GuiScript = Join-Path $AppRoot 'deploy\update-xianyu-gui.ps1'
$UpdaterExecutable = Join-Path $PackageRoot 'xianyu-updater.exe'
$ChineseUpdaterExecutable = Join-Path $PackageRoot ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
$ErrorHelper = Join-Path $AppRoot 'deploy\windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'manual-update' } else { '' }

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'The manual updater could not be opened.' -ErrorRecord $_ -LogPath $LogPath
    }
    exit 1
}

if (-not (Test-Path -LiteralPath $GuiScript)) {
    throw 'The update GUI component is missing. Copy the complete installer package again.'
}

if ($Headless) {
    & $GuiScript -ProjectRoot $AppRoot -Headless
    $guiExitCode = $LASTEXITCODE
    Stop-XianyuLogSession
    exit $guiExitCode
}

foreach ($candidate in @($UpdaterExecutable, $ChineseUpdaterExecutable)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    $process = Start-Process -FilePath $candidate -ArgumentList '--updater' -WorkingDirectory $PackageRoot -WindowStyle Normal -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "The updater exited with code $($process.ExitCode)." }
    Stop-XianyuLogSession
    exit 0
}

# Compatibility fallback for a package without the compiled updater. The
# PowerShell GUI remains available, but it is no longer reached through the
# old URI handler chain.
& $GuiScript -ProjectRoot $AppRoot
$guiExitCode = $LASTEXITCODE
if ($guiExitCode -ne 0) { throw "The update GUI exited with code $guiExitCode." }
Stop-XianyuLogSession
exit 0
