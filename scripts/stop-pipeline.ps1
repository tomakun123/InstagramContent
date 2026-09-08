<#
.SYNOPSIS
    Stops everything start-pipeline.ps1 started.

.DESCRIPTION
    Reads logs/pids.json and stops each service in reverse dependency order
    (watcher -> cloudflared -> n8n -> LM Studio), then clears a stale
    generate_lock.lock if one was left behind by an interrupted run.

.PARAMETER KeepLock
    Leave generate_lock.lock in place (use if a render is deliberately still running).
#>
[CmdletBinding()]
param(
    [switch]$KeepLock
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$LogDir  = Join-Path $Root 'logs'
$PidFile = Join-Path $LogDir 'pids.json'
$Lock    = Join-Path $Root 'HorrorStories\generate_lock.lock'

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Skip { param($m) Write-Host "    SKIP $m" -ForegroundColor DarkGray }

# Reverse dependency order: stop consumers before the things they depend on.
$order = @('watcher', 'cloudflared', 'n8n', 'lmstudio')

$pids = @{}
if (Test-Path $PidFile) {
    try {
        (Get-Content $PidFile -Raw | ConvertFrom-Json).PSObject.Properties |
            ForEach-Object { $pids[$_.Name] = [int]$_.Value }
    } catch {
        Write-Host "Could not parse $PidFile - falling back to name matching." -ForegroundColor Yellow
    }
} else {
    Write-Host "No pid file at $PidFile - falling back to name matching." -ForegroundColor Yellow
}

Write-Host ''
foreach ($name in $order) {
    Write-Step $name

    $stopped = $false

    if ($pids.ContainsKey($name)) {
        $target = Get-Process -Id $pids[$name] -ErrorAction SilentlyContinue
        if ($target) {
            Stop-Process -Id $pids[$name] -Force -ErrorAction SilentlyContinue
            Write-Ok "stopped pid $($pids[$name])"
            $stopped = $true
        }
    }

    if (-not $stopped) {
        # Fall back to finding it by what it is, for processes started outside this script.
        switch ($name) {
            'watcher' {
                $procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
                         Where-Object { $_.CommandLine -like '*storyWatcher.py*' }
                foreach ($p in $procs) {
                    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                    Write-Ok "stopped pid $($p.ProcessId) (matched by command line)"
                    $stopped = $true
                }
            }
            'cloudflared' {
                $procs = Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue
                foreach ($p in $procs) {
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                    Write-Ok "stopped pid $($p.Id) (matched by name)"
                    $stopped = $true
                }
            }
        }
    }

    if (-not $stopped) { Write-Skip 'not running' }
}

if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force
    Write-Host ''
    Write-Ok 'cleared pids.json'
}

Write-Step 'generate_lock.lock'
if (Test-Path $Lock) {
    if ($KeepLock) {
        Write-Skip '-KeepLock specified, leaving in place'
    } else {
        Remove-Item $Lock -Force -ErrorAction SilentlyContinue
        Write-Ok 'removed stale lock'
    }
} else {
    Write-Skip 'no lock present'
}

# Report anything still holding the ports, so a follow-up start is not surprised.
Write-Host ''
foreach ($port in @(1234, 5678)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Write-Host "WARNING: port $port is still listening (pid $($conn[0].OwningProcess))" -ForegroundColor Yellow
    }
}

Write-Host 'Pipeline stopped.' -ForegroundColor Green
Write-Host ''
