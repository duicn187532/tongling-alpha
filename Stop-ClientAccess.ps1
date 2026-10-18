param([int]$ClientPort = 8001)
$ErrorActionPreference = 'Stop'
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
        $children = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $gatewayId })
        foreach ($child in $children) {
            if ($child.CommandLine -like "*uvicorn app.main:app*--port $ClientPort*") {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $gatewayId -ErrorAction SilentlyContinue
    }
}

$ngrokId = Read-TrackedPid 'ngrok.pid'
if ($ngrokId) {
    $tunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$ngrokId"
    if ($tunnel) {
        if ($tunnel.Name -notlike 'ngrok*' -or $tunnel.CommandLine -notlike "*http $ClientPort*") {
            throw 'ngrok 程序記錄與目前程序不符，未停止。'
        }
        Stop-Process -Id $ngrokId -ErrorAction SilentlyContinue
    }
}

foreach ($name in @('client_gateway.pid', 'ngrok.pid', 'client_gateway_url.txt')) {
    $path = Join-Path $dataDir $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
}
Write-Host '已關閉受限客戶端與此腳本建立的 ngrok 通道；本機主機仍可使用。'
