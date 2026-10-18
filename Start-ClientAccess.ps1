param([int]$AdminPort = 8000, [int]$ClientPort = 8001)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$dataDir = Join-Path $taskRoot 'data'
$python = Join-Path $taskRoot '.venv/Scripts/python.exe'
$backend = Join-Path $taskRoot 'backend'
$urlFile = Join-Path $dataDir 'client_gateway_url.txt'

if (-not (Test-Path -LiteralPath $python)) { throw '請先啟動彤靈主機，完成安裝。' }
try { $null = Invoke-RestMethod "http://127.0.0.1:$AdminPort/api/health" -TimeoutSec 3 }
catch { throw '請先在這台電腦啟動彤靈主機（8000）。' }
$ngrok = Get-Command ngrok -ErrorAction SilentlyContinue
if (-not $ngrok) { throw '找不到 ngrok。請先依官方指示安裝 ngrok。' }
if (Get-NetTCPConnection -LocalPort $ClientPort -State Listen -ErrorAction SilentlyContinue) {
    throw "客戶端連線埠 $ClientPort 已被使用。請先檢查現有客戶端服務。"
}
if (Get-NetTCPConnection -LocalPort 4040 -State Listen -ErrorAction SilentlyContinue) {
    throw 'ngrok 管理埠 4040 已被使用。請先關閉其他 ngrok 工作。'
}

$env:DATA_DIR = $dataDir
$env:TONGLING_CLIENT_GATEWAY = '1'
$gateway = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ClientPort") -WorkingDirectory $backend -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'client_gateway.log') -RedirectStandardError (Join-Path $dataDir 'client_gateway.err.log') -PassThru
$ready = $false
for ($i=0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$ClientPort/api/health" -TimeoutSec 2
        if ($health.gateway) { $ready = $true; break }
    } catch {}
}
if (-not $ready) {
    Get-Process -Id $gateway.Id -ErrorAction SilentlyContinue | Stop-Process
    throw '客戶端服務未能啟動。請檢查 data/client_gateway.err.log。'
}

$ngrokProcess = Start-Process -FilePath $ngrok.Source -ArgumentList @('http',"$ClientPort",'--log=stdout') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'ngrok.log') -RedirectStandardError (Join-Path $dataDir 'ngrok.err.log') -PassThru
$publicUrl = ''
for ($i=0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tunnels = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
        $publicUrl = @($tunnels.tunnels | Where-Object { $_.public_url -like 'https://*' } | Select-Object -First 1).public_url
        if ($publicUrl) { break }
    } catch {}
    if ($ngrokProcess.HasExited) { break }
}
if (-not $publicUrl) {
    $gatewayListener = Get-NetTCPConnection -LocalPort $ClientPort -State Listen -ErrorAction SilentlyContinue
    if ($gatewayListener) { Stop-Process -Id $gatewayListener.OwningProcess -ErrorAction SilentlyContinue }
    Get-Process -Id $gateway.Id -ErrorAction SilentlyContinue | Stop-Process
    Get-Process -Id $ngrokProcess.Id -ErrorAction SilentlyContinue | Stop-Process
    throw 'ngrok 未能取得公開網址。請先在本機執行 ngrok config add-authtoken，並檢查 data/ngrok.err.log。請勿把權杖貼到對話中。'
}

[System.IO.File]::WriteAllText($urlFile, $publicUrl, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $dataDir 'client_gateway.pid'), "$($gateway.Id)")
[System.IO.File]::WriteAllText((Join-Path $dataDir 'ngrok.pid'), "$($ngrokProcess.Id)")
$access = Invoke-RestMethod "http://127.0.0.1:$AdminPort/api/admin/client-access"
Write-Host "客戶端網址：$publicUrl"
Write-Host "配對碼：$($access.code)（每 10 分鐘更新；也可在彤靈本機設定頁查看）"
Write-Host '其他人透過此網址配對後，只會看到自己建立的專案。'
