@echo off
REM Serve XAUUSD + GOLDM to the phone app and home-screen widget.
REM
REM Reachable over Tailscale at http://algo-pc.taile5cae1.ts.net:8787 -- that name
REM resolves from mobile data and any other network, not just this LAN.
REM
REM The token is read from state\relay_token.txt, which is gitignored. Do NOT
REM inline it here: this file is tracked in a public repository, so a literal
REM token would be published on the next commit.
REM
REM Poll interval is seconds, matched to the app and widget so the relay is never
REM the bottleneck. Spot open/high/low and previous close accumulate in
REM state\spot_session.json and survive restarts, so the --seed-* flags are only
REM needed on a genuinely cold start.

cd /d "D:\algo trading"

if not exist "D:\algo trading\state\relay_token.txt" (
  echo Missing state\relay_token.txt -- generate one with:
  echo   .venv\Scripts\python -c "import secrets;print(secrets.token_urlsafe(18))" ^> state\relay_token.txt
  exit /b 1
)
set /p RELAY_TOKEN=<"D:\algo trading\state\relay_token.txt"

"D:\algo trading\.venv\Scripts\python.exe" "D:\algo trading\scripts\price_publisher.py" ^
  --serve --port 8787 --loop 2 ^
  --token %RELAY_TOKEN% ^
  --out "D:\algo trading\state\prices.json" ^
  >> "D:\algo trading\state\price_widget.log" 2>&1
