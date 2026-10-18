@echo off
title TongLing Demo Company File Server 8765
cd /d "%~dp0"
set "COMPANY_FILES_DIR=%~dp0company_files"
echo Company file server live log: http://127.0.0.1:8765
"%~dp0.venv\Scripts\python.exe" -m uvicorn company_file_server.app:app --host 127.0.0.1 --port 8765
if errorlevel 1 pause
