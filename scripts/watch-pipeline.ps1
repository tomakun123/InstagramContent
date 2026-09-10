<#
.SYNOPSIS
    Live view of the running pipeline. Read-only and safe to start or stop at will.

.DESCRIPTION
    Streams everything the pipeline is doing into one terminal:

      * new lines from logs\*.log, prefixed by which service wrote them
      * n8n workflow executions as they finish
      * new files landing in Metadata\ and HorrorVideos\
      * a periodic status line: services, counter, lock

    Ctrl+C leaves the view. It does NOT stop the pipeline - use stop-pipeline.ps1
    for that.

.PARAMETER IntervalSeconds
    How often to poll. Default 3.

.PARAMETER StatusEverySeconds
    How often to reprint the status line even when nothing changed. Default 120.

.PARAMETER Backfill
    Show this many existing log lines per file on startup, so you get context
    rather than an empty screen. Default 5.

.EXAMPLE
    .\scripts\watch-pipeline.ps1
#>
[CmdletBinding()]
param(
    [int]$IntervalSeconds = 3,
    [int]$StatusEverySeconds = 120,
    [int]$Backfill = 5
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$LogDir  = Join-Path $Root 'logs'
$Lock    = Join-Path $Root 'HorrorStories\generate_lock.lock'
$Counter = Join-Path $Root 'HorrorStories\counter.txt'
$Helper  = Join-Path $PSScriptRoot 'n8n_recent.py'

# name -> byte offset already consumed
$Offsets   = @{}
$LastExec  = 0
$LastStatus = [DateTime]::MinValue
$SeenFiles = @{}

function Get-ServiceColor {
    param([string]$Name)
    switch -Wildcard ($Name) {
        'watcher*'     { 'Green' }
        'n8n*'         { 'Cyan' }
        'cloudflared*' { 'DarkCyan' }
        'lmstudio*'    { 'Magenta' }
        default        { 'Gray' }
    }
}

function Read-NewText {
    <# Read whatever has been appended since we last looked.

       Opens with FileShare ReadWrite|Delete so a running service is never
       blocked from writing to (or rotating) its own log while we tail it. #>
    param([string]$Path, [long]$From)

    try {
        $fs = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
    } catch {
        return $null
    }

    try {
        if ($fs.Length -lt $From) { $From = 0 }   # file was truncated or rotated
        if ($fs.Length -eq $From) { return @{ Text = ''; Offset = $From } }

        [void]$fs.Seek($From, [System.IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] ($fs.Length - $From)
        $read = $fs.Read($buf, 0, $buf.Length)
        $text = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
        return @{ Text = $text; Offset = $From + $read }
    } finally {
        $fs.Dispose()
    }
}

function Show-LogLines {
    if (-not (Test-Path $LogDir)) { return }

    foreach ($file in Get-ChildItem -Path $LogDir -Filter '*.log' -File) {
        # Service name is the part before the -YYYY-MM-DD stamp.
        $label = $file.BaseName -replace '-\d{4}-\d{2}-\d{2}.*$', ''
        $isErr = $file.Name -like '*.err.log'

        if (-not $Offsets.ContainsKey($file.FullName)) {
            # First sight of this file: start near the end so we do not replay
            # the whole day, but show a little context.
            $start = 0
            if ($file.Length -gt 0) {
                $tail = Get-Content -Path $file.FullName -Tail $Backfill -ErrorAction SilentlyContinue
                if ($tail) {
                    foreach ($line in $tail) {
                        if ($line.Trim() -ne '') {
                            Write-Host ("  {0,-12} {1}" -f $label, $line) -ForegroundColor DarkGray
                        }
                    }
                }
                $start = $file.Length
            }
            $Offsets[$file.FullName] = $start
            continue
        }

        $result = Read-NewText -Path $file.FullName -From $Offsets[$file.FullName]
        if ($null -eq $result) { continue }
        $Offsets[$file.FullName] = $result.Offset
        if ($result.Text -eq '') { continue }

        $color = Get-ServiceColor $label
        if ($isErr) { $color = 'Red' }

        foreach ($line in ($result.Text -split "`r?`n")) {
            if ($line.Trim() -eq '') { continue }
            $stamp = (Get-Date).ToString('HH:mm:ss')
            Write-Host ("{0}  {1,-12} {2}" -f $stamp, $label, $line) -ForegroundColor $color
        }
    }
}

function Show-Executions {
    if (-not (Test-Path $Helper)) { return }

    try {
        $lines = & python $Helper --since $script:LastExec --limit 25 2>$null
    } catch {
        return
    }
    if (-not $lines) { return }

    foreach ($line in $lines) {
        $parts = $line -split "`t"
        if ($parts.Count -lt 4) { continue }

        $script:LastExec = [int]$parts[0]
        $status = $parts[2]
        $color  = 'White'
        if ($status -eq 'success') { $color = 'Green' }
        if ($status -eq 'error')   { $color = 'Red' }
        if ($status -eq 'running') { $color = 'Yellow' }

        Write-Host ("{0}  {1,-12} {2} {3}" -f $parts[1], 'n8n-exec', $status.PadRight(8), $parts[3]) -ForegroundColor $color
    }
}

function Show-NewArtifacts {
    foreach ($spec in @(
        @{ Dir = 'Metadata';     Filter = '*.json'; Label = 'metadata' },
        @{ Dir = 'HorrorVideos'; Filter = '*.mp4';  Label = 'video' }
    )) {
        $dir = Join-Path $Root $spec.Dir
        if (-not (Test-Path $dir)) { continue }

        foreach ($f in Get-ChildItem -Path $dir -Filter $spec.Filter -File -ErrorAction SilentlyContinue) {
            $key = $f.FullName
            $sig = "$($f.Length):$($f.LastWriteTimeUtc.Ticks)"

            if (-not $SeenFiles.ContainsKey($key)) {
                $SeenFiles[$key] = $sig
                continue    # pre-existing, not news
            }
            if ($SeenFiles[$key] -eq $sig) { continue }

            $SeenFiles[$key] = $sig
            $mb = [math]::Round($f.Length / 1MB, 1)
            Write-Host ("{0}  {1,-12} {2} ({3} MB)" -f (Get-Date).ToString('HH:mm:ss'), $spec.Label, $f.Name, $mb) -ForegroundColor Yellow
        }
    }
}

function Show-Status {
    $svc = @()

    if (Get-NetTCPConnection -LocalPort 1234 -State Listen -ErrorAction SilentlyContinue) {
        $svc += 'lmstudio'
    } else { $svc += 'lmstudio:DOWN' }

    if (Get-NetTCPConnection -LocalPort 5678 -State Listen -ErrorAction SilentlyContinue) {
        $svc += 'n8n'
    } else { $svc += 'n8n:DOWN' }

    if (Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue) {
        $svc += 'tunnel'
    } else { $svc += 'tunnel:DOWN' }

    $watcher = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -like '*storyWatcher.py*' }
    if ($watcher) { $svc += 'watcher' } else { $svc += 'watcher:DOWN' }

    $counterValue = '?'
    if (Test-Path $Counter) { $counterValue = (Get-Content $Counter -Raw).Trim() }

    $lockState = 'free'
    if (Test-Path $Lock) { $lockState = 'HELD' }

    $line = "[{0}] {1} | counter {2} | lock {3}" -f `
            (Get-Date).ToString('HH:mm:ss'), ($svc -join ' '), $counterValue, $lockState

    $color = 'DarkGray'
    if ($line -match 'DOWN') { $color = 'Yellow' }
    Write-Host $line -ForegroundColor $color

    $script:LastStatus = Get-Date
}

# ------------------------------------------------------------------ main ----

Write-Host ''
Write-Host 'Pipeline live view' -ForegroundColor White
Write-Host "Root: $Root" -ForegroundColor DarkGray
Write-Host 'Ctrl+C exits this view. The pipeline keeps running.' -ForegroundColor DarkGray
Write-Host ''

# Prime the caches so the first pass reports only genuinely new activity.
Show-LogLines
try {
    $seed = & python $Helper --since 0 --limit 1000 2>$null
    if ($seed) {
        $last = $seed[-1] -split "`t"
        $LastExec = [int]$last[0]
    }
} catch { }
Show-NewArtifacts
Show-Status
Write-Host ''

while ($true) {
    Show-LogLines
    Show-Executions
    Show-NewArtifacts

    if (((Get-Date) - $LastStatus).TotalSeconds -ge $StatusEverySeconds) {
        Show-Status
    }

    Start-Sleep -Seconds $IntervalSeconds
}
