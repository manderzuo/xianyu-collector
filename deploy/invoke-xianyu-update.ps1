param(
    [string]$RequestUri = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Checker = Join-Path $PSScriptRoot 'check-xianyu-update.ps1'
$LogDir = Join-Path $ProjectRoot 'logs'
$LaunchLog = Join-Path $LogDir 'updater-launch.log'
$ErrorHelper = Join-Path $PSScriptRoot 'windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }

Add-Type -AssemblyName System.Windows.Forms

function Write-LaunchLog([string]$Message) {
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Add-Content -LiteralPath $LaunchLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') $Message" -Encoding UTF8
}

# The URI is only a wake-up signal. Never execute or interpolate content from it.
if ($RequestUri -and -not $RequestUri.StartsWith('xianyu-update://', [StringComparison]::OrdinalIgnoreCase)) {
    Write-LaunchLog "invalid_uri value=$RequestUri"
    exit 2
}
if (-not (Test-Path -LiteralPath $Checker)) {
    Write-LaunchLog "checker_missing path=$Checker"
    if (Get-Command Show-XianyuErrorDialog -ErrorAction SilentlyContinue) {
        Show-XianyuErrorDialog -Title 'Xianyu updater error' -Summary 'The updater file is missing. Copy the complete installer package again.' -Detail "Missing file: $Checker" -LogPath $LaunchLog
    } else {
        [System.Windows.Forms.MessageBox]::Show('更新程序文件缺失，请重新复制完整安装包。', '闲鱼管理系统更新') | Out-Null
    }
    exit 1
}

try {
    Write-LaunchLog "launch_start root=$ProjectRoot"
    & $Checker -Force
    $code = $LASTEXITCODE
    Write-LaunchLog "launch_end exit_code=$code"
    exit $code
} catch {
    $detail = $_.Exception.ToString()
    Write-LaunchLog "launch_failed error=$detail"
    if (Get-Command Show-XianyuErrorDialog -ErrorAction SilentlyContinue) {
        Show-XianyuErrorDialog -Title 'Xianyu updater error' -Summary 'The updater could not be started.' -Detail $detail -LogPath $LaunchLog
    } else {
        [System.Windows.Forms.MessageBox]::Show("更新程序启动失败。`r`n`r`n$($_.Exception.Message)`r`n`r`n日志：$LaunchLog", '闲鱼管理系统更新失败', 'OK', 'Error') | Out-Null
    }
    exit 1
}
