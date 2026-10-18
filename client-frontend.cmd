@echo off
title TongLing Client Frontend 8002
cd /d "%~dp0frontend"
echo Client frontend live log: http://127.0.0.1:8002
node server.mjs client 8002 8001
if errorlevel 1 pause
