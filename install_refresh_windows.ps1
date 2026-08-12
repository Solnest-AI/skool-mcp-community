#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the scheduled task that refreshes your Skool cookie every 2 days (Windows).

.DESCRIPTION
    Registers a Task Scheduler job that runs `uv run refresh_cookie.py` from this
    folder, at logon and every 2 days thereafter. Run it from wherever you put this
    folder — the script finds itself, so there is no path to edit.

    Windows has no equivalent of the macOS Full Disk Access ordeal, so there is no
    protected-folder branch here. Task Scheduler does not capture stdout/stderr, so
    refresh_cookie.py writes its own log to %LOCALAPPDATA%\skool-mcp\refresh.log —
    that file is the place to look when something goes wrong.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_refresh_windows.ps1
#>
[CmdletBinding()]
param(
    # Task Scheduler name. doctor.py looks for this exact string.
    [string]$TaskName = 'SkoolMCP-CookieRefresh',

    # Time of day for the every-2-days run.
    [string]$AtTime = '09:00'
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# ── Locate this folder ───────────────────────────────────────────────────────
$RepoDir = $PSScriptRoot
if (-not $RepoDir) {
    Fail "Could not determine the script directory. Run this as a file:`n  powershell -ExecutionPolicy Bypass -File .\install_refresh_windows.ps1"
}

$ScriptPath = Join-Path $RepoDir 'refresh_cookie.py'
if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Fail "refresh_cookie.py not found next to this script (looked in $RepoDir)."
}

# ── Find uv ──────────────────────────────────────────────────────────────────
$UvPath = $null
$uvCmd = Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue
if ($uvCmd) {
    $UvPath = @($uvCmd)[0].Source
} else {
    # PATH may be stale in this session if uv was just installed — check the
    # standard install locations before giving up.
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { $UvPath = $c; break }
    }
}

if (-not $UvPath) {
    Fail @"
uv is not installed, or is not on this session's PATH.

Install it, then run this script again:
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

If you JUST installed uv, close this window and open a new PowerShell first —
PATH is only refreshed for new sessions.
"@
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir 'pyproject.toml'))) {
    Write-Warning "No pyproject.toml in $RepoDir — 'uv run' may not resolve dependencies."
}

# ── Log location (refresh_cookie.py writes here too — same path, by design) ──
$LogDir  = Join-Path $env:LOCALAPPDATA 'skool-mcp'
$LogFile = Join-Path $LogDir 'refresh.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Register the task ────────────────────────────────────────────────────────
# WorkingDirectory is this folder so `uv run` resolves the project and the script.
$action = New-ScheduledTaskAction -Execute $UvPath `
                                  -Argument 'run refresh_cookie.py' `
                                  -WorkingDirectory $RepoDir

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Daily -DaysInterval 2 -At $AtTime)
)

# StartWhenAvailable catches up a run the machine slept through; the battery
# settings keep it working on a laptop that is rarely plugged in.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                                         -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries `
                                         -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

try {
    Register-ScheduledTask -TaskName $TaskName `
                           -Action $action `
                           -Trigger $triggers `
                           -Settings $settings `
                           -Description 'Refreshes the Skool MCP session cookie every 2 days.' `
                           -Force | Out-Null
} catch {
    Fail "Could not register the scheduled task: $($_.Exception.Message)"
}

Write-Host ''
Write-Host "Installed $TaskName - runs at logon and every 2 days at $AtTime." -ForegroundColor Green
Write-Host "  Folder: $RepoDir"
Write-Host "  Runner: $UvPath run refresh_cookie.py"
Write-Host "  Log:    $LogFile"

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir '.env'))) {
    Write-Warning "No .env in this folder yet. Run connect_skool.py first, or the job will fail."
}

Write-Host ''
Write-Host 'Test it now:'
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  Get-Content `"$LogFile`" -Tail 3        # expect: OK: cookie refreshed"
Write-Host ''
Write-Host 'If the log stays empty, uv itself failed before Python started. Check the result code:'
Write-Host "  Get-ScheduledTaskInfo -TaskName `"$TaskName`" | Select-Object LastRunTime, LastTaskResult"
Write-Host ''
Write-Host 'Uninstall:'
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
