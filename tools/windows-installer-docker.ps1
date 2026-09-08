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

function Test-DockerReady {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) { return $false }
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds = 600)
    $startedAt = Get-Date
    $attempts = [Math]::Max(1, [int][Math]::Ceiling($TimeoutSeconds / 2))
    for ($i = 0; $i -lt $attempts; $i++) {
        if (Test-DockerReady) {
            Write-Host '[xianyu] Docker Desktop is ready.' -ForegroundColor Green
            return $true
        }
        if (($i % 5) -eq 0) {
            $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
            Write-Host "[xianyu] Waiting for Docker Desktop. If it shows Try again, click it once. elapsed=${elapsed}s" -ForegroundColor Cyan
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

if (Test-DockerReady) { exit 0 }

$desktop = Find-DockerDesktop
if ($desktop) {
    Write-Host '[xianyu] Starting Docker Desktop.' -ForegroundColor Cyan
    Start-Process -FilePath $desktop | Out-Null
    if (Wait-DockerReady -TimeoutSeconds 600) { exit 0 }
    Write-Host '[xianyu] Docker Desktop is installed but did not become ready within 10 minutes.' -ForegroundColor Red
    Write-Host '[xianyu] Click Try again in Docker Desktop, wait until it reports running, then reopen the installer.' -ForegroundColor Yellow
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
