$script:XianyuTranscriptActive = $false
$script:XianyuTranscriptPath = ''

function Start-XianyuLogSession {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $logDir = Join-Path $ProjectRoot 'logs'
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $path = Join-Path $logDir "$Name.log"
    $header = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') session_start name=$Name computer=$env:COMPUTERNAME user=$env:USERNAME powershell=$($PSVersionTable.PSVersion)"
    try {
        Add-Content -LiteralPath $path -Value $header -Encoding UTF8 -ErrorAction Stop
    } catch {
        # A second launcher may still have Start-Transcript holding the shared
        # file. Fall back to a process-specific log instead of failing setup.
        $path = Join-Path $logDir ("{0}-{1}-{2}.log" -f $Name, $PID, (Get-Date -Format 'yyyyMMddHHmmssfff'))
        try { Add-Content -LiteralPath $path -Value $header -Encoding UTF8 -ErrorAction Stop } catch { return $path }
    }
    try {
        Start-Transcript -LiteralPath $path -Append -Force | Out-Null
        $script:XianyuTranscriptActive = $true
        $script:XianyuTranscriptPath = $path
    } catch {
        try { Add-Content -LiteralPath $path -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') transcript_unavailable error=$($_.Exception.Message)" -Encoding UTF8 } catch { }
    }
    return $path
}

function Write-XianyuLog {
    param([string]$LogPath, [string]$Message)
    if ([string]::IsNullOrWhiteSpace($LogPath)) { return }
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') $Message"
    if ($script:XianyuTranscriptActive -and $script:XianyuTranscriptPath -eq $LogPath) {
        Write-Host $line
        return
    }
    try {
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch { }
}

function Stop-XianyuLogSession {
    if ($script:XianyuTranscriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:XianyuTranscriptActive = $false
        $script:XianyuTranscriptPath = ''
    }
}

function Show-XianyuErrorDialog {
    param(
        [string]$Title = 'Xianyu error',
        [string]$Summary = 'The operation failed.',
        [string]$Detail = '',
        [string]$LogPath = ''
    )

    $content = "$Summary`r`n`r`n$Detail"
    if ($LogPath) { $content += "`r`n`r`nLog file:`r`n$LogPath" }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $form = New-Object System.Windows.Forms.Form
        $form.Text = $Title
        $form.StartPosition = 'CenterScreen'
        $form.ClientSize = New-Object System.Drawing.Size(760, 520)
        $form.TopMost = $true
        $form.MinimizeBox = $false
        $form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)

        $label = New-Object System.Windows.Forms.Label
        $label.Text = $Summary
        $label.Location = New-Object System.Drawing.Point(18, 16)
        $label.Size = New-Object System.Drawing.Size(724, 44)
        $label.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11, [System.Drawing.FontStyle]::Bold)
        $form.Controls.Add($label)

        $box = New-Object System.Windows.Forms.TextBox
        $box.Location = New-Object System.Drawing.Point(18, 68)
        $box.Size = New-Object System.Drawing.Size(724, 382)
        $box.Multiline = $true
        $box.ScrollBars = 'Both'
        $box.ReadOnly = $true
        $box.WordWrap = $false
        $box.Font = New-Object System.Drawing.Font('Consolas', 9)
        $box.Text = $content
        $form.Controls.Add($box)

        $copy = New-Object System.Windows.Forms.Button
        $copy.Text = [string]::Concat([char]0x590d, [char]0x5236, [char]0x9519, [char]0x8bef)
        $copy.Location = New-Object System.Drawing.Point(406, 466)
        $copy.Size = New-Object System.Drawing.Size(100, 34)
        $copy.Add_Click({ [System.Windows.Forms.Clipboard]::SetText($content) })
        $form.Controls.Add($copy)

        $open = New-Object System.Windows.Forms.Button
        $open.Text = [string]::Concat([char]0x6253, [char]0x5f00, [char]0x65e5, [char]0x5fd7, [char]0x76ee, [char]0x5f55)
        $open.Location = New-Object System.Drawing.Point(516, 466)
        $open.Size = New-Object System.Drawing.Size(112, 34)
        $open.Enabled = [bool]$LogPath
        $open.Add_Click({
            if ($LogPath) { Start-Process explorer.exe -ArgumentList "/select,`"$LogPath`"" }
        })
        $form.Controls.Add($open)

        $close = New-Object System.Windows.Forms.Button
        $close.Text = [string]::Concat([char]0x5173, [char]0x95ed)
        $close.Location = New-Object System.Drawing.Point(638, 466)
        $close.Size = New-Object System.Drawing.Size(104, 34)
        $close.Add_Click({ $form.Close() })
        $form.Controls.Add($close)
        $form.AcceptButton = $close

        [void]$form.ShowDialog()
    } catch {
        Write-Host $content -ForegroundColor Red
        # The GUI launcher intentionally starts PowerShell without a console.
        # Do not leave that hidden process waiting for input if WinForms is
        # unavailable; the caller will present the captured error instead.
        if ($env:XIANYU_NONINTERACTIVE -ne '1') {
            try { Read-Host 'Press Enter to close' | Out-Null } catch { }
        }
    }
}

function Complete-XianyuFailure {
    param(
        [string]$Context,
        [System.Management.Automation.ErrorRecord]$ErrorRecord,
        [string]$LogPath
    )
    $detail = if ($ErrorRecord) { $ErrorRecord | Format-List * -Force | Out-String } else { 'Unknown error' }
    Write-XianyuLog -LogPath $LogPath -Message "fatal_error context=$Context detail=$($detail -replace '[\r\n]+', ' ')"
    Stop-XianyuLogSession
    Show-XianyuErrorDialog -Title 'Xianyu Management System Error' -Summary $Context -Detail $detail.Trim() -LogPath $LogPath
}
