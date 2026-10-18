@echo off
title TongLing Frontend 5173
cd /d "%~dp0frontend"
echo Frontend live log: http://127.0.0.1:5173
node server.mjs host 5173 8000
if errorlevel 1 pause
