@echo off
title TongLing ngrok Tunnel
cd /d "%~dp0"
echo ngrok live log: forwarding to client frontend 8002
ngrok http 8002 --log=stdout
if errorlevel 1 pause
