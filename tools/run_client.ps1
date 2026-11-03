# 一键启动桌宠客户端（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File tools\run_client.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
$client = Join-Path $here "client"
$py = Join-Path $here ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sys) {
        Write-Host "找不到 python，先装 Python 3.10+，或在本目录建虚拟环境：" -ForegroundColor Yellow
        Write-Host "  python -m venv .venv"
        Write-Host "  .venv\Scripts\python -m pip install -r client\requirements.txt"
        exit 1
    }
    $py = $sys.Source
}

& $py -c "import PySide6" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在安装 PySide6（首次需要下载约 200MB）…" -ForegroundColor Cyan
    & $py -m pip install -r (Join-Path $client "requirements.txt")
}

Write-Host "启动桌宠（关掉这个窗口不会退出，右键桌宠图标可退出）" -ForegroundColor Green
& $py (Join-Path $client "pet.py")
