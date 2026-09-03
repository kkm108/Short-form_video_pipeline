#Requires -Version 5.1
<#
.SYNOPSIS
    Registers the per-platform credential refresh as scheduled tasks.
    Run from an ELEVATED prompt (admin) - register-boot-probe has the same
    constraint; Task Scheduler creation needs elevation.

.DESCRIPTION
    Registers "03-OPENC-RefreshInstagram" to run refresh_tokens.py for
    instagram on a cadence comfortably inside the 60-day long-lived window
    (default: every 15 days). Use -Days to set the interval.

    Use the SAME -User / -Password (or OPENENC_RUN_PASSWORD) as
    register_boot_probe.ps1 so the refresh runs whether or not you're logged on.

    Usage:
        .\register_tokens.ps1                     # instagram, every 15 days
        .\register_tokens.ps1 -Days 20
#>
[CmdletBinding()]
param(
    [string]$User = $env:USERNAME,
    [string]$Password = $env:OPENENC_RUN_PASSWORD,
    [ValidateRange(1, 59)][int]$Days = 15
)

$RepoDir   = "C:\Users\HP\Desktop\Kameshwar\03-OPENC\pipeline"
$RefreshPy = Join-Path $RepoDir "scripts\refresh_tokens.py"

if (-not (Test-Path $RefreshPy)) {
    Write-Error "refresh_tokens.py not found: $RefreshPy"; exit 1
}

# ---- Instagram refresh (every {Days} days, well inside the 60-day window) ----
$pythonExe = (Get-Command python).Source
$igAction = New-ScheduledTaskAction -Execute $pythonExe `
    -Argument "`"$RefreshPy`" instagram" `
    -WorkingDirectory $RepoDir
$igTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval $Days -At 03:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($Password) {
    Register-ScheduledTask -TaskName "03-OPENC-RefreshInstagram" `
        -Action $igAction -Trigger $igTrigger -Settings $settings `
        -User $User -Password $Password -RunLevel Limited -Force | Out-Null
    Write-Host "Registered 03-OPENC-RefreshInstagram (every $Days days, run-as $User, whether logged on or not)."
} else {
    Register-ScheduledTask -TaskName "03-OPENC-RefreshInstagram" `
        -Action $igAction -Trigger $igTrigger -Settings $settings `
        -RunLevel Limited -Force | Out-Null
    Write-Host "Registered 03-OPENC-RefreshInstagram (every $Days days, only while $User is logged on)."
}

Write-Host ""
Write-Host "TikTok and YouTube have NO auto-refresh endpoint (see refresh_tokens.py):"
Write-Host "  - TikTok: 24h token - refresh via OAuth app flow before expiry, or the job can't mint a new one."
Write-Host "  - YouTube: google-auth auto-refreshes; only a one-time Production consent-screen check is needed."
