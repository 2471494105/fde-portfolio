# 一键起环境：检查依赖 → 起 mock QMS(8010) → 起 Web(8020) → 健康检查
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Open   # 起完自动开浏览器
#
# 说明：
#   - PostgreSQL 需已作为服务运行（本例 postgresql-x64-18），脚本只做连通性检查，不负责启动它
#   - Ollama 只在 search_manual 用到（nomic-embed-text），不在跑时给警告但不停下——
#     诊断链路本身走 DeepSeek API，不受影响

param(
    [switch]$Open,
    [int]$QmsPort = 8010,
    [int]$WebPort = 8020
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[X] 找不到 venv: $py" -ForegroundColor Red
    Write-Host "    先建虚拟环境并装依赖: python -m venv venv; & venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

function Test-Port([int]$port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

function Test-Http([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Host "== 环境检查 ==" -ForegroundColor Cyan

# 1) PostgreSQL
$pgOk = $false
try {
    $env:PGPASSWORD = 'postgres'
    $pg = & 'E:\POSTGRESQL\bin\psql.exe' -h localhost -p 5432 -U postgres -d fde_rag -t -c "select count(*) from devices;" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pgOk = $true
        Write-Host "[ok] PostgreSQL 可达，devices 表 $($pg.Trim()) 行"
    }
} catch { }
if (-not $pgOk) {
    Write-Host "[!] PostgreSQL 连不上（fde_rag 库）。先跑 data\schema.sql 与 data\seed\002_data.sql" -ForegroundColor Yellow
}

# 2) Ollama（只影响 search_manual）
if (Test-Http "http://localhost:11434/api/tags") {
    Write-Host "[ok] Ollama 可达（search_manual 可用）"
} else {
    Write-Host "[!] Ollama 未运行：search_manual 会失败，冲突检测类问题演示受限" -ForegroundColor Yellow
}

# 3) 模型 key —— key 可能来自两处，必须都认
#    项目根 .env（换 provider 后写这里，FDE_API_KEY / DEEPSEEK_API_KEY）
#    w0\.env（W0 阶段留下的 DEEPSEEK_API_KEY）
# 只查 w0\.env 会在切到中转站/本地后误报"诊断会失败"。
$rootEnv = Join-Path $root '.env'
$w0Env  = Join-Path $root 'w0\.env'

function Get-EnvVal([string]$file, [string]$name) {
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $line = Select-String -LiteralPath $file -Pattern "^$name=(.+)$" -ErrorAction SilentlyContinue
    if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    return $null
}

$keyOk  = $false
$keySrc = ''
if ((Get-EnvVal $rootEnv 'FDE_API_KEY') -or (Get-EnvVal $rootEnv 'DEEPSEEK_API_KEY')) {
    $keyOk = $true; $keySrc = '.env'
} elseif (Get-EnvVal $w0Env 'DEEPSEEK_API_KEY') {
    $keyOk = $true; $keySrc = 'w0\.env'
}

$prov  = Get-EnvVal $rootEnv 'FDE_PROVIDER'
if (-not $prov) { $prov = 'deepseek' }
$model = Get-EnvVal $rootEnv 'FDE_MODEL'
$base  = Get-EnvVal $rootEnv 'FDE_BASE_URL'
# 不用 ?? ：PowerShell 5.1 没有空值合并运算符，写上去会直接语法报错
if (-not $model) { $model = '默认' }
if (-not $base)  { $base  = '默认' }

if ($prov -eq 'ollama') {
    Write-Host "[i] provider=ollama，本地模型不需要 key"
} elseif ($keyOk) {
    Write-Host "[ok] key 已配置（$keySrc），provider=$prov model=$model base=$base"
} else {
    Write-Host "[!] 两个 .env 里都没有 key（FDE_API_KEY / DEEPSEEK_API_KEY），诊断会失败" -ForegroundColor Yellow
}

Write-Host "`n== 启动服务 ==" -ForegroundColor Cyan

# mock QMS
if (Test-Port $QmsPort) {
    Write-Host "[skip] mock QMS 已在 $QmsPort 运行"
} else {
    $p = Start-Process -FilePath $py `
        -ArgumentList '-m', 'uvicorn', 'mock_qms.server:app', '--host', '127.0.0.1', '--port', "$QmsPort" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 3
    if (Test-Http "http://127.0.0.1:$QmsPort/health") {
        Write-Host "[ok] mock QMS 已启动 (PID $($p.Id))"
    } else {
        Write-Host "[X] mock QMS 启动失败" -ForegroundColor Red
    }
}

# Web
if (Test-Port $WebPort) {
    Write-Host "[skip] Web 已在 $WebPort 运行"
} else {
    $p2 = Start-Process -FilePath $py `
        -ArgumentList '-m', 'uvicorn', 'app.web.server:app', '--host', '127.0.0.1', '--port', "$WebPort" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 4
    if (Test-Http "http://127.0.0.1:$WebPort/") {
        Write-Host "[ok] Web 已启动 (PID $($p2.Id))"
    } else {
        Write-Host "[X] Web 启动失败" -ForegroundColor Red
    }
}

$url = "http://127.0.0.1:$WebPort"
Write-Host "`n打开: $url" -ForegroundColor Green
Write-Host "命令行诊断: & '$py' scripts\run_case.py `"CX-101 误判率有点高，你看看怎么回事`""
Write-Host "评测:       & '$py' eval\run_eval.py"

if ($Open) { Start-Process $url }
