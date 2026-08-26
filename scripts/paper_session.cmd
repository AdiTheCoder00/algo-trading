@echo off
REM ---------------------------------------------------------------------------
REM One PAPER trading session, start to finish. No real orders: `algo live`
REM refuses anything but backtest/paper mode before it reads a credential.
REM
REM Sized for the MCX session, 09:00-23:30 IST:
REM   --poll 120   a 30-minute bar cannot be missed by more than two minutes,
REM                and this is a quarter of the API calls a 30s poll would make.
REM   --passes 450 450 x 120s = 15 hours, which covers the session and stops.
REM                The loop is bounded on purpose - it will not still be
REM                trading tomorrow because nobody turned it off.
REM   --wait-for-bar 45
REM                Starting at 09:00 there is no closed 30-minute bar until
REM                09:30. Without this the run would find an empty feed and
REM                abandon the whole day - which is what the first version of
REM                this script would have done.
REM
REM The Kotak session token is minted at connect and expires at midnight IST,
REM so a 09:00 start covers the whole day with no mid-session re-login.
REM
REM Output goes to runs\ (gitignored - it carries account identifiers).
REM ---------------------------------------------------------------------------
cd /d "D:\algo trading"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set STAMP=%%i
set LOG=runs\paper_session_%STAMP%.log

echo ===== paper session started %DATE% %TIME% ===== >> "%LOG%"
".venv\Scripts\algo.exe" live config\goldm.yaml ^
    --passes 450 ^
    --poll 120 ^
    --wait-for-bar 45 ^
    --state state\paper_live.db >> "%LOG%" 2>&1
echo ===== paper session ended %DATE% %TIME% (exit %ERRORLEVEL%) ===== >> "%LOG%"
