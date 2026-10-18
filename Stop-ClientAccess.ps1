param([int]$ClientPort = 8001)
$ErrorActionPreference = 'Stop'
$FrontendPort = 8002
$dataDir = Join-Path $PSScriptRoot 'data'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

function Read-TrackedPid([string]$name) {
    $path = Join-Path $dataDir $name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $content = (Get-Content -Raw -LiteralPath $path).Trim()
    if ($content -notmatch '^\d+$') { throw "無效的程序記錄：$name" }
    return [int]$content
}

$gatewayId = Read-TrackedPid 'client_gateway.pid'
if ($gatewayId) {
    $gateway = Get-CimInstance Win32_Process -Filter "ProcessId=$gatewayId"
    if ($gateway) {
        $expected = '"' + $python + '"'
        if (-not $gateway.CommandLine.StartsWith($expected, [System.StringComparison]::OrdinalIgnoreCase) -or
            $gateway.CommandLine -notlike "*uvicorn app.main:app*--port $ClientPort*") {
            throw '客戶端程序記錄與目前程序不符，未停止。'
        }
        $gatewayWindow = Get-CimInstance Win32_Process -Filter "ProcessId=$($gateway.ParentProcessId)"
        $children = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $gatewayId })
        foreach ($child in $children) {
            if ($child.CommandLine -like "*uvicorn app.main:app*--port $ClientPort*") {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $gatewayId -ErrorAction SilentlyContinue
        if ($gatewayWindow -and $gatewayWindow.Name -eq 'cmd.exe' -and
            $gatewayWindow.CommandLine -like '*client-gateway.cmd*') {
            Stop-Process -Id $gatewayWindow.ProcessId -ErrorAction SilentlyContinue
        }
    }
}

$frontendId = Read-TrackedPid 'client_frontend.pid'
if ($frontendId) {
    $frontend = Get-CimInstance Win32_Process -Filter "ProcessId=$frontendId"
    if ($frontend) {
        if ($frontend.Name -ne 'node.exe' -or
            $frontend.CommandLine -notlike "*server.mjs client $FrontendPort $ClientPort*") {
            throw '客戶端前端程序記錄與目前程序不符，未停止。'
        }
        $frontendWindow = Get-CimInstance Win32_Process -Filter "ProcessId=$($frontend.ParentProcessId)"
        Stop-Process -Id $frontendId -ErrorAction SilentlyContinue
        if ($frontendWindow -and $frontendWindow.Name -eq 'cmd.exe' -and
            $frontendWindow.CommandLine -like '*client-frontend.cmd*') {
            Stop-Process -Id $frontendWindow.ProcessId -ErrorAction SilentlyContinue
        }
    }
}

$ngrokId = Read-TrackedPid 'ngrok.pid'
if ($ngrokId) {
    $tunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$ngrokId"
    if ($tunnel) {
        if ($tunnel.Name -notlike 'ngrok*' -or $tunnel.CommandLine -notlike "*http $FrontendPort*") {
            throw 'ngrok 程序記錄與目前程序不符，未停止。'
        }
        $ngrokWindow = Get-CimInstance Win32_Process -Filter "ProcessId=$($tunnel.ParentProcessId)"
        Stop-Process -Id $ngrokId -ErrorAction SilentlyContinue
        if ($ngrokWindow -and $ngrokWindow.Name -eq 'cmd.exe' -and
            $ngrokWindow.CommandLine -like '*client-ngrok.cmd*') {
            Stop-Process -Id $ngrokWindow.ProcessId -ErrorAction SilentlyContinue
        }
    }
}

$activeTunnel = $false
try {
    $tunnels = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
    $activeTunnel = @($tunnels.tunnels | Where-Object {
        $_.public_url -like 'https://*' -and $_.config.addr -in @("http://localhost:$FrontendPort", "http://127.0.0.1:$FrontendPort")
    }).Count -gt 0
} catch {}
$records = @('client_gateway.pid', 'client_frontend.pid', 'ngrok.pid')
if (-not $activeTunnel) { $records += 'client_gateway_url.txt' }
foreach ($name in $records) {
    $path = Join-Path $dataDir $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
}
if ($activeTunnel) {
    Write-Host '仍偵測到運作中的 ngrok 客戶端通道；已保留公開網址記錄。'
} else {
    Write-Host '已關閉受限客戶端與此腳本建立的 ngrok 通道；本機主機仍可使用。'
}
