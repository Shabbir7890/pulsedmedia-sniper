# Show whether the sniper is running, and what it has been doing.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "  PULSEDMEDIA SNIPER - STATUS" -ForegroundColor Cyan
Write-Host "  ============================" -ForegroundColor Cyan
Write-Host ""

$pidFile = Join-Path $root 'sniper.pid'
$proc = $null
if (Test-Path $pidFile) {
    $procId = (Get-Content $pidFile | Select-Object -First 1).Trim()
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
}

if ($proc) {
    $up = (Get-Date) - $proc.StartTime
    Write-Host ("  STATUS   : RUNNING") -ForegroundColor Green
    Write-Host ("  PID      : {0}" -f $proc.Id)
    Write-Host ("  Started  : {0}" -f $proc.StartTime)
    Write-Host ("  Uptime   : {0}h {1}m" -f [int]$up.TotalHours, $up.Minutes)
} else {
    Write-Host ("  STATUS   : NOT RUNNING") -ForegroundColor Red
    Write-Host ("  Start it by double-clicking start.bat")
}

# Armed or not
$dry = (Select-String -Path (Join-Path $root '.env') -Pattern '^PM_DRY_RUN=(.*)$' -ErrorAction SilentlyContinue).Matches.Groups[1].Value
if ($dry -eq 'false') {
    Write-Host ("  MODE     : ARMED - will place a real order") -ForegroundColor Yellow
} else {
    Write-Host ("  MODE     : DRY RUN - will NOT order") -ForegroundColor DarkGray
}

$log = Join-Path $root 'sniper.log'
if (Test-Path $log) {
    $last = (Get-Item $log).LastWriteTime
    $age = (Get-Date) - $last
    Write-Host ("  Last log : {0}  ({1:N0} min ago)" -f $last, $age.TotalMinutes)
    if ($proc -and $age.TotalMinutes -gt 15) {
        Write-Host "  WARNING: log is stale - check it is still polling" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  --- last 12 log lines ---" -ForegroundColor Cyan
    Get-Content $log -Tail 12 | ForEach-Object { Write-Host "  $_" }
}

$err = Join-Path $root 'sniper.err.log'
if ((Test-Path $err) -and (Get-Item $err).Length -gt 0) {
    Write-Host ""
    Write-Host "  --- errors ---" -ForegroundColor Red
    Get-Content $err -Tail 8 | ForEach-Object { Write-Host "  $_" }
}
Write-Host ""
