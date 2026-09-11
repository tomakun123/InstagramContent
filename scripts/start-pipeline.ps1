<#
.SYNOPSIS
    Starts the whole content pipeline with one command.

.DESCRIPTION
    Brings up the five services the pipeline depends on, in dependency order,
    waiting for each to report healthy before starting the next:

        1. LM Studio    (:1234)  - serves the local model n8n prompts
        2. n8n          (:5678)  - orchestration
        3. videoServer  (:8090)  - serves HorrorVideos\ for Instagram to fetch
        4. cloudflared           - public tunnel to n8n and the video server
        5. storyWatcher.py       - renders a video when n8n drops metadata

    Safe to run twice: anything already running is left alone.

.PARAMETER Install
    Register a Scheduled Task that runs this script at logon, so the pipeline
    comes back automatically after a reboot.

.PARAMETER SkipTunnel
    Start everything except cloudflared (useful when working locally).

.PARAMETER Follow
    After startup, stay attached and stream the live view (watch-pipeline.ps1)
    instead of returning to the prompt. Ctrl+C leaves the view; the pipeline
    keeps running.

.PARAMETER TimeoutSeconds
    How long to wait for each health check before giving up. Default 90.

.EXAMPLE
    .\scripts\start-pipeline.ps1
    .\scripts\start-pipeline.ps1 -Install
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$SkipTunnel,
    [switch]$Follow,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'

$Root      = Split-Path -Parent $PSScriptRoot
$LogDir    = Join-Path $Root 'logs'
$PidFile   = Join-Path $LogDir 'pids.json'
$Stamp     = Get-Date -Format 'yyyy-MM-dd'
$Started   = @{}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ---------------------------------------------------------------- helpers ----

function Write-Step   { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok     { param($m) Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Skip   { param($m) Write-Host "    SKIP $m" -ForegroundColor DarkGray }
function Write-Fail   { param($m) Write-Host "    FAIL $m" -ForegroundColor Red }

function Import-DotEnv {
    <# Load KEY=VALUE pairs from .env into this process, so children inherit them.
       Replaces the need to launch n8n through `npx dotenv-cli`. #>
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        Write-Skip ".env not found at $Path"
        return 0
    }

    $count = 0
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }

        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }

        $key = $trimmed.Substring(0, $idx).Trim()
        $val = $trimmed.Substring($idx + 1).Trim()

        # strip matching surrounding quotes
        if ($val.Length -ge 2) {
            if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
                ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                $val = $val.Substring(1, $val.Length - 2)
            }
        }

        Set-Item -Path "Env:$key" -Value $val
        $count++
    }
    return $count
}

function Test-PortListening {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return ($null -ne $conn)
    } catch {
        return $false
    }
}

function Test-ProcessRunning {
    param([string]$Name)
    try   { return $null -ne (Get-Process -Name $Name -ErrorAction Stop) }
    catch { return $false }
}

function Wait-Healthy {
    <# Poll a URL until it answers, or the timeout expires. #>
    param(
        [string]$Url,
        [int]$Timeout,
        [string]$Label
    )
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { return $true }
        } catch {
            # not up yet
        }
        Start-Sleep -Seconds 2
    }
    Write-Fail "$Label did not become healthy within ${Timeout}s ($Url)"
    return $false
}

function ConvertTo-QuotedArgs {
    <# Quote any argument containing whitespace.

       Start-Process -ArgumentList joins the array with spaces and does no
       quoting of its own, so an unquoted path with a space is silently split
       into two arguments. That is not hypothetical here: this repo lives under
       "C:\Users\Thomas M\...", so passing the watcher's path handed Python
       "C:\Users\Thomas" as the script name - and a stray VC_redist install log
       happens to sit at exactly that path, so Python parsed *that* and raised a
       SyntaxError instead of failing cleanly.

       Done centrally rather than per call site so future callers inherit it. #>
    param([string[]]$Arguments)

    if (-not $Arguments) { return @() }

    return @($Arguments | ForEach-Object {
        if ($_ -match '\s' -and $_ -notmatch '^".*"$') { '"' + $_ + '"' } else { $_ }
    })
}

function Get-PipelinePython {
    <# The interpreter for pipeline\*.py: the repo .venv if present, else PATH. #>
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) { return $venvPython }
    Write-Host '    NOTE .venv not found, falling back to system Python' -ForegroundColor Yellow
    $python = Resolve-Command 'python'
    if ($null -eq $python) { Abort 'No Python interpreter found.' }
    return $python
}

function Start-Logged {
    <# Launch a background process with stdout/stderr captured into logs/. #>
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory = $Root
    )
    $out = Join-Path $LogDir "$Name-$Stamp.out.log"
    $err = Join-Path $LogDir "$Name-$Stamp.err.log"

    $p = Start-Process -FilePath $FilePath `
                       -ArgumentList (ConvertTo-QuotedArgs $ArgumentList) `
                       -WorkingDirectory $WorkingDirectory `
                       -RedirectStandardOutput $out `
                       -RedirectStandardError $err `
                       -WindowStyle Hidden `
                       -PassThru
    return $p
}

function Resolve-Command {
    <# Find a *launchable* path for a command.

       Get-Command prefers .ps1 shims (npm installs n8n.ps1, n8n.cmd and n8n
       side by side), but Start-Process cannot execute a .ps1 - it hands it to
       the shell's default handler instead. So prefer real executables. #>
    param([string]$Name)

    $candidates = @(Get-Command $Name -All -ErrorAction SilentlyContinue |
                    Where-Object { $_.CommandType -eq 'Application' })
    if ($candidates.Count -eq 0) { return $null }

    foreach ($ext in @('.exe', '.cmd', '.bat', '.com')) {
        $hit = $candidates | Where-Object {
            [System.IO.Path]::GetExtension($_.Source).ToLower() -eq $ext
        } | Select-Object -First 1
        if ($hit) { return $hit.Source }
    }

    # Nothing directly launchable - fall back to the first match and let the
    # caller fail loudly rather than silently opening it in an editor.
    return $candidates[0].Source
}

function Save-Pids {
    <# Persist what we started, merging with any pids from a previous partial
       run so stop-pipeline can still find them. #>
    $allPids = @{}
    if (Test-Path $PidFile) {
        try {
            (Get-Content $PidFile -Raw | ConvertFrom-Json).PSObject.Properties |
                ForEach-Object { $allPids[$_.Name] = $_.Value }
        } catch { }
    }
    foreach ($k in $Started.Keys) { $allPids[$k] = $Started[$k] }

    if ($allPids.Count -gt 0) {
        $allPids | ConvertTo-Json | Set-Content -Path $PidFile -Encoding utf8
    }
}

function Abort {
    param([string]$Message)
    # Save first: a partial startup is exactly when stop-pipeline needs the pid
    # list, and the message below tells the user to go and run it.
    Save-Pids

    Write-Host ''
    Write-Fail $Message
    Write-Host "Logs: $LogDir" -ForegroundColor Yellow
    Write-Host 'Downstream services were not started. Run stop-pipeline.ps1 to clean up.' -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------- install ----

if ($Install) {
    Write-Step 'Registering logon Scheduled Task "HorrorPipeline"'
    $self   = Join-Path $PSScriptRoot 'start-pipeline.ps1'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$self`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName 'HorrorPipeline' `
                           -Action $action -Trigger $trigger -Settings $settings `
                           -Description 'Starts LM Studio, n8n, cloudflared and the story watcher.' `
                           -Force | Out-Null
    Write-Ok 'Task registered. The pipeline will now start at logon.'
    Write-Host 'Remove it with: Unregister-ScheduledTask -TaskName HorrorPipeline' -ForegroundColor DarkGray
    exit 0
}

# ------------------------------------------------------------------ start ----

Write-Host ''
Write-Host 'Horror pipeline launcher' -ForegroundColor White
Write-Host "Root: $Root" -ForegroundColor DarkGray
Write-Host ''

Write-Step 'Loading .env'
$loaded = Import-DotEnv -Path (Join-Path $Root '.env')
Write-Ok "$loaded variable(s) loaded into environment"

$Model      = if ($env:LMS_MODEL)            { $env:LMS_MODEL }            else { 'qwen2.5-14b-instruct' }
$TunnelName = if ($env:CLOUDFLARED_TUNNEL)   { $env:CLOUDFLARED_TUNNEL }   else { $null }

# -- 1. LM Studio ------------------------------------------------------------
Write-Step "LM Studio (:1234, model $Model)"
if (Test-PortListening -Port 1234) {
    Write-Skip 'already listening on :1234'
} else {
    $lms = Resolve-Command 'lms'
    if ($null -eq $lms) {
        Abort 'LM Studio CLI (lms) not found on PATH. Start LM Studio and load the model manually, or install the CLI, then re-run.'
    }

    $p = Start-Logged -Name 'lmstudio' -FilePath $lms -ArgumentList @('server', 'start')
    $Started['lmstudio'] = $p.Id

    if (-not (Wait-Healthy -Url 'http://127.0.0.1:1234/v1/models' -Timeout $TimeoutSeconds -Label 'LM Studio')) {
        Abort 'LM Studio failed to start.'
    }

    # Load the model so the very first n8n prompt does not time out waiting for it.
    & $lms load $Model 2>&1 | Out-File -FilePath (Join-Path $LogDir "lmstudio-$Stamp.out.log") -Append -Encoding utf8
    Write-Ok "server up, model '$Model' requested"
}
if (Test-PortListening -Port 1234) { Write-Ok 'healthy on :1234' }

# -- 2. n8n ------------------------------------------------------------------
Write-Step 'n8n (:5678)'
if (Test-PortListening -Port 5678) {
    Write-Skip 'already listening on :5678'
} else {
    $n8n = Resolve-Command 'n8n'
    if ($null -eq $n8n) {
        Abort 'n8n not found on PATH. Install it globally with:  npm i -g n8n'
    }

    $p = Start-Logged -Name 'n8n' -FilePath $n8n -ArgumentList @('start')
    $Started['n8n'] = $p.Id

    if (-not (Wait-Healthy -Url 'http://127.0.0.1:5678/healthz' -Timeout $TimeoutSeconds -Label 'n8n')) {
        Abort 'n8n failed to start. Check logs\n8n-*.err.log'
    }
    Write-Ok 'healthy on :5678'
}

# -- 3. video server ---------------------------------------------------------
# Instagram fetches the finished mp4 from a public URL, so the videos directory
# has to be reachable through the tunnel. Skipped when the secret is unset so a
# YouTube-only install keeps working unchanged.
Write-Step 'videoServer.py (:8090)'
if (-not $env:VIDEO_URL_SECRET) {
    Write-Skip 'VIDEO_URL_SECRET not set in .env (Instagram publishing disabled)'
} elseif (Test-PortListening -Port 8090) {
    Write-Skip 'already listening on :8090'
} else {
    $python = Get-PipelinePython
    $server = Join-Path $Root 'pipeline\videoServer.py'
    $p = Start-Logged -Name 'videoserver' -FilePath $python -ArgumentList @('-u', $server)
    $Started['videoserver'] = $p.Id

    if (-not (Wait-Healthy -Url 'http://127.0.0.1:8090/healthz' -Timeout $TimeoutSeconds -Label 'videoServer')) {
        Abort 'videoServer.py failed to start. Check logs\videoserver-*.err.log'
    }
    Write-Ok "healthy on :8090 (pid $($p.Id))"
}

# -- 4. cloudflared ----------------------------------------------------------
if ($SkipTunnel) {
    Write-Step 'cloudflared'
    Write-Skip '-SkipTunnel specified'
} else {
    Write-Step 'cloudflared tunnel'
    if (Test-ProcessRunning -Name 'cloudflared') {
        Write-Skip 'already running'
    } else {
        $cf = Resolve-Command 'cloudflared'
        if ($null -eq $cf) {
            Abort 'cloudflared not found on PATH.'
        }

        # ~/.cloudflared/config.yml normally names the tunnel already, in which
        # case `cloudflared tunnel run` needs no argument. Only insist on an
        # explicit name when there is no config to fall back on.
        $cfConfig = Join-Path $env:USERPROFILE '.cloudflared\config.yml'
        if ($TunnelName) {
            $cfArgs = @('tunnel', 'run', $TunnelName)
            $label  = "tunnel '$TunnelName'"
        } elseif (Test-Path $cfConfig) {
            $cfArgs = @('tunnel', 'run')
            $label  = 'tunnel (from ~/.cloudflared/config.yml)'
        } else {
            Abort 'No tunnel configured: no CLOUDFLARED_TUNNEL in .env and no ~/.cloudflared/config.yml. Pass -SkipTunnel to run without it.'
        }

        $p = Start-Logged -Name 'cloudflared' -FilePath $cf -ArgumentList $cfArgs
        $Started['cloudflared'] = $p.Id

        Start-Sleep -Seconds 5
        if ($p.HasExited) {
            Abort "cloudflared exited immediately (code $($p.ExitCode)). Check logs\cloudflared-$Stamp.err.log"
        }
        Write-Ok "$label running (pid $($p.Id))"
    }
}

# -- 5. story watcher --------------------------------------------------------
Write-Step 'storyWatcher.py'

$existing = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*storyWatcher.py*' }

if ($existing) {
    Write-Skip "already running (pid $($existing.ProcessId))"
} else {
    $python = Get-PipelinePython

    $watcher = Join-Path $Root 'pipeline\storyWatcher.py'
    $p = Start-Logged -Name 'watcher' -FilePath $python -ArgumentList @('-u', $watcher)
    $Started['watcher'] = $p.Id

    Start-Sleep -Seconds 3
    if ($p.HasExited) {
        Abort "storyWatcher.py exited immediately (code $($p.ExitCode)). Check logs\watcher-$Stamp.err.log"
    }
    Write-Ok "watching Metadata\ (pid $($p.Id))"
}

# ---------------------------------------------------------------- finish ----

Save-Pids

Write-Host ''
Write-Host 'Pipeline is up.' -ForegroundColor Green
Write-Host "  n8n     http://localhost:5678"
if ($env:N8N_EDITOR_BASE_URL) { Write-Host "  public  $($env:N8N_EDITOR_BASE_URL)" }
Write-Host "  logs    $LogDir"
Write-Host "  watch   .\scripts\watch-pipeline.ps1"
Write-Host "  stop    .\scripts\stop-pipeline.ps1"
Write-Host ''

if ($Follow) {
    # Hand over to the live view. Everything above runs hidden with its output
    # redirected to logs\, so without this the terminal goes quiet the moment
    # startup finishes. Ctrl+C leaves the view without stopping anything.
    & (Join-Path $PSScriptRoot 'watch-pipeline.ps1')
}
