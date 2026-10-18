@echo off
title TongLing Backend 8000
cd /d "%~dp0backend"
set "DATA_DIR=%~dp0data"
set "TONGLING_LAN=0"
set "TONGLING_CLIENT_GATEWAY=0"
echo Backend live log: http://127.0.0.1:8000
"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 pause
