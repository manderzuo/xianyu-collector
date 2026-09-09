param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$Handler = Join-Path $ProjectRoot 'deploy\invoke-xianyu-update.ps1'
if (-not (Test-Path -LiteralPath $Handler)) { throw "Update handler not found: $Handler" }

$ProtocolRoot = 'HKCU:\Software\Classes\xianyu-update'
$CommandKey = Join-Path $ProtocolRoot 'shell\open\command'
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$PackageRoot = Split-Path -Parent $ProjectRoot
$UpdaterExecutable = Join-Path $PackageRoot 'xianyu-updater.exe'
$ChineseUpdaterExecutable = Join-Path $PackageRoot ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
if (Test-Path -LiteralPath $UpdaterExecutable) {
    # The compiled updater owns the GUI and starts PowerShell hidden. Passing
    # the protocol URI is unnecessary for the current updater and would make
    # the old PowerShell handler appear in front of the user.
    $Command = "`"$UpdaterExecutable`" --updater"
} elseif (Test-Path -LiteralPath $ChineseUpdaterExecutable) {
    $Command = "`"$ChineseUpdaterExecutable`" --updater"
} else {
    $Command = "`"$PowerShell`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Handler`" `"%1`""
}

New-Item -Path $CommandKey -Force | Out-Null
Set-Item -Path $ProtocolRoot -Value 'URL:Xianyu Update Protocol'
New-ItemProperty -Path $ProtocolRoot -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
Set-Item -Path $CommandKey -Value $Command
