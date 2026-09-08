param([switch]$NonInteractive)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) {
    Write-Host "[xianyu] RDP residue check: $Message" -ForegroundColor Cyan
}

function Is-SuspectRdpCommand([string]$CommandLine) {
    return -not [string]::IsNullOrWhiteSpace($CommandLine) -and $CommandLine -match '(?i)rdclientax\.dll|(?:^|[\\/])mstsc(?:\.exe)?(?:\s|"|$)|(?:^|[\\/])msrdc(?:\.exe)?(?:\s|"|$)'
}

function Test-CommandTargetMissing([string]$CommandLine) {
    if (-not (Is-SuspectRdpCommand $CommandLine)) { return $false }
    $quoted = [regex]::Match($CommandLine, '"([^"]+\.(?:exe|dll))"', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($quoted.Success) { return -not (Test-Path -LiteralPath $quoted.Groups[1].Value) }
    $bare = [regex]::Match($CommandLine, '(?i)([A-Za-z]:[\\/][^\s"]+\.(?:exe|dll))')
    if ($bare.Success) { return -not (Test-Path -LiteralPath $bare.Groups[1].Value) }
    return $false
}

function Is-WslgClient([string]$Name, [string]$CommandLine) {
    return "$Name" -match '(?i)^(msrdc|mstsc)(\.exe)?$' -and "$CommandLine" -match '(?i)/wslg|wslg\.rdp|hvsocketserviceid|WSLDVC_PACKAGE'
}

Write-Step 'checking stale remote desktop client processes'
try {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $command = "$($_.CommandLine)"
        $name = "$($_.Name)"
        -not (Is-WslgClient $name $command) -and (
            (($name -match '(?i)^rdclientax(\.exe)?$') -and (Test-CommandTargetMissing $command)) -or
            ((Is-SuspectRdpCommand $command) -and (Test-CommandTargetMissing $command))
        )
    })
    foreach ($process in $processes) {
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
            Write-Step "stopped stale client process pid=$($process.ProcessId) name=$($process.Name)"
        } catch {
            Write-Step "could not stop pid=$($process.ProcessId) name=$($process.Name): $($_.Exception.Message)"
        }
    }
    if ($processes.Count -eq 0) { Write-Step 'no stale client process found' }
} catch {
    Write-Step "process scan skipped: $($_.Exception.Message)"
}

# Remove only an orphaned current-user Run value whose command explicitly
# targets the RDP ActiveX/client and whose executable is already gone.
try {
    $runPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (Test-Path -LiteralPath $runPath) {
        $runKey = Get-ItemProperty -LiteralPath $runPath
        foreach ($property in $runKey.PSObject.Properties) {
            if ($property.Name -like 'PS*') { continue }
            $command = "$($property.Value)"
            if ((Is-SuspectRdpCommand $command) -and (Test-CommandTargetMissing $command)) {
                Remove-ItemProperty -LiteralPath $runPath -Name $property.Name -Force -ErrorAction Stop
                Write-Step "removed orphaned current-user startup entry name=$($property.Name)"
            }
        }
    }
} catch {
    Write-Step "startup entry cleanup skipped: $($_.Exception.Message)"
}

# Disable, rather than delete, an orphaned scheduled task. This is reversible
# and is limited to actions that explicitly reference the RDP client/ActiveX.
try {
    foreach ($task in @(Get-ScheduledTask -ErrorAction Stop)) {
        $actions = ($task.Actions | Out-String)
        if ((Is-SuspectRdpCommand $actions) -and (Test-CommandTargetMissing $actions)) {
            Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop | Out-Null
            Write-Step "disabled orphaned scheduled task path=$($task.TaskPath)$($task.TaskName)"
        }
    }
} catch {
    Write-Step "scheduled task cleanup skipped: $($_.Exception.Message)"
}

Write-Step 'RDP residue check completed; Windows remote desktop services and system DLLs were not changed'
exit 0
