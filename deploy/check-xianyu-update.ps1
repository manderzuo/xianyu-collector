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

$envMap = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $envMap[$Matches[1]] = $Matches[2] }
}
# The launcher itself runs hidden to avoid a console window.  Start the GUI in
# a separate visible process; otherwise Windows may keep the WinForms window
# hidden on machines where the parent PowerShell process is hidden.
$guiArguments = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$GuiScript`" -ProjectRoot `"$ProjectRoot`""
if ($Force) { $guiArguments += ' -Force' }
Write-XianyuLog -LogPath $LogPath -Message "gui_launch path=$GuiScript force=$Force"
$guiProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $guiArguments -WindowStyle Normal -Wait -PassThru
Write-XianyuLog -LogPath $LogPath -Message "gui_exit code=$($guiProcess.ExitCode)"
if ($guiProcess.ExitCode -ne 0) { throw "Update window exited with code $($guiProcess.ExitCode)." }
exit 0
