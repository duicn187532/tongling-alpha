$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listener) { Write-Host '公司檔案服務器已關閉。'; return }
$server = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$parent = if ($server) { Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ParentProcessId)" }
$window = if ($parent) { Get-CimInstance Win32_Process -Filter "ProcessId=$($parent.ParentProcessId)" }
$expected = '"' + $python + '"'
if (-not $server -or -not $parent -or
    $server.CommandLine -notlike '*uvicorn company_file_server.app:app*--port 8765*' -or
    -not $parent.CommandLine.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw '8765 的程序不是此資料夾的公司檔案服務器，未停止。'
}
Stop-Process -Id $server.ProcessId -ErrorAction Stop
Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue
if ($window -and $window.Name -eq 'cmd.exe' -and $window.CommandLine -like '*file-server.cmd*') {
    Stop-Process -Id $window.ProcessId -ErrorAction SilentlyContinue
}
Write-Host '公司檔案服務器已關閉。'
