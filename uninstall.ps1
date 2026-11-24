# Undo everything the sniper set up on this machine.
#
#   .\uninstall.ps1 -DryRun     # show what would happen, change nothing
#   .\uninstall.ps1             # stop it and remove the watchdog task
#   .\uninstall.ps1 -RestorePower  # ...and reset ALL power schemes to Windows defaults
#   .\uninstall.ps1 -PurgeFiles # ...and delete the state files, including the
#                               #    saved WHMCS session cookie
#
# The project folder itself is left alone - delete it by hand afterwards.

param(
    [switch]$DryRun,
    [switch]$PurgeFiles,
    [switch]$RestorePower
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
# The README asks you to turn sleep off by hand, and your original values were
# never recorded, so the only safe reset is Windows' own defaults. Opt-in,
# because it also discards any unrelated power tuning.
Write-Host "Power settings"
if ($RestorePower) {
    Step "restore all power schemes to Windows defaults" {
        powercfg -restoredefaultschemes
        if ($LASTEXITCODE -ne 0) { throw "powercfg exited $LASTEXITCODE" }
    }
} else {
    Write-Host "  SKIP   left as they are (use -RestorePower to reset to Windows defaults)" -ForegroundColor DarkGray
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
