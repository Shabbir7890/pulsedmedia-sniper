# Undo everything the sniper set up on this machine.
#
#   .\uninstall.ps1 -DryRun     # show what would happen, change nothing
#   .\uninstall.ps1             # stop it, remove the task, restore power settings
#   .\uninstall.ps1 -PurgeFiles # ...and delete the state files, including the
#                               #    saved WHMCS session cookie
#
# The project folder itself is left alone - delete it by hand afterwards.

param(
    [switch]$DryRun,
    [switch]$PurgeFiles,
    [switch]$RestoreDefaultSchemes
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$did = @()

function Step($label, [scriptblock]$action) {
    if ($DryRun) { Write-Host ("  WOULD  " + $label) -ForegroundColor DarkGray; return }
    try {
        & $action
        Write-Host ("  DONE   " + $label) -ForegroundColor Green
        $script:did += $label
    } catch {
        Write-Host ("  FAILED " + $label + " : " + $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  PULSEDMEDIA SNIPER - UNINSTALL" -ForegroundColor Cyan
if ($DryRun) { Write-Host "  (dry run - nothing will change)" -ForegroundColor Yellow }
Write-Host ""

# 1. the scheduled task, first - otherwise it restarts what we are about to stop
Write-Host "Scheduled task"
if (Get-ScheduledTask -TaskName 'PulsedSniperWatchdog' -ErrorAction SilentlyContinue) {
    Step "unregister PulsedSniperWatchdog" {
        Unregister-ScheduledTask -TaskName 'PulsedSniperWatchdog' -Confirm:$false
    }
} else {
    Write-Host "  SKIP   PulsedSniperWatchdog is not registered" -ForegroundColor DarkGray
}

# 2. the sniper itself
Write-Host "Process"
$procs = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*sniper.py*' })
if ($procs.Count -gt 0) {
    foreach ($p in $procs) {
        $procId = $p.ProcessId
        Step ("stop sniper pid " + $procId) { Stop-Process -Id $procId -Force }
    }
} else {
    Write-Host "  SKIP   the sniper is not running" -ForegroundColor DarkGray
}

# 3. power settings
#
# Only ONE original value is known for certain: the AC sleep timeout was 4 hours
# (0x3840) before the sniper changed it. The unattended-sleep and lid-close
# originals were never captured - they are restored to the usual Windows
# defaults below, which may not be what this machine had. Use
# -RestoreDefaultSchemes for a guaranteed-correct reset of ALL power settings
# (it also discards any unrelated tuning you have done).
Write-Host "Power settings"
if ($RestoreDefaultSchemes) {
    Step "restore all power schemes to Windows defaults" {
        powercfg -restoredefaultschemes
        if ($LASTEXITCODE -ne 0) { throw "powercfg exited $LASTEXITCODE" }
    }
} else {
    Step "AC sleep timeout back to 4 hours" {
        powercfg /change standby-timeout-ac 240
        if ($LASTEXITCODE -ne 0) { throw "powercfg exited $LASTEXITCODE" }
    }
    Step "AC unattended sleep back to 2 minutes (assumed default)" {
        powercfg /setacvalueindex SCHEME_CURRENT `
            238c9fa8-0aad-41ed-83f4-97be242c8f20 7bc4a2f9-d8fc-4469-b07b-33eb785aaca0 120
        powercfg /attributes 238c9fa8-0aad-41ed-83f4-97be242c8f20 `
            7bc4a2f9-d8fc-4469-b07b-33eb785aaca0 +ATTRIB_HIDE
    }
    Step "lid close on AC back to sleep (assumed default)" {
        powercfg /setacvalueindex SCHEME_CURRENT `
            4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 1
        powercfg /attributes 4f971e89-eebd-4455-a8de-9e59040e7347 `
            5ca83367-6e45-459f-a27b-476b1d01c936 +ATTRIB_HIDE
    }
    Step "apply the scheme" { powercfg /setactive SCHEME_CURRENT }
}

# 4. state files
Write-Host "Files"
$state = @('session.json', 'targets.json', 'state.json', 'sniper.lock', 'sniper.pid',
           'sniper.log', 'sniper.err.log', 'watchdog.log')
if ($PurgeFiles) {
    foreach ($f in $state) {
        $path = Join-Path $root $f
        if (Test-Path $path) { Step ("delete " + $f) { Remove-Item $path -Force } }
    }
    Write-Host "  NOTE   .env still holds your Telegram token and cookie - delete it too" -ForegroundColor Yellow
} else {
    $present = $state | Where-Object { Test-Path (Join-Path $root $_) }
    Write-Host ("  KEPT   " + ($present -join ', ')) -ForegroundColor DarkGray
    Write-Host "  NOTE   session.json holds a LIVE WHMCS session cookie. Re-run with" -ForegroundColor Yellow
    Write-Host "         -PurgeFiles to delete it, or delete it by hand." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Left for you to do:" -ForegroundColor Cyan
Write-Host "  - log out of pulsedmedia.com in the browser (kills the session server-side)"
Write-Host "  - revoke the bot token: message @BotFather, /revoke, pick your bot"
Write-Host "  - delete this folder: $root"
Write-Host ""
if (-not $DryRun -and $did.Count -gt 0) {
    Write-Host ("Removed " + $did.Count + " thing(s).") -ForegroundColor Green
}
Write-Host ""
