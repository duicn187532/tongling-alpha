param([switch]$Lan, [int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
function Get-DependencyHash([string]$relativePath) {
    $stream = [System.IO.File]::OpenRead((Join-Path $taskRoot $relativePath))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}
if ($Lan) { throw '管理入口只允許本機存取。請使用 Start-ClientAccess.ps1 分享受限客戶端。' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "連線埠 $Port 已使用；彤靈可能已在執行。"
}
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) { throw '請先安裝 Node.js 22.12 以上（含 npm）。' }
$nodeVersion = [version]((& node --version).TrimStart('v'))
if ($nodeVersion -lt [version]'22.12.0') { throw '彤靈需要 Node.js 22.12 或更新版本。' }
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw '請先安裝 Python 3.12。' }
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
}
$taskInstallMarker = Join-Path $taskRoot '.venv/tongling-installed.txt'
$taskDependencyHash = Get-DependencyHash 'backend/requirements.lock.txt'
if (-not (Test-Path -LiteralPath $taskInstallMarker) -or (Get-Content -Raw -LiteralPath $taskInstallMarker).Trim() -ne $taskDependencyHash) {
    & $taskPython -m pip install -r backend/requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    & $taskPython -m pip install --no-deps -e backend
    if ($LASTEXITCODE -ne 0) { throw 'Backend installation failed.' }
    Set-Content -LiteralPath $taskInstallMarker -Value $taskDependencyHash -Encoding ASCII
}
$nodeMarker = Join-Path $taskRoot 'frontend/node_modules/.tongling-lock-hash'
$nodeDependencyHash = Get-DependencyHash 'frontend/package-lock.json'
if (-not (Test-Path -LiteralPath $nodeMarker) -or
    (Get-Content -Raw -LiteralPath $nodeMarker).Trim() -ne $nodeDependencyHash) {
    Push-Location frontend
    try { & npm.cmd ci; if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' } }
    finally { Pop-Location }
    Set-Content -LiteralPath $nodeMarker -Value $nodeDependencyHash -Encoding ASCII
}
Push-Location frontend
try { & npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } }
finally { Pop-Location }
$env:DATA_DIR = Join-Path $taskRoot 'data'
$env:TONGLING_LAN = '0'
$env:TONGLING_CLIENT_GATEWAY = '0'
$taskBind = '127.0.0.1'
Push-Location backend
try {
    & $taskPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    Write-Host "TongLing: http://127.0.0.1:$Port"
    Write-Host '其他裝置請使用 Start-ClientAccess.ps1 啟動受限客戶端。'
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
    & $taskPython -m uvicorn app.main:app --host $taskBind --port $Port
} finally { Pop-Location }
