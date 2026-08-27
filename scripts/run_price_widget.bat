@echo off
REM Serve XAUUSD + GOLDM to the phone widget page.
REM
REM Open http://<this-machine-lan-ip>:8787/ on the phone and use Chrome's
REM "Add to Home screen". Add --token <secret> below if this is ever exposed
REM beyond the home network.
REM
REM Poll interval is seconds. It matches the app/widget so the relay is never the
REM bottleneck. Spot open/high/low and previous close accumulate in
REM state\spot_session.json and survive restarts, so the one-off --seed-* flags
REM are only needed on a genuinely cold start.
cd /d "D:\algo trading"
"D:\algo trading\.venv\Scripts\python.exe" "D:\algo trading\scripts\price_publisher.py" ^
  --serve --port 8787 --loop 2 ^
  --out "D:\algo trading\state\prices.json" ^
  >> "D:\algo trading\state\price_widget.log" 2>&1
