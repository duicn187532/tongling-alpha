param([int]$AdminPort = 8000, [int]$ClientPort = 8001, [int]$FrontendPort = 8002)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$dataDir = Join-Path $taskRoot 'data'
$python = Join-Path $taskRoot '.venv\Scripts\python.exe'
$urlFile = Join-Path $dataDir 'client_gateway_url.txt'

if ($AdminPort -ne 8000 -or $ClientPort -ne 8001 -or $FrontendPort -ne 8002) {
    throw '目前的雙擊啟動器固定使用 8000、8001、8002。'
}
if (-not (Test-Path -LiteralPath $python)) {
    & (Join-Path $taskRoot 'start.ps1') -NoBrowser
}
try { $admin = Invoke-RestMethod "http://127.0.0.1:$AdminPort/api/health" -TimeoutSec 3 }
catch {
    & (Join-Path $taskRoot 'start.ps1') -NoBrowser
    $admin = Invoke-RestMethod "http://127.0.0.1:$AdminPort/api/health" -TimeoutSec 3
}
if ($admin.gateway -or $admin.status -ne 'ok') { throw '8000 不是彤靈主機管理端。' }
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) { throw '找不到 ngrok。請先安裝並設定 ngrok。' }

$gatewayListener = Get-NetTCPConnection -LocalPort $ClientPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($gatewayListener) {
    $health = Invoke-RestMethod "http://127.0.0.1:$ClientPort/api/health" -TimeoutSec 3
    if (-not $health.gateway) { throw "連線埠 $ClientPort 不是彤靈受限後端，未更動。" }
} else {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'client-gateway.cmd') -WorkingDirectory $taskRoot | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$ClientPort/api/health" -TimeoutSec 2
            if ($health.gateway) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) { throw '客戶端後端未能啟動，請查看 Client Gateway 8001 視窗。' }
    $gatewayListener = Get-NetTCPConnection -LocalPort $ClientPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

$frontendListener = Get-NetTCPConnection -LocalPort $FrontendPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($frontendListener) {
    $frontendHealth = Invoke-RestMethod "http://127.0.0.1:$FrontendPort/health" -TimeoutSec 3
    if ($frontendHealth.role -ne 'client') { throw "連線埠 $FrontendPort 不是彤靈客戶端前端，未更動。" }
} else {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'client-frontend.cmd') -WorkingDirectory $taskRoot | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        try {
            $frontendHealth = Invoke-RestMethod "http://127.0.0.1:$FrontendPort/health" -TimeoutSec 2
            if ($frontendHealth.role -eq 'client') { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) { throw '客戶端前端未能啟動，請查看 Client Frontend 8002 視窗。' }
    $frontendListener = Get-NetTCPConnection -LocalPort $FrontendPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

$ngrokListener = Get-NetTCPConnection -LocalPort 4040 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ngrokListener) {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'client-ngrok.cmd') -WorkingDirectory $taskRoot | Out-Null
}
$publicUrl = ''
for ($i = 0; $i -lt 20; $i++) {
    try {
        $tunnels = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
        $matching = @($tunnels.tunnels | Where-Object {
            $_.public_url -like 'https://*' -and $_.config.addr -in @("http://localhost:$FrontendPort", "http://127.0.0.1:$FrontendPort")
        })
        if ($matching.Count -eq 1) { $publicUrl = $matching[0].public_url; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $publicUrl) { throw 'ngrok 尚未建立指向客戶端前端 8002 的 HTTPS 通道，請查看 ngrok 視窗。' }

[System.IO.File]::WriteAllText($urlFile, $publicUrl, (New-Object System.Text.UTF8Encoding($false)))
$gatewayProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($gatewayListener.OwningProcess)"
$gatewayParent = if ($gatewayProcess) { Get-CimInstance Win32_Process -Filter "ProcessId=$($gatewayProcess.ParentProcessId)" }
$expectedPython = '"' + $python + '"'
if ($gatewayParent -and $gatewayParent.CommandLine.StartsWith($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -and
    $gatewayParent.CommandLine -like "*uvicorn app.main:app*--port $ClientPort*") {
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'client_gateway.pid'), "$($gatewayParent.ProcessId)")
}
$frontendProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($frontendListener.OwningProcess)"
if ($frontendProcess -and $frontendProcess.Name -eq 'node.exe' -and
    $frontendProcess.CommandLine -like "*server.mjs client $FrontendPort $ClientPort*") {
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'client_frontend.pid'), "$($frontendProcess.ProcessId)")
}
$ngrokListener = Get-NetTCPConnection -LocalPort 4040 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
$ngrokProcess = if ($ngrokListener) { Get-CimInstance Win32_Process -Filter "ProcessId=$($ngrokListener.OwningProcess)" }
if ($ngrokProcess -and $ngrokProcess.Name -like 'ngrok*' -and $ngrokProcess.CommandLine -like "*http $FrontendPort*") {
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'ngrok.pid'), "$($ngrokProcess.ProcessId)")
}
$access = Invoke-RestMethod "http://127.0.0.1:$AdminPort/api/admin/client-access"
Write-Host "客戶端已啟用：$publicUrl"
Write-Host "配對碼：$($access.code)（每 10 分鐘更新；也可在彤靈本機設定頁查看）"
Write-Host '客戶端前端、受限後端與 ngrok 各有一個即時日誌視窗。'
