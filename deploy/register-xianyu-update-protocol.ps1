param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$Handler = Join-Path $ProjectRoot 'deploy\invoke-xianyu-update.ps1'
if (-not (Test-Path -LiteralPath $Handler)) { throw "Update handler not found: $Handler" }

$ProtocolRoot = 'HKCU:\Software\Classes\xianyu-update'
$CommandKey = Join-Path $ProtocolRoot 'shell\open\command'
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Command = "`"$PowerShell`" -NoProfile -ExecutionPolicy Bypass -File `"$Handler`" `"%1`""

New-Item -Path $CommandKey -Force | Out-Null
Set-Item -Path $ProtocolRoot -Value 'URL:Xianyu Update Protocol'
New-ItemProperty -Path $ProtocolRoot -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
Set-Item -Path $CommandKey -Value $Command
