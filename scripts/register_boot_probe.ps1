#Requires -Version 5.1
<#
.SYNOPSIS
    Registers the boot-time readiness probe as a Task Scheduler entry.
    Run from an ELEVATED PowerShell prompt (Start-Process powershell -Verb RunAs).

.DESCRIPTION
    Registers "03-OPENC-BootProbe" to run the readiness probe at startup,
    whether or not the user is logged on. Uses the specified account with
    stored credentials (the brief requires "run whether user is logged on or
    not", which needs a stored password).

    Usage:   .\register_boot_probe.ps1
             .\register_boot_probe.ps1 -User HP -Password (secret)
    Or set the OPENENC_RUN_ACCOUNT / OPENENC_RUN_PASSWORD env vars first.

.PARAMETER User
    Windows account to run the probe under (default: current user).

.PARAMETER Password
    Password for that account (stored with the task).
#>
[CmdletBinding()]
param(
    [string]$User = $env:USERNAME,
    [string]$Password = $env:OPENENC_RUN_PASSWORD
)

$ProbeScript = "C:\Users\HP\Desktop\Kameshwar\03-OPENC\pipeline\scripts\boot_probe.ps1"
$TaskName    = "03-OPENC-BootProbe"

if (-not (Test-Path $ProbeScript)) {
    Write-Error "Boot probe script not found: $ProbeScript"; exit 1
}

# Validate we're elevated enough to create a task for another account / with stored creds.
if (-not $Password) {
    Write-Warning "No password supplied. If the task must run 'whether user is logged on or not', a stored password is required - pass -Password or set OPENENC_RUN_PASSWORD."
}

$action  = New-ScheduledTaskAction -Execute "pwsh" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProbeScript`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($Password) {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings `
        -User $User -Password $Password -RunLevel Limited -Force | Out-Null
    Write-Host "Task registered to run '$User' whether logged on or not."
} else {
    # No stored credential - run only while the user is logged on. To run
    # regardless of login, pass -Password or set OPENENC_RUN_PASSWORD (elevated).
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings `
        -RunLevel Limited -Force | Out-Null
    Write-Host "Task registered to run only while '$User' is logged on (no stored credential)."
}

$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Host "Registered '$TaskName':"
Write-Host "  State:      $($registered.State)"
Write-Host "  Triggers:   $((($registered.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '))"
Write-Host "  Script:     $ProbeScript"
