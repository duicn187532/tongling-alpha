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
if ($Port -ne 8000) { throw '目前的雙擊啟動器固定使用管理 API 8000。' }
$existingBackend = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existingBackend) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
        if ($health.status -ne 'ok' -or $health.gateway) { throw '不是彤靈管理服務' }
    } catch { throw "連線埠 $Port 已被其他服務使用。" }
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
$nodeModulesChanged = $false
if (-not (Test-Path -LiteralPath $nodeMarker) -or
    (Get-Content -Raw -LiteralPath $nodeMarker).Trim() -ne $nodeDependencyHash) {
    Push-Location frontend
    try { & npm.cmd ci; if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' } }
    finally { Pop-Location }
    Set-Content -LiteralPath $nodeMarker -Value $nodeDependencyHash -Encoding ASCII
    $nodeModulesChanged = $true
}
$distIndex = Join-Path $taskRoot 'frontend/dist/index.html'
$frontendInputs = @('frontend/src', 'frontend/public', 'frontend/index.html',
                    'frontend/vite.config.ts', 'frontend/tsconfig.json',
                    'frontend/tsconfig.app.json', 'frontend/tsconfig.node.json',
                    'frontend/package-lock.json')
$latestInput = $frontendInputs | ForEach-Object {
    $path = Join-Path $taskRoot $_
    if (Test-Path -LiteralPath $path -PathType Container) {
        Get-ChildItem -LiteralPath $path -File -Recurse
    } elseif (Test-Path -LiteralPath $path) {
        Get-Item -LiteralPath $path
    }
} | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($nodeModulesChanged -or -not (Test-Path -LiteralPath $distIndex) -or
    $latestInput.LastWriteTimeUtc -gt (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc) {
    Push-Location frontend
    try { & npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } }
    finally { Pop-Location }
}
$env:DATA_DIR = Join-Path $taskRoot 'data'
$env:TONGLING_CLIENT_GATEWAY = '0'
Push-Location backend
try {
    & $taskPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
} finally { Pop-Location }
if (-not $existingBackend) {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'backend.cmd') -WorkingDirectory $taskRoot | Out-Null
}
if (-not (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'frontend.cmd') -WorkingDirectory $taskRoot | Out-Null
}
if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'file-server.cmd') -WorkingDirectory $taskRoot | Out-Null
}
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        $frontendHealth = Invoke-RestMethod 'http://127.0.0.1:5173/health' -TimeoutSec 2
        $page = Invoke-WebRequest 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 2
        if ($health.status -eq 'ok' -and -not $health.gateway -and
            $frontendHealth.role -eq 'host' -and $page.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw '前端或後端未能啟動，請查看各自的命令視窗。' }
$fileService = $null
for ($i = 0; $i -lt 15; $i++) {
    try {
        $fileService = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 3
        if ($fileService.service -ne 'company-file-server') { throw '8765 不是彤靈公司檔案服務器' }
        break
    } catch { Start-Sleep -Seconds 1 }
}
if ($fileService) {
    Write-Host "公司檔案服務器：http://127.0.0.1:8765（$($fileService.documents) 份文件）"
} else {
    Write-Warning '公司檔案服務器尚未就緒；彤靈內建知識庫仍可使用。請查看檔案服務器視窗。'
}
Write-Host '彤靈已啟動。前端：http://127.0.0.1:5173；後端：http://127.0.0.1:8000'
Write-Host '前端、後端與公司檔案服務器各有一個即時日誌視窗。遠端客戶端請雙擊 client.cmd。'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:5173' }
