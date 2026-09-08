param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $PackageRoot 'app'
$PendingRoot = Join-Path $AppRoot 'updates\pending'
$LogRoot = Join-Path $AppRoot 'logs'
$LogFile = Join-Path $LogRoot 'startup.log'

function Write-ClientUpdateLog([string]$Message) {
    if (-not (Test-Path -LiteralPath $LogRoot)) { New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null }
    Add-Content -LiteralPath $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [client-update] $Message" -Encoding UTF8
}

if (-not (Test-Path -LiteralPath $PendingRoot)) { exit 0 }
$archive = Get-ChildItem -LiteralPath $PendingRoot -Filter '*.zip' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $archive) { exit 0 }

$stageRoot = Join-Path $AppRoot 'updates\work'
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
$stage = Join-Path $stageRoot ('xianyu-client-update-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    Write-ClientUpdateLog "apply_start archive=$($archive.Name)"
    Expand-Archive -LiteralPath $archive.FullName -DestinationPath $stage -Force
    $skipExact = @(
        'app\.env', 'app\.env.local', 'app\logs', 'app\static', 'app\backups',
        'app\browser_data', 'app\updates', 'resources\images'
    )
    $stageRoot = (Resolve-Path -LiteralPath $stage).Path.TrimEnd('\')
    Get-ChildItem -LiteralPath $stage -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($stageRoot.Length).TrimStart('\')
        $normalized = $relative -replace '/', '\\'
        if ($skipExact | Where-Object { $normalized -eq $_ -or $normalized.StartsWith("$_\\", [StringComparison]::OrdinalIgnoreCase) }) { return }
        $target = Join-Path $PackageRoot $relative
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
    Remove-Item -LiteralPath $archive.FullName -Force
    Get-ChildItem -LiteralPath $PendingRoot -Filter '*.zip' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Write-ClientUpdateLog 'apply_completed'
} catch {
    Write-ClientUpdateLog "apply_failed error=$($_.Exception.ToString())"
    # Keep the archive for a later retry; a failed client maintenance update
    # must never prevent the existing application from starting.
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
exit 0
