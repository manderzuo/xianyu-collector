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
# The launcher itself runs hidden to avoid a console window.  Start the GUI in
# a separate visible process; otherwise Windows may keep the WinForms window
# hidden on machines where the parent PowerShell process is hidden.
$guiArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $GuiScript, '-ProjectRoot', $ProjectRoot)
if ($Force) { $guiArguments += '-Force' }
$guiProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $guiArguments -WindowStyle Normal -Wait -PassThru
exit $guiProcess.ExitCode
