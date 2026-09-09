param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot '.env'
$GuiScript = Join-Path $PSScriptRoot 'update-xianyu-gui.ps1'
$ErrorHelper = Join-Path $PSScriptRoot 'windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogDir = Join-Path $ProjectRoot 'logs'
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir 'update-check.log'
if (Get-Command Write-XianyuLog -ErrorAction SilentlyContinue) {
    Write-XianyuLog -LogPath $LogPath -Message "check_start force=$Force"
} else {
    Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') check_start force=$Force" -Encoding UTF8
}

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'Update checker failed before the update window could finish.' -ErrorRecord $_ -LogPath $LogPath
    } else {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.MessageBox]::Show("Update checker failed.`r`n`r`n$($_.Exception.Message)`r`n`r`nLog: $LogPath", 'Xianyu updater error', 'OK', 'Error') | Out-Null
        } catch { }
    }
    exit 1
}

if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Environment file is missing: $EnvFile" }
if (-not (Test-Path -LiteralPath $GuiScript)) { throw "Update GUI script is missing: $GuiScript" }

# Keep database credentials aligned before the update window starts. This also
# repairs installations that reused a MySQL volume created by an older package.
$credentialSync = Join-Path $PSScriptRoot 'sync-xianyu-db-credentials.ps1'
if (Test-Path -LiteralPath $credentialSync) {
    & $credentialSync -ProjectRoot $ProjectRoot
}

# Run the worker in the existing STA process. This removes the old visible
# PowerShell child window while preserving the WinForms update UI.
Write-XianyuLog -LogPath $LogPath -Message "gui_direct path=$GuiScript force=$Force"
if ($Force) { & $GuiScript -ProjectRoot $ProjectRoot -Force } else { & $GuiScript -ProjectRoot $ProjectRoot }
$guiExitCode = $LASTEXITCODE
Write-XianyuLog -LogPath $LogPath -Message "gui_exit code=$guiExitCode"
if ($guiExitCode -ne 0) { throw "Update window exited with code $guiExitCode." }
exit 0
