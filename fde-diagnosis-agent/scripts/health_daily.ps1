# 每日巡检：跑 data_health.py 的四块检查，写一行摘要进日志，退出码原样透传
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\health_daily.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\health_daily.ps1 -LogPath eval\x.log
#
# 为什么要这一层：data_health.py 已经有非零退出码，但那只在「有人开终端跑」时看得到。
# 巡检的价值是留下痕迹，所以这条脚本只做三件事：跑、记一行、透传退出码。
#
# 刻意不做的三件：
#   - 不自动重试：重试会把「第一次就失败」藏起来
#   - 不自动清隔离区：脏行是证据，不是垃圾
#   - 不解析控制台输出：摘要只从 eval/data_health.json 读，输出格式会变、JSON 不会

param(
    [string]$LogPath = '',
    [int]$KeepLines = 2000,
    # 只在验证失败分支时替换：默认就是 scripts\data_health.py。
    # 留着这个口子是因为「包装层有没有正确处理失败」必须能被测到，
    # 否则和 data_health 自己一样，只有人手动跑才看得见它坏了。
    [string]$HealthScript = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
if (-not $HealthScript) { $HealthScript = Join-Path $root 'scripts\data_health.py' }
$dh = $HealthScript
$json = Join-Path $root 'eval\data_health.json'
if (-not $LogPath) { $LogPath = Join-Path $root 'eval\health_daily.log' }

if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[X] 找不到 venv: $py" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $dh)) {
    Write-Host "[X] 找不到检查入口: $dh" -ForegroundColor Red
    exit 1
}

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$started = Get-Date
$code = 0
$out = ''
try {
    $out = (& $py $dh 2>&1 | Out-String)
    $code = $LASTEXITCODE
} catch {
    $code = 1
    $out = $_.Exception.Message
}

$line = ''
if ($code -eq 0 -and (Test-Path -LiteralPath $json)) {
    try {
        $d = Get-Content -LiteralPath $json -Raw -Encoding UTF8 | ConvertFrom-Json
        $c = $d.contract
        $m = $d.matrix
        $i = $d.incremental
        $dl = $d.delete
        $q = $d.quarantine
        $line = "$ts OK   契约 $($c.pass)/$($c.total)  矩阵B一致 $($m.b_consistent)  增量 $($i.final_rows) 行/$($i.unique_ids) id  删除 活跃 $($dl.live_devices)/软删 $($dl.soft_deleted)  隔离 $($q.rows) 条"
    } catch {
        $line = "$ts WARN 退出码 0 但 data_health.json 读不出来：$($_.Exception.Message)"
        $code = 1
    }
} else {
    $issues = @()
    $stale = ''
    if (Test-Path -LiteralPath $json) {
        try {
            $prev = Get-Content -LiteralPath $json -Raw -Encoding UTF8 | ConvertFrom-Json
            $issues = @($prev.issues)
            # 崩在检查内部时，json 还是上一轮的、issues 也是旧的——
            # 拿旧 issues 当本轮结论，就是把「没跑成」说成「跑完了但有这些问题」。
            # 判据用文件写入时间，不用 json 里的 checked_at：前者是客观事实，
            # 后者是内容自己声明的，写错了也能骗过判断。
            if ((Get-Item -LiteralPath $json).LastWriteTime -lt $started) {
                $stale = '（本轮没写新 json，可能崩在检查内部）'
            }
        } catch { }
    }
    $detail = if ($issues.Count -gt 0) { ($issues -join ' / ') + $stale } else { '见本行上方完整输出' + $stale }
    $line = "$ts FAIL 退出码 $code：$detail"
    # 失败时把完整输出先落进日志，否则只看摘要分不清是哪一块崩的
    Add-Content -LiteralPath $LogPath -Value ("$ts ---- 完整输出 ----`r`n" + $out.Trim()) -Encoding UTF8
}

Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
Write-Host $line

# 只留最近 N 行：巡检跑一年，单文件会拖到几十 MB
if (Test-Path -LiteralPath $LogPath) {
    $all = @(Get-Content -LiteralPath $LogPath)
    if ($all.Count -gt $KeepLines) {
        $all[-$KeepLines..-1] | Set-Content -LiteralPath $LogPath -Encoding UTF8
    }
}

exit $code
