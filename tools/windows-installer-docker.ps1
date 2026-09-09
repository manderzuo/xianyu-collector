param([switch]$Install, [switch]$NonInteractive)

$ErrorActionPreference = 'Stop'

function Find-DockerDesktop {
    $command = Get-Command 'Docker Desktop.exe' -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles} 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:LOCALAPPDATA} 'Docker\Docker Desktop.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Get-DockerProbe {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        return [pscustomobject]@{ Ready = $false; ExitCode = 127; Output = 'Docker CLI was not found.' }
    }

    try {
        $output = @(& docker info 2>&1)
        $exitCode = $LASTEXITCODE
        $text = (($output | ForEach-Object { [string]$_ }) -join ' ').Trim()
        return [pscustomobject]@{
            Ready = ($exitCode -eq 0)
            ExitCode = $exitCode
            Output = if ($text) { $text } else { 'Docker returned no diagnostic text.' }
        }
    } catch {
        return [pscustomobject]@{ Ready = $false; ExitCode = 1; Output = $_.Exception.Message }
    }
}

function Test-DockerReady {
    return (Get-DockerProbe).Ready
}

function Invoke-DockerDesktopControl([string]$Action) {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) { return $false }
    try {
        $output = @(& docker desktop $Action 2>&1)
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) {
            if (-not [string]::IsNullOrWhiteSpace([string]$line)) {
                Write-Host "[xianyu] Docker Desktop: $line" -ForegroundColor DarkCyan
            }
        }
        return $exitCode -eq 0
    } catch {
        Write-Host "[xianyu] Docker Desktop CLI control failed: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
}

function Restart-DockerDesktopApplication([string]$Path) {
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    $processes = @()
    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='Docker Desktop.exe'" |
            Where-Object {
                $_.ExecutablePath -and ([IO.Path]::GetFullPath([string]$_.ExecutablePath) -ieq $resolvedPath)
            })
    } catch {
        Write-Host "[xianyu] Could not inspect the Docker Desktop process: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    foreach ($record in $processes) {
        try {
            $process = Get-Process -Id ([int]$record.ProcessId) -ErrorAction Stop
            [void]$process.CloseMainWindow()
            if (-not $process.WaitForExit(8000)) {
                Stop-Process -Id $process.Id -Force -ErrorAction Stop
            }
        } catch {
            Write-Host "[xianyu] Docker Desktop process restart fallback could not stop PID $($record.ProcessId): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    Start-Sleep -Seconds 3
    Start-Process -FilePath $Path | Out-Null
    return $true
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds = 900)
    $startedAt = Get-Date
    $attempts = [Math]::Max(1, [int][Math]::Ceiling($TimeoutSeconds / 2))
    for ($i = 0; $i -lt $attempts; $i++) {
        $probe = Get-DockerProbe
        if ($probe.Ready) {
            Write-Host '[xianyu] Docker Desktop is ready.' -ForegroundColor Green
            return $true
        }
        if (($i % 5) -eq 0) {
            $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
            $reason = [string]$probe.Output
            if ($reason.Length -gt 280) { $reason = $reason.Substring(0, 280) + '...' }
            Write-Host "[xianyu] Waiting for Docker Desktop. elapsed=${elapsed}s exit_code=$($probe.ExitCode) reason=$reason" -ForegroundColor Cyan
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

if (Test-DockerReady) { exit 0 }

$desktop = Find-DockerDesktop
if ($desktop) {
    Write-Host '[xianyu] Docker Desktop is installed but the engine is not ready.' -ForegroundColor Yellow
    Write-Host '[xianyu] Restarting Docker Desktop after WSL maintenance.' -ForegroundColor Cyan
    $restarted = Invoke-DockerDesktopControl 'restart'
    if (-not $restarted) {
        Write-Host '[xianyu] Docker Desktop CLI restart was unavailable; restarting the desktop application.' -ForegroundColor Cyan
        Restart-DockerDesktopApplication $desktop | Out-Null
    }
    if (Wait-DockerReady -TimeoutSeconds 900) { exit 0 }
    Write-Host '[xianyu] Docker Desktop is installed but did not become ready within 15 minutes.' -ForegroundColor Red
    Write-Host '[xianyu] Open Docker Desktop, resolve the displayed error or click Try again once, then reopen the installer.' -ForegroundColor Yellow
    exit 2
}

if (-not $Install) {
    Write-Host '[xianyu] Docker Desktop is required but is not ready.' -ForegroundColor Red
    exit 2
}

$downloadUrl = 'https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe'
$bundledPath = Join-Path $PSScriptRoot 'DockerDesktopInstaller.exe'
$downloadPath = Join-Path ([IO.Path]::GetTempPath()) 'DockerDesktopInstaller.exe'
$installerPath = $bundledPath
if (Test-Path -LiteralPath $installerPath) {
    Write-Host '[xianyu] Starting the Docker Desktop installer from resources.' -ForegroundColor Cyan
} else {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host '[xianyu] Installing Docker Desktop with winget.' -ForegroundColor Cyan
        winget install --id Docker.DockerDesktop --exact --source winget --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) {
            Write-Host '[xianyu] Docker Desktop was installed. Start it and run install.bat again.' -ForegroundColor Yellow
            exit 3
        }
    }
    if ($NonInteractive) {
        throw 'Docker Desktop is missing. Install Docker Desktop, then open the installer again.'
    }
    $answer = Read-Host 'Docker Desktop is missing. Download the official installer now? (Y/N)'
    if ($answer -notmatch '^[Yy]$') { exit 4 }
    Write-Host '[xianyu] Downloading the official Docker Desktop installer.' -ForegroundColor Cyan
    Invoke-WebRequest -Uri $downloadUrl -OutFile $downloadPath -UseBasicParsing
    $installerPath = $downloadPath
}
Start-Process -FilePath $installerPath -Wait
Write-Host '[xianyu] Docker Desktop installation finished. Start Docker Desktop and run install.bat again.' -ForegroundColor Yellow
exit 3
