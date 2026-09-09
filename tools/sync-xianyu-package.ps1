[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PackageRoot = 'D:\Package',
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'

function Resolve-FullPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        $Path = Join-Path (Get-Location) $Path
    }
    return [IO.Path]::GetFullPath($Path)
}

$SourceRoot = Resolve-FullPath $SourceRoot
$PackageRoot = Resolve-FullPath $PackageRoot
$DestinationRoot = Join-Path $PackageRoot 'app'

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source directory does not exist: $SourceRoot"
}
if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) {
    throw "Package directory does not exist: $PackageRoot"
}
if ([string]::Equals($SourceRoot.TrimEnd('\'), $DestinationRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Source and destination are the same. Stopped to protect the source tree.'
}
if (-not (Test-Path -LiteralPath $DestinationRoot -PathType Container)) {
    if ($Preview) {
        Write-Host "[preview] would create destination: $DestinationRoot"
    } else {
        New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    }
}

$copyDirectories = @(
    '.github',
    'assets',
    'backend',
    'common',
    'deploy',
    'dialogue_packs',
    'docs',
    'frontend',
    'scheduler',
    'tools',
    'websocket'
)

$copyRootFiles = @(
    '.dockerignore',
    '.env.example',
    '.gitignore',
    'BUILD_ID.txt',
    'docker-compose.yml',
    'install-xianyu.bat',
    'README.md',
    'report-source.md',
    'start-xianyu.bat',
    'stop-xianyu.bat',
    'VERSION.txt'
)

function Invoke-Robocopy([string]$Source, [string]$Destination, [switch]$ListOnly) {
    $arguments = @(
        $Source,
        $Destination,
        '/E',
        '/COPY:DAT',
        '/DCOPY:DAT',
        # 客户端目录可能经历过压缩包解压，时间戳精度与源工作区不同。
        # /IS 和 /IT 确保内容相同或时间戳异常的文件也被覆盖，避免显示
        # “已同步”但容器实际仍挂载旧代码。
        '/IS',
        '/IT',
        '/R:2',
        '/W:1',
        '/XJ',
        '/NFL',
        '/NDL',
        '/XF', '.env', '*.log', '*.pyc',
        '/XD', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules'
    )
    if ($ListOnly) { $arguments += '/L' }
    & robocopy.exe @arguments
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        throw "Directory sync failed. Robocopy exit code: $code. Source=$Source Destination=$Destination"
    }
}

Write-Host "Source: $SourceRoot"
Write-Host "Destination: $DestinationRoot"
if ($Preview) { Write-Host 'Mode: preview; no files will be changed.' -ForegroundColor Yellow }

foreach ($relative in $copyDirectories) {
    $source = Join-Path $SourceRoot $relative
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Write-Host "[skip] source directory does not exist: $relative" -ForegroundColor DarkYellow
        continue
    }
    $destination = Join-Path $DestinationRoot $relative
    $message = if ($Preview) { "[preview] directory: $relative" } else { "[sync] directory: $relative" }
    Write-Host $message
    Invoke-Robocopy $source $destination -ListOnly:$Preview
}

foreach ($relative in $copyRootFiles) {
    $source = Join-Path $SourceRoot $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
    $destination = Join-Path $DestinationRoot $relative
    if ($Preview) {
        Write-Host "[preview] file: $relative"
    } else {
        Copy-Item -LiteralPath $source -Destination $destination -Force
        Write-Host "[done] file: $relative"
    }
}

Write-Host ''
if ($Preview) {
    Write-Host 'Preview complete. The target .env, logs, updates, static, backups and browser_data are untouched.' -ForegroundColor Green
} else {
    Write-Host 'Sync complete. Latest source is in the target app directory. Runtime .env, logs, updates, static, backups and browser_data are preserved.' -ForegroundColor Green
}
