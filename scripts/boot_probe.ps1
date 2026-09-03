#Requires -Version 5.1
<#
.SYNOPSIS
    Boot-time readiness probe for the 03-OPENC pipeline.
    Registered as a Task Scheduler entry ("At startup"). Checks that the
    machine can actually run a pipeline step, and fails loud if it can't.

.DESCRIPTION
    1. Confirms the pipeline repo directory exists.
    2. Confirms python, ffmpeg, and other critical commands resolve.
    3. Confirms the vault can unlock (keyring or env-var path) for at
       least the Gemini key - the minimum needed for a script + media run.
    4. Checks disk free space on the system drive.
    Results go to a timestamped log file in runs/probe_logs/.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoDir   = "C:\Users\HP\Desktop\Kameshwar\03-OPENC\pipeline"
$ProbeDir  = Join-Path $RepoDir "runs\probe_logs"
$LogFile   = Join-Path $ProbeDir ("probe_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))
$MinFreeGB = 5

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
}

function Test-CommandExists {
    param([string]$Name)
    [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# ── Setup log dir ────────────────────────────────────────────────────────
if (-not (Test-Path $ProbeDir)) { New-Item -ItemType Directory -Path $ProbeDir -Force | Out-Null }
Write-Log "probe started"

# ── 1. Pipeline repo directory ──────────────────────────────────────────
$errors = 0
if (Test-Path $RepoDir) {
    Write-Log "repo dir OK: $RepoDir"
} else {
    Write-Log "repo dir MISSING: $RepoDir" "ERROR"
    $errors++
}

# ── 2. Critical commands ────────────────────────────────────────────────
$criticalCommands = @("python", "ffmpeg", "git")
foreach ($cmd in $criticalCommands) {
    if (Test-CommandExists $cmd) {
        Write-Log "command OK: $cmd -> $((Get-Command $cmd).Source)"
    } else {
        Write-Log "command MISSING: $cmd" "ERROR"
        $errors++
    }
}

# ── 3. Vault / credential check ────────────────────────────────────────
# Use a Python one-liner to probe the vault the same way the pipeline does.
# Required key: gemini_api_key (env GEMINI_API_KEY or keyring).
$prompt = @"
import sys; sys.path.insert(0, r'$RepoDir')
try:
    from credentials.vault import Vault, SERVICE_NAME
    v = Vault()
    v.get('gemini_api_key')
    print('VAULT_OK')
except Exception as exc:
    print(f'VAULT_FAIL: {exc}')
"@
try {
    $result = & python -c $prompt 2>&1
    if ($result -match "VAULT_OK") {
        Write-Log "vault OK: gemini_api_key resolves"
    } else {
        Write-Log "vault FAIL: $result" "ERROR"
        $errors++
    }
} catch {
    Write-Log "vault check crashed: $_" "ERROR"
    $errors++
}

# ── 4. Disk space ───────────────────────────────────────────────────────
$sysDrive = $env:SystemDrive
$driveName = $sysDrive.TrimEnd(":")
$freeGB = [math]::Round((Get-PSDrive $driveName).Free / 1GB, 2)
if ($freeGB -ge $MinFreeGB) {
    Write-Log "disk OK: ${freeGB}GB free on $sysDrive"
} else {
    Write-Log "disk LOW: ${freeGB}GB free on $sysDrive (minimum ${MinFreeGB}GB)" "ERROR"
    $errors++
}

# ── Result ──────────────────────────────────────────────────────────────
Write-Log "probe complete: $errors error(s)"
if ($errors -gt 0) {
    Write-Log "READINESS FAILED - pipeline runs will not succeed until this is fixed" "ERROR"
    exit 1
}
