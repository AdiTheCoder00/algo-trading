# Registers "Kotak Excel Bridge" as a Windows Scheduled Task: one refresh of
# the workbook each trading morning, shortly after MCX opens.
#
# Run this once, elevated: right-click this file -> "Run with PowerShell",
# and accept the UAC prompt. Registering a scheduled task needs an
# administrator token even for a task that only runs as your own user - a
# non-elevated PowerShell gets "Access is denied" on ANY task creation
# (tools/macd_telegram_alert/setup_scheduled_task.ps1 found the same).
#
# What it sets up:
#   - Weekdays at 09:05 IST. MCX opens at 09:00 and trades Monday to Friday,
#     so a daily trigger would fire into a closed market twice a week and log
#     two failures that mean nothing. The five extra minutes let the first
#     quotes settle rather than polling into the opening auction.
#   - One refresh, then exit. excel_bridge.py with no flags does exactly one
#     poll - that is the shape it was written for. For a sheet that keeps
#     updating while you watch it, run `--loop` by hand instead; a scheduled
#     task is the wrong home for a process meant to run all session.
#   - Runs only while you are logged on, so no password is stored anywhere.
#     Excel is an interactive application: there is no desktop to attach a
#     workbook to when nobody is signed in, so this could not usefully run
#     logged-out even if you wanted it to.
#   - StartWhenAvailable, so a morning where the machine was asleep at 09:05
#     still gets its refresh when you wake it, rather than silently skipping.
#
# This does NOT need your credentials and does not read them. If .env still
# holds template values the run will fail and say so in data\excel_bridge.log.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$runner = Join-Path $root "scripts\run_excel_bridge.ps1"
$taskName = "Kotak Excel Bridge"

if (-not (Test-Path $runner)) {
    throw "missing $runner - this script expects to live in scripts\ next to it"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 9:05am

# A single refresh finishes in well under a minute; an hour is generous
# headroom for a slow instrument-master download on the first run, while
# still guaranteeing a hung task cannot sit there until the next morning.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings `
    -Description "One refresh of data\kotak_bridge.xlsx from Kotak Neo each trading morning. scripts/excel_bridge.py" `
    -Force | Out-Null

Write-Host "Registered '$taskName'." -ForegroundColor Green

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "State:    $((Get-ScheduledTask -TaskName $taskName).State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Log:      $root\data\excel_bridge.log" -ForegroundColor Cyan
Write-Host "Test now: Start-ScheduledTask -TaskName '$taskName'" -ForegroundColor Cyan
Write-Host "Remove:   Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Enter to close this window..."
Read-Host | Out-Null
