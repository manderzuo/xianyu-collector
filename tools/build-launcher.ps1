param(
    [string]$OutputDirectory = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$OutputDirectory = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { "$env:XIANYU_PACKAGE_OUTPUT_DIRECTORY".Trim() } else { $OutputDirectory }
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw 'OutputDirectory is required. Choose a destination outside the source tree, for example D:\xianyu-release.'
}
$resolvedSource = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd('\')
$resolvedOutputCandidate = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd('\')
if ($resolvedOutputCandidate -eq $resolvedSource -or
    $resolvedOutputCandidate.StartsWith($resolvedSource + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $resolvedSource.StartsWith($resolvedOutputCandidate + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputDirectory must be outside the source tree.'
}
$sourceDir = Join-Path $PSScriptRoot 'launcher'
$source = Join-Path $sourceDir 'XianyuLauncher.cs'
$sourceCore = Join-Path $sourceDir 'XianyuLauncherCore.cs'
$appManifest = Join-Path $sourceDir 'app.manifest'
$icon = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\xianyu-launcher.ico'
$iconPng = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\xianyu-app-icon.png'
if (-not (Test-Path -LiteralPath $source)) { throw "Launcher source not found: $source" }
if (-not (Test-Path -LiteralPath $sourceCore)) { throw "Launcher source not found: $sourceCore" }
if (-not (Test-Path -LiteralPath $OutputDirectory)) { New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null }

$compilerCandidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $compiler) { throw 'The .NET Framework C# compiler was not found. Install the Windows .NET Framework developer tools.' }

$launcherName = (-join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$installerName = (-join ([char[]](0x5b89, 0x88c5, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$updaterName = (-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$stopperName = (-join ([char[]](0x505c, 0x6b62, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$diagnosticsName = (-join ([char[]](0x8bca, 0x65ad, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$launcherPath = Join-Path $OutputDirectory $launcherName
$installerPath = Join-Path $OutputDirectory $installerName
$arguments = @(
    '/nologo',
    '/target:winexe',
    '/platform:x64',
    '/optimize+',
    '/reference:System.dll',
    '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll',
    '/reference:System.Core.dll',
    "/out:$launcherPath"
)
if (Test-Path -LiteralPath $icon) { $arguments += "/win32icon:$icon" }
if (Test-Path -LiteralPath $iconPng) { $arguments += "/resource:$iconPng,xianyu-app-icon.png" }
if (Test-Path -LiteralPath $appManifest) { $arguments += "/win32manifest:$appManifest" }
$arguments += $sourceCore
$arguments += $source

Write-Host "[xianyu] Building desktop launcher: $launcherPath" -ForegroundColor Cyan
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed with exit code $LASTEXITCODE." }
if (-not (Test-Path -LiteralPath $launcherPath)) { throw 'Launcher compiler completed without producing an EXE.' }
Copy-Item -LiteralPath $launcherPath -Destination $installerPath -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory $updaterName) -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory $stopperName) -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory $diagnosticsName) -Force
# ASCII aliases keep the legacy BAT wrappers free of non-ASCII text. The
# launcher receives the role as an argument, so these aliases remain GUI-only.
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory 'xianyu-launcher.exe') -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory 'xianyu-installer.exe') -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory 'xianyu-updater.exe') -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory 'xianyu-stopper.exe') -Force
Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $OutputDirectory 'xianyu-diagnostics.exe') -Force
Write-Host "[xianyu] Launcher created: $launcherPath" -ForegroundColor Green
Write-Host "[xianyu] Installer entry created: $installerPath" -ForegroundColor Green
