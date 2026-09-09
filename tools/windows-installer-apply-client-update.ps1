param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$WaitForProcessId = 0,
    [string]$RestartExecutable = ''
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $PackageRoot 'app'
$PendingRoot = Join-Path $AppRoot 'updates\pending'
$LogRoot = Join-Path $AppRoot 'logs'
$LogFile = Join-Path $LogRoot 'client-update.log'
$ResolvedPackageRoot = [IO.Path]::GetFullPath($PackageRoot).TrimEnd('\')

function Write-ClientUpdateLog([string]$Message) {
    if (-not (Test-Path -LiteralPath $LogRoot)) { New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null }
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [client-update] $Message"
    # startup.ps1 keeps its own session log open while this script runs. Use a
    # separate file and shared read/write access so update diagnostics cannot
    # block the actual client replacement.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $stream = $null
        $writer = $null
        try {
            $stream = New-Object System.IO.FileStream($LogFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
            $stream.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
            $writer.WriteLine($line)
            $writer.Flush()
            $writer.Dispose()
            $stream.Dispose()
            return
        } catch {
            if ($writer) { $writer.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($attempt -eq 5) { throw }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Test-PathInsidePackage([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    try {
        $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        return $full.StartsWith($ResolvedPackageRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

$restartPath = ''
if (-not [string]::IsNullOrWhiteSpace($RestartExecutable)) {
    if (-not (Test-PathInsidePackage $RestartExecutable)) {
        Write-ClientUpdateLog "restart_skipped reason=outside_package path=$RestartExecutable"
    } else {
        $restartPath = [IO.Path]::GetFullPath($RestartExecutable)
    }
}

if ($WaitForProcessId -gt 0 -and $WaitForProcessId -ne $PID) {
    $handoffDeadline = (Get-Date).AddSeconds(20)
    Write-ClientUpdateLog "handoff_wait_start pid=$WaitForProcessId"
    while ((Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue) -and (Get-Date) -lt $handoffDeadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue) {
        Write-ClientUpdateLog "handoff_wait_timeout pid=$WaitForProcessId"
    } else {
        Write-ClientUpdateLog "handoff_wait_complete pid=$WaitForProcessId"
    }
}

function Stop-PackagedLauncherProcesses([string]$StagePath) {
    # Only consider root-level EXEs shipped by this package. This deliberately
    # excludes Docker Desktop and unrelated user applications.
    $candidateNames = @{}
    Get-ChildItem -LiteralPath $StagePath -Filter '*.exe' -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $candidateNames[$_.Name.ToLowerInvariant()] = $true
    }
    if ($candidateNames.Count -eq 0) { return }

    $processes = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -and $candidateNames.ContainsKey(([IO.Path]::GetFileName($_.ExecutablePath).ToLowerInvariant())) -and
                (Test-PathInsidePackage $_.ExecutablePath) -and $_.ProcessId -ne $PID
            }
    )
    if ($processes.Count -eq 0) {
        Write-ClientUpdateLog 'process_cleanup_complete count=0'
        return
    }

    Write-ClientUpdateLog "process_cleanup_start count=$($processes.Count)"
    foreach ($processInfo in $processes) {
        $process = $null
        try {
            $process = Get-Process -Id ([int]$processInfo.ProcessId) -ErrorAction Stop
            if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
                [void]$process.CloseMainWindow()
                Write-ClientUpdateLog "process_cleanup_close pid=$($process.Id) name=$($processInfo.Name)"
            }
        } catch {
            Write-ClientUpdateLog "process_cleanup_close_failed pid=$($processInfo.ProcessId) error=$($_.Exception.Message)"
        }
    }

    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.ExecutablePath -and $candidateNames.ContainsKey(([IO.Path]::GetFileName($_.ExecutablePath).ToLowerInvariant())) -and
                    (Test-PathInsidePackage $_.ExecutablePath) -and $_.ProcessId -ne $PID
                }
        )
    } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)

    foreach ($processInfo in @($remaining)) {
        try {
            Stop-Process -Id ([int]$processInfo.ProcessId) -Force -ErrorAction Stop
            Write-ClientUpdateLog "process_cleanup_force pid=$($processInfo.ProcessId) name=$($processInfo.Name)"
        } catch {
            Write-ClientUpdateLog "process_cleanup_force_failed pid=$($processInfo.ProcessId) error=$($_.Exception.Message)"
        }
    }
    Write-ClientUpdateLog "process_cleanup_complete count=$($processes.Count) remaining=$(@($remaining).Count)"
}

function Copy-ClientFileWithRetry([string]$Source, [string]$Destination) {
    $lastError = $null
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        try {
            Copy-Item -LiteralPath $Source -Destination $Destination -Force
            return
        } catch {
            $lastError = $_
            if ($attempt -lt 8) { Start-Sleep -Milliseconds (250 * $attempt) }
        }
    }
    throw $lastError
}

if (-not (Test-Path -LiteralPath $PendingRoot)) { exit 0 }
$archive = Get-ChildItem -LiteralPath $PendingRoot -Filter '*.zip' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $archive) { exit 0 }

$stageRoot = Join-Path $AppRoot 'updates\work'
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
$stage = Join-Path $stageRoot ('xianyu-client-update-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$applySucceeded = $false
try {
    Write-ClientUpdateLog "apply_start archive=$($archive.Name)"
    Expand-Archive -LiteralPath $archive.FullName -DestinationPath $stage -Force
    $stagedExecutables = @(Get-ChildItem -LiteralPath $stage -Filter '*.exe' -File -Force -ErrorAction SilentlyContinue)
    $stagedExecutableNames = ($stagedExecutables | ForEach-Object { $_.Name }) -join ','
    Write-ClientUpdateLog "package_contents executables=$stagedExecutableNames"
    foreach ($requiredExecutable in @('xianyu-launcher.exe', 'xianyu-updater.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $stage $requiredExecutable) -PathType Leaf)) {
            throw "客户端更新包缺少入口文件：$requiredExecutable"
        }
    }
    $stagedVersion = Join-Path $stage 'app\VERSION.txt'
    if (Test-Path -LiteralPath $stagedVersion -PathType Leaf) {
        Write-ClientUpdateLog "package_version version=$((Get-Content -LiteralPath $stagedVersion -Raw).Trim())"
    }
    Stop-PackagedLauncherProcesses $stage
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
        Copy-ClientFileWithRetry -Source $_.FullName -Destination $target
    }
    Remove-Item -LiteralPath $archive.FullName -Force
    Get-ChildItem -LiteralPath $PendingRoot -Filter '*.zip' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Write-ClientUpdateLog 'apply_completed'
    $applySucceeded = $true
} catch {
    Write-ClientUpdateLog "apply_failed error=$($_.Exception.ToString())"
    # Keep the archive for a later retry; a failed client maintenance update
    # must never prevent the existing application from starting.
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}

if ($restartPath) {
    try {
        Start-Sleep -Milliseconds 300
        Start-Process -FilePath $restartPath -WorkingDirectory $ResolvedPackageRoot -WindowStyle Normal | Out-Null
        Write-ClientUpdateLog "restart_started success=$applySucceeded path=$restartPath"
    } catch {
        Write-ClientUpdateLog "restart_failed success=$applySucceeded path=$restartPath error=$($_.Exception.ToString())"
    }
}
exit 0
