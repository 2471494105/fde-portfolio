# 起接口（第三条取数路）。Ctrl+C 停。
# 用法：powershell -File api\run.ps1
$root = Split-Path -Parent $PSScriptRoot
$py = "E:\fde工程师学习指南\06_GitHub作品仓库\fde-diagnosis-agent\venv\Scripts\python.exe"
Set-Location $root
& $py -m uvicorn api.app:app --host 127.0.0.1 --port 8021 --log-level warning