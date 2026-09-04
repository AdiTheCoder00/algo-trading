# Entry point for the "Kotak Excel Bridge" scheduled task.
#
# Task Scheduler starts a fresh environment with none of an interactive
# shell's state: no working directory, no PATH to the venv, and nowhere for
# stdout to go. This script supplies all three, which is the only reason it
# exists separately from excel_bridge.py.
#
# It deliberately does NOT pass --trade. A scheduled run that establishes a
# trade session would leave a live broker session open on a timer with nobody
# watching; quotes and the chain need only the consumer key. Add --trade here
# once you actually want positions and P&L on the sheet.
#
# Output is appended, never truncated, so a morning that fails leaves evidence
# next to the mornings that worked.

$ErrorActionPreference = "Stop"

# This script lives in scripts/, so the repo root is its parent.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -Path $root

$python = Join-Path $root ".venv\Scripts\python.exe"
$bridge = Join-Path $root "scripts\excel_bridge.py"
$log = Join-Path $root "data\excel_bridge.log"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null

# A dated banner per run: without it, appended tracebacks from different days
# run together and the log stops being readable exactly when it matters.
"" | Out-File -FilePath $log -Append -Encoding utf8
"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -FilePath $log -Append -Encoding utf8

if (-not (Test-Path $python)) {
    "FATAL: no venv at $python - run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e `".[dev,excel]`"" |
        Out-File -FilePath $log -Append -Encoding utf8
    exit 1
}

# Redirected through cmd rather than PowerShell's `*>>`, which on Windows
# PowerShell 5.1 writes UTF-16 and leaves the log full of NUL bytes that no
# ordinary reader renders. cmd appends the process's raw bytes, so structlog's
# UTF-8 output lands intact. It also sidesteps 5.1 wrapping each stderr line
# from a native executable in a NativeCommandError - structlog writes to
# stderr, so every line would otherwise arrive as a PowerShell error object.
& cmd.exe /c "`"$python`" `"$bridge`" >> `"$log`" 2>&1"
exit $LASTEXITCODE
