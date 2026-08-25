@echo off
cd /d "D:\algo trading"
"D:\algo trading\.venv\Scripts\python.exe" "D:\algo trading\scripts\mcx_live_to_excel.py" >> "D:\algo trading\state\mcx_poll.log" 2>&1
"D:\algo trading\.venv\Scripts\python.exe" "D:\algo trading\scripts\mcx_option_chain_to_excel.py" >> "D:\algo trading\state\mcx_poll.log" 2>&1
