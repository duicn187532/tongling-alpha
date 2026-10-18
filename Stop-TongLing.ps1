param([int]$AdminPort = 8000, [int]$ClientPort = 8001)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

& (Join-Path $root 'Stop-ClientAccess.ps1') -ClientPort $ClientPort

$listener = Get-NetTCPConnection -LocalPort $AdminPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $listener) { Write-Host '彤靈主機已關閉。'; return }
$server = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$parent = if ($server) { Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ParentProcessId)" }
$expected = '"' + $python + '"'
if (-not $server -or -not $parent -or
    $server.CommandLine -notlike "*uvicorn app.main:app*--port $AdminPort*" -or
    -not $parent.CommandLine.StartsWith($expected, [System.StringComparison]::OrdinalIgnoreCase) -or
    $parent.CommandLine -notlike "*uvicorn app.main:app*--port $AdminPort*") {
    throw "埠 $AdminPort 的程序無法確認為此資料夾的彤靈主機，未停止。"
}
Stop-Process -Id $server.ProcessId -ErrorAction Stop
Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue
Write-Host '彤靈主機已關閉。'
