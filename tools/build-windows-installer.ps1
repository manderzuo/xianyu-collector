param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'xianyu-one-click-installer'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot

if ((Test-Path -LiteralPath $OutputDirectory) -and -not $Force) {
    throw "Output directory already exists: $OutputDirectory. Use -Force only when replacing this package."
}

if (Test-Path -LiteralPath $OutputDirectory) {
    $resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
    $resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path
    if ($resolvedOutput -eq $resolvedSource) {
        throw 'Output directory must not be the project root.'
    }
    Remove-Item -LiteralPath $OutputDirectory -Recurse -Force
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$AppRoot = Join-Path $OutputDirectory 'app'
$ScriptsRoot = Join-Path $OutputDirectory 'scripts'
$ResourcesRoot = Join-Path $OutputDirectory 'resources'
New-Item -ItemType Directory -Path $AppRoot, $ScriptsRoot, $ResourcesRoot -Force | Out-Null

$excludedDirectoryNames = @(
    '.git', '.pytest_cache', '.venv', 'venv', 'node_modules', 'dist', 'build',
    'static', 'backups', 'browser_data', 'logs', 'release', 'xianyu-one-click-installer'
)
$excludedFileNames = @('.env', '.env.local')

function Copy-ProjectTree {
    param([string]$Source, [string]$Destination)

    $sourceFull = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    Get-ChildItem -LiteralPath $sourceFull -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceFull.Length).TrimStart('\')
        $parts = $relative -split '[\\/]'
        if ($parts | Where-Object { $excludedDirectoryNames -contains $_ }) { return }
        if ($excludedFileNames -contains $_.Name) { return }
        $target = Join-Path $Destination $relative
        $targetParent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetParent)) {
            New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

Copy-ProjectTree -Source $SourceRoot -Destination $AppRoot

# The package entry points live at the package root. The source tree is kept under app.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-install.ps1') -Destination (Join-Path $ScriptsRoot 'install.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-start.ps1') -Destination (Join-Path $ScriptsRoot 'start.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-stop.ps1') -Destination (Join-Path $ScriptsRoot 'stop.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-diagnostics.ps1') -Destination (Join-Path $ScriptsRoot 'diagnostics.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-update.ps1') -Destination (Join-Path $ScriptsRoot 'update.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-docker.ps1') -Destination (Join-Path $ResourcesRoot 'docker-bootstrap.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-wsl.ps1') -Destination (Join-Path $ResourcesRoot 'prepare-wsl.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-install.bat') -Destination (Join-Path $OutputDirectory 'install.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-start.bat') -Destination (Join-Path $OutputDirectory 'start.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-stop.bat') -Destination (Join-Path $OutputDirectory 'stop.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-diagnostics.bat') -Destination (Join-Path $OutputDirectory 'diagnostics.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-update.bat') -Destination (Join-Path $OutputDirectory 'update.bat') -Force
$iconSource = Join-Path $SourceRoot 'assets\xianyu-launcher.ico'
if (Test-Path -LiteralPath $iconSource) {
    Copy-Item -LiteralPath $iconSource -Destination (Join-Path $OutputDirectory 'xianyu-launcher.ico') -Force
}

$version = '0.0.0'
$versionPath = Join-Path $SourceRoot 'VERSION.txt'
if (Test-Path -LiteralPath $versionPath) {
    $candidate = (Get-Content -LiteralPath $versionPath -Raw).Trim()
    if ($candidate) { $version = $candidate }
}
$manifest = [ordered]@{
    product = 'xianyu-rewrite'
    package_type = 'windows-portable-docker'
    version = $version
    created_at = [DateTime]::UtcNow.ToString('o')
    source_directory = 'app'
    install_entry = 'install.bat'
    start_entry = 'start.bat'
    stop_entry = 'stop.bat'
    data_location = 'Docker named volumes managed by the generated compose project'
    requires = @('Windows 10 or later', 'Docker Desktop with Linux containers', 'Internet access for the first image build unless images are preloaded')
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'package-manifest.json') -Encoding UTF8

$readme = @'
Xianyu One-Click Installer

Copy this whole folder to another Windows computer. Do not move only one file.

1. Install or start Docker Desktop.
2. Double-click install.bat.
3. Open the desktop shortcut named 闲鱼管理系统.

On the first run, install.bat checks and configures WSL 2 prerequisites. Windows may ask for administrator permission and a restart. Run install.bat again after the restart.

The installer uses its own folder as the project root. It does not depend on a fixed drive or user path.
The installer chooses free ports beginning at 20000 and never uses the old 19000 deployment.
The app source, compose file, environment template, scripts and Docker bootstrap helper are all under this folder.

This package is for standalone deployment. If multiple computers must share users and registration approvals, deploy one central copy on your Tencent Cloud server and let all computers access that same URL. Do not run separate local databases for that scenario. See app\docs\腾讯云中心化部署说明.md.

Docker Desktop is not included in this package. Install Docker Desktop separately, then run install.bat.
You may also place your installer at resources\DockerDesktopInstaller.exe before copying the package.
If Docker Desktop is missing, install.bat first tries winget and then offers to download the official installer.
The target computer needs administrator permission for Docker Desktop installation.

First startup builds the four application images and downloads base images. This can take several minutes.
After installation, start.bat starts the existing containers without deleting data.
Each start.bat run opens the Xianyu update window and checks the Tencent-hosted release
manifest. If a newer image release is available, the window shows the release notes and
asks for confirmation, then pulls the images and restarts the services while preserving
Docker volumes. The detailed update log is saved at app\logs\update.log, including the
manifest response, Docker command output, exit codes and post-failure container status.
A temporary network or registry failure is logged and the current installation still starts.
The installer also synchronizes the application database credentials with an existing
MySQL container before the services start, so an update does not break an existing database.
If an older package left a garbled desktop shortcut, copy this package over the same
installation folder and run start.bat once. The launcher will remove the stale Xianyu
shortcut and recreate it with the correct Chinese name.
stop.bat stops containers without deleting data.
diagnostics.bat prints Docker and service status for troubleshooting.
update.bat opens the graphical updater directly. Launcher failures are also
recorded in app\logs\updater-launch.log and shown in a visible error dialog.

Default first-login credentials are created by the application. Change them after first login.
Never share app\.env: it contains database passwords, JWT secrets and external API keys.
'@
$readme | Set-Content -LiteralPath (Join-Path $OutputDirectory 'README.txt') -Encoding UTF8

Write-Host "Package created: $OutputDirectory" -ForegroundColor Green
Write-Host "Run: $OutputDirectory\install.bat" -ForegroundColor Cyan
