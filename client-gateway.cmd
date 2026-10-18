@echo off
title TongLing Client Gateway 8001
cd /d "%~dp0backend"
set "DATA_DIR=%~dp0data"
set "TONGLING_CLIENT_GATEWAY=1"
echo Client gateway live log: http://127.0.0.1:8001
"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8001
if errorlevel 1 pause
