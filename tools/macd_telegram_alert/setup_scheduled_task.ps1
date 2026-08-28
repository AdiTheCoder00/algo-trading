# Registers "MACD Telegram Alert" as a Windows Scheduled Task so the monitor
# survives a reboot and restarts itself on a logon, instead of only surviving
# as long as one nohup'd process happens to stay alive.
#
# Run this once, elevated: right-click this file -> "Run with PowerShell",
# and accept the UAC prompt. (Registering a scheduled task needs an
# administrator token even for a task that only runs as your own user -
# a non-elevated PowerShell gets "Access is denied" on ANY task creation,
# confirmed by probing with a trivial one.)
#
# What it sets up:
#   - Runs run_live.ps1 (which sets REQUESTS_CA_BUNDLE, then launches
#     macd_alert.py) at your next logon, and immediately, right now.
#   - No execution time limit - Task Scheduler kills tasks after 3 days by
#     default, which would silently stop a monitor meant to run forever.
#   - Restarts up to 999 times, 1 minute apart, if the process ever exits.
#   - Runs only while you are logged on (Interactive/Limited) - no password
#     is stored. If you want it to survive being logged out entirely, that
#     needs a stored password, which this script deliberately does not do;
#     set that up yourself via Task Scheduler's GUI if you want it.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run_live.ps1`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "MACD Telegram Alert" -Action $action -Trigger $trigger `
    -Settings $settings `
    -Description "Live MACD(12,26,9) crossover monitor for BTC/USDT on binance, alerts via Telegram. tools/macd_telegram_alert" `
    -Force | Out-Null

Write-Host "Registered 'MACD Telegram Alert'." -ForegroundColor Green

# Stop any nohup'd instance so the scheduled task's copy is the only one running.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*macd_alert.py*" } |
    ForEach-Object {
        Write-Host "Stopping existing process $($_.ProcessId)..." -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 1

Start-ScheduledTask -TaskName "MACD Telegram Alert"
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName "MACD Telegram Alert"
Write-Host "State: $((Get-ScheduledTask -TaskName 'MACD Telegram Alert').State)   LastTaskResult: $($info.LastTaskResult)"
Write-Host ""
Write-Host "Check tools\macd_telegram_alert\macd_alert.log for output." -ForegroundColor Cyan
Write-Host "Press Enter to close this window..."
Read-Host | Out-Null
