# Launch the sniper on this machine using settings from a local .env file.
#
#   .\run_local.ps1            # run in this window (Ctrl+C to stop)
#   .\run_local.ps1 -Detached  # run in the background, logging to sniper.log
#
# .env is gitignored: secrets never reach the repo.
param([switch]$Detached)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $root '.env'

if (-not (Test-Path $envFile)) {
    Write-Error "No .env found at $envFile. Copy .env.example to .env and fill it in."
}

# Load KEY=VALUE lines, ignoring blanks and # comments.
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
        $i = $line.IndexOf('=')
        $key = $line.Substring(0, $i).Trim()
        $val = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$key" -Value $val
    }
}

$py = (Get-Command python).Source
Write-Host "python : $py"
Write-Host "target : pid $env:PM_PID  mode=$env:PM_MODE  dry_run=$env:PM_DRY_RUN"

if ($Detached) {
    $log = Join-Path $root 'sniper.log'
    $proc = Start-Process -FilePath $py `
        -ArgumentList 'sniper.py' `
        -WorkingDirectory $root `
        -RedirectStandardOutput $log `
        -RedirectStandardError (Join-Path $root 'sniper.err.log') `
        -WindowStyle Hidden -PassThru
    $proc.Id | Out-File (Join-Path $root 'sniper.pid') -Encoding ascii
    Write-Host ""
    Write-Host "Started detached. PID $($proc.Id)"
    Write-Host "  follow log : Get-Content '$log' -Wait -Tail 20"
    Write-Host "  stop       : Stop-Process -Id $($proc.Id)"
} else {
    & $py (Join-Path $root 'sniper.py')
}
