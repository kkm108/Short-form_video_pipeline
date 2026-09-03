#Requires -Version 5.1
<#
.SYNOPSIS
    Idempotent machine setup for the 03-OPENC pipeline.
    Safe to re-run on a clean machine or after a Python patch update.

.DESCRIPTION
    1. Creates a symlink at ~/bin/python3.exe -> the installed python.exe
       (replaces the stale copy that silently drifts after updates).
    2. Ensures every directory a pipeline tool needs is on the user PATH.
    3. Verifies every required command resolves, printing PASS/FAIL per tool.

.NOTES
    Run from an elevated or normal PowerShell prompt. Symlink creation requires
    either Administrator privileges or Developer Mode enabled on Windows 10+.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RequiredTools = @(
    @{ Name = "python";  Fallback = $null }
    @{ Name = "pip";     Fallback = $null }
    @{ Name = "python3"; Fallback = $null }
    @{ Name = "node";    Fallback = $null }
    @{ Name = "git";     Fallback = $null }
    @{ Name = "ffmpeg";  Fallback = $null }
    @{ Name = "ruff";    Fallback = "python -m ruff" }
    @{ Name = "mypy";    Fallback = "python -m mypy" }
    @{ Name = "keyring"; Fallback = "python -m keyring" }
)
$RequiredPaths = @(
    "C:\Users\HP\AppData\Roaming\Python\Python314\Scripts"
    "C:\Users\HP\AppData\Local\Programs\AutoHotkey\v2"
    "C:\Users\HP\AppData\Local\Programs\AutoHotkey\v1.1.37.02"
)

# ── Helpers ──────────────────────────────────────────────────────────────
function Find-Python {
    foreach ($name in @("python", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "python.exe not found on PATH."
}

function Ensure-Path {
    param([string]$Dir)
    $current = [Environment]::GetEnvironmentVariable("PATH", "User")
    $found = $false
    foreach ($entry in ($current -split ";")) {
        if ($entry -and ([IO.Path]::GetFullPath($entry) -eq [IO.Path]::GetFullPath($Dir))) {
            $found = $true; break
        }
    }
    if (-not $found) {
        [Environment]::SetEnvironmentVariable("PATH", "$current;$Dir", "User")
        $env:PATH = "$env:PATH;$Dir"
        Write-Host "  PATH added: $Dir"
    }
}

# ── 1. python3 symlink ──────────────────────────────────────────────────
Write-Host "`n[1/3] python3 shim" -ForegroundColor Cyan
$pythonExe  = Find-Python
$pythonDir  = Join-Path $env:USERPROFILE "bin"
$python3Dst = Join-Path $pythonDir "python3.exe"

if (Test-Path $python3Dst) {
    $item = Get-Item $python3Dst -Force
    if ($item.LinkType) {
        Write-Host "  already linked ($($item.LinkType)) to $($item.Target) - ok"
    } else {
        Write-Host "  removing stale copy ($($item.Length) bytes)..."
        Remove-Item $python3Dst -Force
        try {
            New-Item -ItemType SymbolicLink -Path $python3Dst -Target $pythonExe | Out-Null
            Write-Host "  symlink created: $python3Dst -> $pythonExe"
        } catch {
            try {
                New-Item -ItemType HardLink -Path $python3Dst -Target $pythonExe | Out-Null
                Write-Host "  hardlink created (symlink needs admin/DevMode): $python3Dst -> $pythonExe"
            } catch {
                Copy-Item $pythonExe $python3Dst -Force
                Write-Host "  WARNING: copy fallback (both link types failed): $python3Dst = $pythonExe" -ForegroundColor Yellow
                Write-Host "    Enable Developer Mode for user-level symlinks, or run as Administrator."
            }
        }
    }
} else {
    if (-not (Test-Path $pythonDir)) { New-Item -ItemType Directory -Path $pythonDir | Out-Null }
    try {
        New-Item -ItemType SymbolicLink -Path $python3Dst -Target $pythonExe | Out-Null
        Write-Host "  symlink created: $python3Dst -> $pythonExe"
    } catch {
        try {
            New-Item -ItemType HardLink -Path $python3Dst -Target $pythonExe | Out-Null
            Write-Host "  hardlink created: $python3Dst -> $pythonExe"
        } catch {
            Copy-Item $pythonExe $python3Dst -Force
            Write-Host "  WARNING: copy fallback: $python3Dst = $pythonExe" -ForegroundColor Yellow
            Write-Host "    Enable Developer Mode for user-level symlinks, or run as Administrator."
        }
    }
}

# Ensure ~/bin is on PATH
Ensure-Path -Dir $pythonDir

# ── 2. PATH entries ─────────────────────────────────────────────────────
Write-Host "`n[2/3] PATH entries" -ForegroundColor Cyan
foreach ($entry in $RequiredPaths) { Ensure-Path -Dir $entry }

# ── 3. Verify commands ──────────────────────────────────────────────────
Write-Host "`n[3/3] Verify commands" -ForegroundColor Cyan
$failed = 0
foreach ($tool in $RequiredTools) {
    $resolved = $null
    if (Get-Command $tool.Name -ErrorAction SilentlyContinue) {
        $resolved = (Get-Command $tool.Name).Source
    } elseif ($tool.Fallback) {
        $resolved = "$($tool.Fallback) (fallback)"
    }
    if ($resolved) {
        Write-Host "  PASS $($tool.Name) -> $resolved"
    } else {
        Write-Host "  FAIL $($tool.Name) not found on PATH" -ForegroundColor Red
        $failed++
    }
}

# ── 3b. python3 must actually BE python (gotcha) ────────────────────────
# A bare `python3` on Windows commonly resolves to the Microsoft Store "App
# Execution Alias" stub (AppData\...\WindowsApps\python3.exe) that prints
# "Python was not found" and runs nothing - or to a stale copied shim that
# drifted from a patched python.exe. `Get-Command` treats all of those as
# "found", so the loop above would mark them PASS while `python3 -m tests`
# silently does nothing. The real correctness test is: does `python3` run at
# all, and does it run the SAME interpreter as `python`?
Write-Host "`n[3b/3] python3===python consistency"
$pyVer  = (& python  --version 2>&1 | Select-Object -First 1) -as [string]
$py3Ver = (& python3 --version 2>&1 | Select-Object -First 1) -as [string]
$py3Src = (Get-Command python3 -ErrorAction SilentlyContinue).Source
if ($py3Ver -match 'Python 3' -and $py3Ver -eq $pyVer) {
    Write-Host "  PASS python3 -> $py3Src -> $py3Ver (matches python: $pyVer)"
} else {
    if ($py3Src -match 'WindowsApps') {
        Write-Host "  FAIL python3 resolves to the Store App Execution Alias stub ($py3Src) - it prints 'Python was not found' and runs nothing" -ForegroundColor Red
        Write-Host "       Run this script, or re-point PATH so ~/bin/python3.exe (the symlink to python.exe) precedes WindowsApps." -ForegroundColor Yellow
    } else {
        Write-Host "  FAIL python3 -> $py3Src -> '$py3Ver' but python -> '$pyVer' - stale copy or mismatched interpreter" -ForegroundColor Red
    }
    $failed++
}

# ── Result ──────────────────────────────────────────────────────────────
Write-Host ""
if ($failed -eq 0) {
    Write-Host "bootstrap complete - all tools resolve" -ForegroundColor Green
} else {
    Write-Host "bootstrap finished with $failed failure(s)" -ForegroundColor Red
    exit 1
}
