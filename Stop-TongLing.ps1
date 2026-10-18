param([int]$AdminPort = 8000, [int]$ClientPort = 8001)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

& (Join-Path $root 'Stop-ClientAccess.ps1') -ClientPort $ClientPort
& (Join-Path $root 'Stop-FileServer.ps1')

$frontendListener = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($frontendListener) {
    $frontend = Get-CimInstance Win32_Process -Filter "ProcessId=$($frontendListener.OwningProcess)"
    if (-not $frontend -or $frontend.Name -ne 'node.exe' -or
        $frontend.CommandLine -notlike '*server.mjs host 5173 8000*') {
        throw '5173 不是此版彤靈前端，未停止。'
    }
    $frontendWindow = Get-CimInstance Win32_Process -Filter "ProcessId=$($frontend.ParentProcessId)"
    Stop-Process -Id $frontend.ProcessId -ErrorAction SilentlyContinue
    if ($frontendWindow -and $frontendWindow.Name -eq 'cmd.exe' -and
        $frontendWindow.CommandLine -like '*frontend.cmd*') {
        Stop-Process -Id $frontendWindow.ProcessId -ErrorAction SilentlyContinue
    }
}

$listener = Get-NetTCPConnection -LocalPort $AdminPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $listener) { Write-Host '彤靈主機已關閉。'; return }
$server = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$parent = if ($server) { Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ParentProcessId)" }
$backendWindow = if ($parent) { Get-CimInstance Win32_Process -Filter "ProcessId=$($parent.ParentProcessId)" }
$expected = '"' + $python + '"'
if (-not $server -or -not $parent -or
    $server.CommandLine -notlike "*uvicorn app.main:app*--port $AdminPort*" -or
    -not $parent.CommandLine.StartsWith($expected, [System.StringComparison]::OrdinalIgnoreCase) -or
    $parent.CommandLine -notlike "*uvicorn app.main:app*--port $AdminPort*") {
    throw "埠 $AdminPort 的程序無法確認為此資料夾的彤靈主機，未停止。"
}
Stop-Process -Id $server.ProcessId -ErrorAction Stop
Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue
if ($backendWindow -and $backendWindow.Name -eq 'cmd.exe' -and
    $backendWindow.CommandLine -like '*backend.cmd*') {
    Stop-Process -Id $backendWindow.ProcessId -ErrorAction SilentlyContinue
}
Write-Host '彤靈主機已關閉。'
