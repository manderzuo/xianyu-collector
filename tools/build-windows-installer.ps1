param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'xianyu-one-click-installer'),
    [switch]$Force,
    [switch]$IncludeDockerImages,
    [string]$ImageSourceRegistry = 'ghcr.io',
    [string]$ImageSourceNamespace = 'manderzuo/xianyu-collector',
    [string]$ImageSourceTag = '',
    [string]$OfflineTempDirectory = ''
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
    '.git', '.pytest_cache', '__pycache__', '.mypy_cache', '.ruff_cache', '.venv', 'venv', 'node_modules', 'dist', 'build',
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
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-apply-client-update.ps1') -Destination (Join-Path $ScriptsRoot 'apply-client-update.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-docker.ps1') -Destination (Join-Path $ResourcesRoot 'docker-bootstrap.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-wsl.ps1') -Destination (Join-Path $ResourcesRoot 'prepare-wsl.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'import-offline-image-bundle.ps1') -Destination (Join-Path $ResourcesRoot 'import-offline-image-bundle.ps1') -Force
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
$launcherBuilder = Join-Path $PSScriptRoot 'build-launcher.ps1'
if (Test-Path -LiteralPath $launcherBuilder) {
    & $launcherBuilder -OutputDirectory $OutputDirectory -Force
    if ($LASTEXITCODE -ne 0) { throw "Launcher build failed with exit code $LASTEXITCODE." }
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
    launcher_entry = ((-join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    installer_entry = ((-join ([char[]](0x5b89, 0x88c5, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    maintenance_entries = @(
        ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'),
        ((-join ([char[]](0x505c, 0x6b62, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'),
        ((-join ([char[]](0x8bca, 0x65ad, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    )
    data_location = 'Docker named volumes managed by the generated compose project'
    offline_images = [bool]$IncludeDockerImages
    offline_image_manifest = if ($IncludeDockerImages) { 'resources/images/offline-manifest.json' } else { $null }
    requires = if ($IncludeDockerImages) {
        @('Windows 10 or later', 'Docker Desktop with Linux containers', 'At least 5 GB of free disk space for image import and Docker volumes')
    } else {
        @('Windows 10 or later', 'Docker Desktop with Linux containers', 'Internet access for the first image build unless images are preloaded')
    }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'package-manifest.json') -Encoding UTF8

if ($IncludeDockerImages) {
    $offlineBuilder = Join-Path $PSScriptRoot 'build-offline-image-bundle.ps1'
    if (-not (Test-Path -LiteralPath $offlineBuilder)) { throw "Offline image builder not found: $offlineBuilder" }
    $offlineArguments = @{
        PackageRoot = $OutputDirectory
        Version = $version
        SourceRegistry = $ImageSourceRegistry
        SourceNamespace = $ImageSourceNamespace
        SourceTag = $ImageSourceTag
    }
    if (-not [string]::IsNullOrWhiteSpace($OfflineTempDirectory)) {
        $offlineArguments.TempDirectory = $OfflineTempDirectory
    }
    & $offlineBuilder @offlineArguments
    if ($LASTEXITCODE -ne 0) { throw "Offline image bundle creation failed with exit code $LASTEXITCODE." }
}

$launcherDisplayName = (-join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$installerDisplayName = (-join ([char[]](0x5b89, 0x88c5, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$updaterDisplayName = (-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$stopperDisplayName = (-join ([char[]](0x505c, 0x6b62, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$diagnosticsDisplayName = (-join ([char[]](0x8bca, 0x65ad, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$centralDeploymentDoc = -join ([char[]](0x817e, 0x8baf, 0x4e91, 0x4e2d, 0x5fc3, 0x5316, 0x90e8, 0x7f72, 0x8bf4, 0x660e))
$readme = @'
Xianyu One-Click Installer

Copy this whole folder to another Windows computer. Do not move only one file.

1. Install or start Docker Desktop.
2. Double-click __INSTALLER__ (install.bat remains available as a compatibility fallback).
3. Open the desktop shortcut named __LAUNCHER__.

On the first run, install.bat checks and configures WSL 2 prerequisites. Windows may ask for administrator permission and a restart. Run install.bat again after the restart.

The installer uses its own folder as the project root. It does not depend on a fixed drive or user path.
The installer chooses free ports beginning at 20000 and never uses the old 19000 deployment.
The app source, compose file, environment template, scripts and Docker bootstrap helper are all under this folder.

This package is for standalone deployment. If multiple computers must share users and registration approvals, deploy one central copy on your Tencent Cloud server and let all computers access that same URL. Do not run separate local databases for that scenario. See app\docs\__CENTRAL_DOC__.md.

Docker Desktop is not included in this package. Install Docker Desktop separately, then run install.bat.
You may also place your installer at resources\DockerDesktopInstaller.exe before copying the package.
If Docker Desktop is missing, install.bat first tries winget and then offers to download the official installer.
The target computer needs administrator permission for Docker Desktop installation.

An offline package may include all application, MySQL and Redis images. When resources\images\offline-manifest.json is present, install.bat imports those images locally and does not download Docker images.
Without an offline image bundle, first startup builds the four application images and downloads base images. This can take several minutes.
After installation, __LAUNCHER__ starts the existing containers without deleting data.
__UPDATER__, __STOPPER__ and __DIAGNOSTICS__ are GUI
maintenance shortcuts; the original update.bat, stop.bat and diagnostics.bat remain available
as compatibility fallbacks.
Each launcher start checks the Tencent-hosted release
manifest. If a newer image release is available, the window shows the release notes and
asks for confirmation, then downloads only changed Tencent-hosted image archives, verifies
their SHA-256 values, imports them and restarts the services while preserving Docker
volumes. The detailed update log is saved at app\logs\update.log, including the manifest
response, download and Docker command output, exit codes and post-failure container status.
If the release also contains a signed client maintenance package, the updater verifies its
SHA-256 value and stages it for safe replacement at the next launcher start. This updates
the GUI and maintenance scripts without replacing files in the middle of a running update.
A temporary network or registry failure is logged and the current installation still starts.
The installer also synchronizes the application database credentials with an existing
MySQL container before the services start, so an update does not break an existing database.
If an older package left a garbled desktop shortcut, copy this package over the same
installation folder and run start.bat once. The launcher will remove the stale Xianyu
shortcut and recreate it with the correct Chinese name.
The launcher can stop containers, check updates and open diagnostics without a console window.
The original stop.bat, diagnostics.bat and update.bat remain available for recovery.
Launcher failures are also
recorded in app\logs\updater-launch.log and shown in a visible error dialog.
diagnostics.bat also creates app\logs\diagnostics-latest.txt, opens it in
Notepad, and includes Docker status, recent logs from every service, and cloud
connectivity checks. It does not dump app\.env or intentionally collect secrets;
review service logs before sharing because they may contain application data.
Startup, installation, shutdown and update-check logs are saved as startup.log,
install.log, shutdown.log and update-check.log in app\logs. If a PowerShell
operation fails, a persistent window displays the full error and provides
buttons to copy it or open the log folder. The window closes only when you
click Close.

Default first-login credentials are created by the application. Change them after first login.
Never share app\.env: it contains database passwords, JWT secrets and external API keys.
'@
$readme = $readme.Replace('__INSTALLER__', $installerDisplayName)
$readme = $readme.Replace('__LAUNCHER__', $launcherDisplayName)
$readme = $readme.Replace('__UPDATER__', $updaterDisplayName)
$readme = $readme.Replace('__STOPPER__', $stopperDisplayName)
$readme = $readme.Replace('__DIAGNOSTICS__', $diagnosticsDisplayName)
$readme = $readme.Replace('__CENTRAL_DOC__', $centralDeploymentDoc)
$readme | Set-Content -LiteralPath (Join-Path $OutputDirectory 'README.txt') -Encoding UTF8

Write-Host "Package created: $OutputDirectory" -ForegroundColor Green
Write-Host "Run: $OutputDirectory\install.bat" -ForegroundColor Cyan
