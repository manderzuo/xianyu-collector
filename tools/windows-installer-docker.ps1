param([switch]$Install)

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

if (Test-DockerReady) { exit 0 }

$desktop = Find-DockerDesktop
if ($desktop) {
    Start-Process -FilePath $desktop | Out-Null
    for ($i = 0; $i -lt 90; $i++) {
        if (Test-DockerReady) { exit 0 }
        Start-Sleep -Seconds 2
    }
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
    $answer = Read-Host 'Docker Desktop is missing. Download the official installer now? (Y/N)'
    if ($answer -notmatch '^[Yy]$') { exit 4 }
    Write-Host '[xianyu] Downloading the official Docker Desktop installer.' -ForegroundColor Cyan
    Invoke-WebRequest -Uri $downloadUrl -OutFile $downloadPath -UseBasicParsing
    $installerPath = $downloadPath
}
Start-Process -FilePath $installerPath -Wait
Write-Host '[xianyu] Docker Desktop installation finished. Start Docker Desktop and run install.bat again.' -ForegroundColor Yellow
exit 3
